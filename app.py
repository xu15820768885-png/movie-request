#!/usr/bin/env python3
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import struct
import time
import base64
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Optional
from urllib.parse import quote, urlparse

import requests
import uvicorn
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from requests.adapters import HTTPAdapter
from dian115_openapi import Dian115OpenAPI, OpenAPIError
from hdhive_openapi import HDHiveOpenAPI, HDHiveOpenAPIError, TokenSet


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "movie-request.db"
WEB_PATH = Path(__file__).parent / "web" / "index.html"
PORT = int(os.getenv("PORT", "5056"))
SESSION_DAYS = 30
# Native HDHive subscriptions are created on demand.  The website deliberately
# does not poll subscription messages or transfer updated resources; HDHive's
# own bot sends the update link and the user decides what to save.
HDHIVE_MESSAGE_POLLING_ENABLED = False
STATUS_NAMES = {
    "pending": "待处理",
    "approved": "已收到",
    "searching": "寻找中",
    "available": "已入库",
    "rejected": "暂时无法完成",
}
TMDB_IMAGE_SIZES = {"w342", "w500", "original"}
TMDB_HTTP = requests.Session()
TMDB_HTTP.mount(
    "https://",
    HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0),
)

APP = FastAPI(title="映单", docs_url=None, redoc_url=None)
TELEGRAM_OFFSET = 0
CACHE_LOCK = Lock()
TMDB_RESPONSE_CACHE: dict[str, tuple[float, float, dict[str, Any]]] = {}
TMDB_REFRESHING: set[str] = set()
SETTINGS_CACHE: dict[tuple[str, str], str] = {}
TMDB_SEARCH_FRESH_SECONDS = 3600
TMDB_STALE_SECONDS = 7 * 86400
EMBY_LIBRARY_CACHE: dict[str, Any] = {
    "key": "",
    "expires": 0.0,
    "ids": set(),
    "refreshing": False,
}
EMBY_LIBRARY_CACHES: dict[str, dict[str, Any]] = {
    "p115": EMBY_LIBRARY_CACHE,
    "p123": {"key": "", "expires": 0.0, "ids": set(), "refreshing": False},
}
EMBY_EPISODE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
WECOM_TOKEN_CACHE: dict[str, Any] = {"key": "", "token": "", "expires": 0.0}
WECOM_TOKEN_LOCK = Lock()
QR_LOGIN_LOCK = Lock()
QR_LOGIN_TOKENS: dict[str, dict[str, Any]] = {}
P115_APPS = {
    "alipaymini": "115生活_支付宝小程序端",
    "wechatmini": "115生活_微信小程序端",
    "qandroid": "115管理_安卓端",
    "android": "115生活_安卓端",
    "ios": "115生活_苹果端",
    "ipad": "115生活_苹果平板端",
    "os_windows": "115生活_Windows端",
    "os_mac": "115生活_macOS端",
    "os_linux": "115生活_Linux端",
    "harmony": "115_鸿蒙端",
    "tv": "115生活_电视端",
}


@APP.exception_handler(HTTPException)
async def movie_http_exception_handler(
    request: Request,
    error: HTTPException,
) -> JSONResponse:
    if request.url.path in ("/api/hdhive/transfer", "/api/dian/transfer"):
        user = session_user(request.cookies.get("movie_session"))
        if user:
            send_notifications(
                f"❌ 资源转存失败 · "
                f"{'123' if user['storage_destination'] == 'p123' else '115'}\n\n"
                f"账号：{user['display_name']}\n原因：{error.detail}"
            )
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail},
        headers=error.headers,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tmdb_image_proxy_url(path: Any, size: str = "w500") -> str:
    clean_path = str(path or "").strip().lstrip("/")
    if not clean_path:
        return ""
    clean_size = size if size in TMDB_IMAGE_SIZES else "w500"
    return f"/api/tmdb/image/{clean_size}/{quote(clean_path, safe='._-')}"


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with db() as connection:
        # WAL is persistent database state. Setting it once during startup
        # avoids an extra PRAGMA and possible lock on every short-lived read.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                active INTEGER NOT NULL DEFAULT 1,
                storage_destination TEXT NOT NULL DEFAULT 'p115',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tmdb_cache (
                cache_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                expires_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tmdb_cache_expiry_idx
                ON tmdb_cache(expires_at);
            CREATE TABLE IF NOT EXISTS tmdb_search_catalog (
                media_type TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                search_text TEXT NOT NULL,
                popularity REAL NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(media_type, tmdb_id)
            );
            CREATE INDEX IF NOT EXISTS tmdb_search_catalog_updated_idx
                ON tmdb_search_catalog(updated_at DESC);
            CREATE TABLE IF NOT EXISTS movie_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                tmdb_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT NOT NULL,
                original_title TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                poster_path TEXT NOT NULL DEFAULT '',
                overview TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                admin_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS request_user_idx ON movie_requests(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS request_status_idx ON movie_requests(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS hdhive_oauth (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                client_id TEXT NOT NULL DEFAULT '',
                app_secret_cipher TEXT NOT NULL DEFAULT '',
                access_token_cipher TEXT NOT NULL DEFAULT '',
                refresh_token_cipher TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT '',
                redirect_uri TEXT NOT NULL DEFAULT '',
                proxy_url_cipher TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'waiting_approval',
                token_expires_at TEXT NOT NULL DEFAULT '',
                authorized_at TEXT NOT NULL DEFAULT '',
                state_hash TEXT NOT NULL DEFAULT '',
                state_expires_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO hdhive_oauth(id, updated_at) VALUES(1, '');
            CREATE TABLE IF NOT EXISTS tv_follows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tmdb_id INTEGER NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'tv',
                title TEXT NOT NULL,
                original_title TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                poster_path TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                mode TEXT NOT NULL DEFAULT 'notify',
                source_preference TEXT NOT NULL DEFAULT 'hdhive_first',
                baseline_season INTEGER NOT NULL DEFAULT 1,
                baseline_episode INTEGER NOT NULL DEFAULT 0,
                last_seen_season INTEGER NOT NULL DEFAULT 1,
                last_seen_episode INTEGER NOT NULL DEFAULT 0,
                last_transferred_season INTEGER NOT NULL DEFAULT 0,
                last_transferred_episode INTEGER NOT NULL DEFAULT 0,
                hdhive_subscription_id INTEGER,
                last_checked_at TEXT NOT NULL DEFAULT '',
                last_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, tmdb_id)
            );
            CREATE INDEX IF NOT EXISTS tv_follow_active_idx
                ON tv_follows(active, updated_at DESC);
            CREATE INDEX IF NOT EXISTS tv_follow_user_search_idx
                ON tv_follows(user_id, active, media_type, tmdb_id);
            CREATE TABLE IF NOT EXISTS resource_transfer_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                follow_id INTEGER REFERENCES tv_follows(id) ON DELETE SET NULL,
                source TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL DEFAULT 0,
                season_number INTEGER NOT NULL DEFAULT 0,
                episode_number INTEGER NOT NULL DEFAULT 0,
                transfer_scope TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            DROP INDEX IF EXISTS resource_transfer_success_idx;
            CREATE UNIQUE INDEX resource_transfer_success_idx
                ON resource_transfer_log(source, resource_key, transfer_scope, episode_number)
                WHERE status = 'success';
            CREATE UNIQUE INDEX IF NOT EXISTS resource_episode_success_idx
                ON resource_transfer_log(tmdb_id, season_number, episode_number)
                WHERE status = 'success' AND episode_number > 0;
            CREATE INDEX IF NOT EXISTS resource_manual_success_tmdb_idx
                ON resource_transfer_log(tmdb_id)
                WHERE transfer_scope = 'manual'
                  AND status = 'success' AND tmdb_id > 0;
            CREATE TABLE IF NOT EXISTS hdhive_message_log (
                message_key TEXT PRIMARY KEY,
                event_type TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wecom_message_log (
                message_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tv_follow_resources (
                follow_id INTEGER NOT NULL REFERENCES tv_follows(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY(follow_id, source, resource_key)
            );
            CREATE INDEX IF NOT EXISTS tv_follow_resource_time_idx
                ON tv_follow_resources(follow_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS hdhive_media_targets (
                media_type TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                target_key TEXT NOT NULL,
                media_url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(media_type, tmdb_id)
            );
            """
        )
        user_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "storage_destination" not in user_columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN storage_destination "
                "TEXT NOT NULL DEFAULT 'p115'"
            )
        connection.execute("DROP TABLE IF EXISTS p123_transfer_jobs")
        connection.execute(
            "DELETE FROM settings WHERE key IN ("
            "'p123_passport', 'p123_password_cipher', 'p123_client_id', "
            "'p123_client_secret', 'p123_target_id', 'p123_target_name')"
        )
        follow_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tv_follows)").fetchall()
        }
        if "media_type" not in follow_columns:
            connection.execute(
                "ALTER TABLE tv_follows "
                "ADD COLUMN media_type TEXT NOT NULL DEFAULT 'tv'"
            )
        hot_setting_keys = (
            "tmdb_token", "emby_url", "emby_api_key",
            "p123_emby_url", "p123_emby_api_key",
        )
        hot_settings = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?, ?)",
                hot_setting_keys,
            ).fetchall()
        }
        with CACHE_LOCK:
            for key in hot_setting_keys:
                SETTINGS_CACHE[(str(DB_PATH), key)] = hot_settings.get(key, "")


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def clean_username(value: Any) -> str:
    username = str(value or "").strip()
    if not 2 <= len(username) <= 32:
        raise HTTPException(400, "账号需为 2 到 32 个字符")
    return username


def clean_password(value: Any) -> str:
    password = str(value or "")
    if len(password) < 8:
        raise HTTPException(400, "密码至少需要 8 位")
    return password


def setting(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def cached_settings(*keys: str) -> dict[str, str]:
    path = str(DB_PATH)
    with CACHE_LOCK:
        cached_values = {
            key: SETTINGS_CACHE[(path, key)]
            for key in keys
            if (path, key) in SETTINGS_CACHE
        }
    missing = [key for key in keys if key not in cached_values]
    if missing:
        placeholders = ",".join("?" for _ in missing)
        with db() as connection:
            rows = connection.execute(
                f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
                missing,
            ).fetchall()
        loaded = {str(row["key"]): str(row["value"]) for row in rows}
        with CACHE_LOCK:
            for key in missing:
                value = loaded.get(key, "")
                SETTINGS_CACHE[(path, key)] = value
                cached_values[key] = value
    return {key: cached_values.get(key, "") for key in keys}


def cached_setting(key: str) -> str:
    return cached_settings(key)[key]


def set_setting(connection: sqlite3.Connection, key: str, value: Any) -> None:
    clean_value = str(value or "").strip()
    connection.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, clean_value),
    )
    with CACHE_LOCK:
        SETTINGS_CACHE[(str(DB_PATH), key)] = clean_value


HDHIVE_SCOPES = "meta query unlock write vip"


def hdhive_key_path() -> Path:
    return DATA_DIR / "hdhive-fernet.key"


def hdhive_fernet() -> Fernet:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = hdhive_key_path()
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o600)
    return Fernet(path.read_bytes().strip())


def encrypt_secret(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    return hdhive_fernet().encrypt(clean.encode()).decode()


def decrypt_secret(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    try:
        return hdhive_fernet().decrypt(clean.encode()).decode()
    except (InvalidToken, ValueError) as error:
        raise HTTPException(500, "敏感凭据无法解密，请管理员重新保存配置") from error


def hdhive_oauth_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM hdhive_oauth WHERE id = 1").fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO hdhive_oauth(id, updated_at) VALUES(1, ?)",
            (now_iso(),),
        )
        row = connection.execute("SELECT * FROM hdhive_oauth WHERE id = 1").fetchone()
    return row


def hdhive_save_tokens(tokens: TokenSet) -> None:
    if not tokens.access_token:
        raise HTTPException(502, "影巢没有返回 Access Token")
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=max(tokens.expires_in, 0))
    ).isoformat()
    with db() as connection:
        current = hdhive_oauth_row(connection)
        refresh = tokens.refresh_token or decrypt_secret(current["refresh_token_cipher"])
        connection.execute(
            "UPDATE hdhive_oauth SET access_token_cipher = ?, "
            "refresh_token_cipher = ?, scopes = ?, token_expires_at = ?, "
            "authorized_at = ?, status = 'connected', last_error = '', updated_at = ? "
            "WHERE id = 1",
            (
                encrypt_secret(tokens.access_token),
                encrypt_secret(refresh),
                " ".join(tokens.scopes) or current["scopes"] or HDHIVE_SCOPES,
                expires_at,
                now_iso(),
                now_iso(),
            ),
        )


def hdhive_client(require_authorized: bool = True) -> HDHiveOpenAPI:
    with db() as connection:
        row = hdhive_oauth_row(connection)
        api_key = decrypt_secret(row["app_secret_cipher"])
        access_token = decrypt_secret(row["access_token_cipher"])
        refresh_token = decrypt_secret(row["refresh_token_cipher"])
        proxy_url = (
            decrypt_secret(row["proxy_url_cipher"])
            or os.getenv("HDHIVE_PROXY_URL", "").strip()
        )
    if not row["client_id"] or not api_key:
        raise HTTPException(503, "影巢应用仍在等待审核或尚未填写凭证")
    if require_authorized and not access_token:
        raise HTTPException(503, "影巢应用尚未完成 OAuth 授权")
    return HDHiveOpenAPI(
        api_key=api_key,
        access_token=access_token,
        refresh_token=refresh_token,
        proxy_url=proxy_url,
        on_token_refresh=hdhive_save_tokens,
    )


def hdhive_call(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = getattr(hdhive_client(), method)(*args, **kwargs)
        with db() as connection:
            connection.execute(
                "UPDATE hdhive_oauth SET status = 'connected', last_error = '', "
                "updated_at = ? WHERE id = 1",
                (now_iso(),),
            )
        return result if isinstance(result, dict) else {"data": result}
    except HTTPException:
        raise
    except HDHiveOpenAPIError as error:
        with db() as connection:
            connection.execute(
                "UPDATE hdhive_oauth SET last_error = ?, status = ?, updated_at = ? "
                "WHERE id = 1",
                (
                    str(error),
                    "rate_limited" if error.status == 429 else "error",
                    now_iso(),
                ),
            )
        if error.status == 429:
            wait = f"，请等待约 {error.retry_after} 秒" if error.retry_after else ""
            raise HTTPException(429, f"影巢调用已达到限制{wait}") from error
        raise HTTPException(error.status or 502, f"影巢接口：{error}") from error
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as error:
        raise HTTPException(502, f"暂时无法连接影巢：{error}") from error


def hdhive_media_page(media_url: str) -> str:
    try:
        return hdhive_client().media_page(media_url)
    except HTTPException:
        raise
    except HDHiveOpenAPIError as error:
        raise HTTPException(
            error.status or 502,
            f"影巢影片页面：{error}",
        ) from error
    except (requests.RequestException, RuntimeError, ValueError) as error:
        raise HTTPException(502, f"暂时无法读取影巢影片页面：{error}") from error


def dian_client() -> Dian115OpenAPI:
    with db() as connection:
        base_url = setting(connection, "dian_base_url")
        api_key = setting(connection, "dian_api_key")
    if not base_url or not api_key:
        raise HTTPException(503, "管理员还没有配置癫影 OpenAPI")
    return Dian115OpenAPI(base_url, api_key)


def dian_call(method: str, *args: Any) -> dict[str, Any]:
    try:
        result = getattr(dian_client(), method)(*args)
        return result if isinstance(result, dict) else {"data": result}
    except HTTPException:
        raise
    except OpenAPIError as error:
        raise HTTPException(error.status or 502, f"癫影接口：{error}") from error
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as error:
        raise HTTPException(502, f"暂时无法连接癫影：{error}") from error


def signin_result_message(result: dict[str, Any], default: str) -> str:
    data = result.get("data")
    return str(
        (data.get("message") if isinstance(data, dict) else "")
        or result.get("message")
        or default
    )


def signin_points(result: dict[str, Any]) -> tuple[Any, Any]:
    """Extract the points earned and current total from Dian response variants."""
    earned_keys = (
        "award", "earned_points", "points_earned", "reward_points", "signin_points",
        "sign_in_points", "added_points", "add_points", "gain_points",
        "result_points", "score_earned", "reward_score", "signin_score",
        "result_score",
        "签到积分", "获得积分",
    )
    total_keys = (
        "new_balance", "total_points", "total_point", "points_total", "current_points",
        "user_points", "point_balance", "points_balance", "total_score",
        "score_total", "current_score", "user_score", "score_balance",
        "balance_points", "总积分", "当前积分", "积分余额",
    )

    objects: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            objects.append(value)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    def pick(keys: tuple[str, ...]) -> Any:
        for item in objects:
            for key in keys:
                value = item.get(key)
                if value is not None and not isinstance(value, (dict, list, bool)):
                    text = str(value).strip()
                    if text:
                        return value
        return None

    collect(result)
    earned = pick(earned_keys)
    total = pick(total_keys)
    if earned is None:
        # Some Dian versions return the random check-in result separately and
        # use ``points``/``score`` for the account balance.
        signin_result = pick(("signin_result", "sign_in_result", "result"))
        balance = pick(("points", "point", "score"))
        if signin_result is not None and balance is not None:
            earned, total = signin_result, total if total is not None else balance
        else:
            earned = balance
    message = signin_result_message(result, "")
    if earned is None:
        match = re.search(
            r"(?:获得|增加|奖励|本次(?:签到)?)[^\d+-]*([+-]?\d+(?:\.\d+)?)\s*积分",
            message,
        )
        if match:
            earned = match.group(1).lstrip("+")
    if total is None:
        match = re.search(
            r"(?:总积分|当前积分|积分余额)[^\d+-]*([+-]?\d+(?:\.\d+)?)",
            message,
        )
        if match:
            total = match.group(1)
    return earned, total


def signin_notification(
    result: dict[str, Any],
    source_label: str,
    mode_label: str,
    service_label: str = "癫影",
) -> str:
    message = signin_result_message(result, "签到成功")
    earned, total = signin_points(result)
    lines = [
        f"✅ {service_label}签到成功",
        "",
        f"{source_label} · {mode_label}",
        f"结果：{message}",
    ]
    if earned is not None:
        lines.append(f"本次签到积分：{earned}")
    if total is not None:
        lines.append(f"当前总积分：{total}")
    return "\n".join(lines)


def perform_dian_signin(mode: str, source: str = "manual") -> dict[str, Any]:
    if mode not in ("normal", "lucky"):
        raise HTTPException(400, "签到模式无效")
    attempted_at = now_iso()
    mode_label = "运气签到" if mode == "lucky" else "普通签到"
    source_label = "自动签到" if source == "auto" else "手动签到"
    try:
        result = dian_call("signin", mode)
    except HTTPException as error:
        message = str(error.detail)
        with db() as connection:
            set_setting(connection, "dian_last_signin_at", attempted_at)
            set_setting(connection, "dian_last_signin_day", datetime.now().date().isoformat())
            set_setting(connection, "dian_last_signin_mode", mode)
            set_setting(connection, "dian_last_signin_status", "failed")
            set_setting(connection, "dian_last_signin_message", message)
        send_notifications(
            f"❌ 癫影签到失败\n\n{source_label} · {mode_label}\n原因：{message}"
        )
        raise
    message = signin_result_message(result, "签到成功")
    with db() as connection:
        set_setting(connection, "dian_last_signin_at", attempted_at)
        set_setting(connection, "dian_last_signin_day", datetime.now().date().isoformat())
        set_setting(connection, "dian_last_signin_mode", mode)
        set_setting(connection, "dian_last_signin_status", "success")
        set_setting(connection, "dian_last_signin_message", message)
        set_setting(connection, "dian_last_signin_result", json.dumps(result, ensure_ascii=False))
    send_notifications(signin_notification(result, source_label, mode_label))
    return result


def perform_hdhive_signin(mode: str, source: str = "manual") -> dict[str, Any]:
    if mode not in ("normal", "lucky"):
        raise HTTPException(400, "签到模式无效")
    attempted_at = now_iso()
    mode_label = "运气签到" if mode == "lucky" else "普通签到"
    source_label = "自动签到" if source == "auto" else "手动签到"
    try:
        result = hdhive_call("checkin", is_gambler=mode == "lucky")
    except HTTPException as error:
        message = str(error.detail)
        with db() as connection:
            set_setting(connection, "hdhive_last_signin_at", attempted_at)
            set_setting(
                connection,
                "hdhive_last_signin_day",
                datetime.now().date().isoformat(),
            )
            set_setting(connection, "hdhive_last_signin_mode", mode)
            set_setting(connection, "hdhive_last_signin_status", "failed")
            set_setting(connection, "hdhive_last_signin_message", message)
        send_notifications(
            f"❌ 影巢签到失败\n\n{source_label} · {mode_label}\n原因：{message}"
        )
        raise
    try:
        profile = hdhive_response_data(hdhive_call("me"))
        if isinstance(profile, dict) and profile.get("points") is not None:
            data = result.get("data")
            if not isinstance(data, dict):
                data = {}
                result["data"] = data
            data["total_points"] = profile["points"]
    except HTTPException:
        # The check-in itself succeeded. Account detail lookup is optional and
        # must not turn it into a failed sign-in notification.
        pass
    message = signin_result_message(result, "签到成功")
    with db() as connection:
        set_setting(connection, "hdhive_last_signin_at", attempted_at)
        set_setting(
            connection,
            "hdhive_last_signin_day",
            datetime.now().date().isoformat(),
        )
        set_setting(connection, "hdhive_last_signin_mode", mode)
        set_setting(connection, "hdhive_last_signin_status", "success")
        set_setting(connection, "hdhive_last_signin_message", message)
        set_setting(
            connection,
            "hdhive_last_signin_result",
            json.dumps(result, ensure_ascii=False),
        )
    send_notifications(
        signin_notification(
            result,
            source_label,
            mode_label,
            service_label="影巢",
        )
    )
    return result


def p115_cookie_path() -> Path:
    return DATA_DIR / "115-cookies.txt"


def p115_client(require_login: bool = True):
    try:
        from p115client import P115Client
    except ImportError as error:
        raise HTTPException(503, "当前镜像缺少 p115client") from error
    path = p115_cookie_path()
    if require_login and (not path.exists() or not path.read_text().strip()):
        raise HTTPException(503, "115 尚未扫码登录")
    return P115Client(path, console_qrcode=False)


def response_ok(payload: dict[str, Any]) -> bool:
    return bool(payload.get("state", payload.get("success", True))) and not payload.get("errno")


def response_message(payload: dict[str, Any], fallback: str) -> str:
    return str(
        payload.get("error")
        or payload.get("error_msg")
        or payload.get("message")
        or payload.get("msg")
        or fallback
    )


def response_summary(payload: dict[str, Any]) -> str:
    allowed = (
        "state", "success", "errno", "errNo", "errcode", "code",
        "error", "error_msg", "message", "msg", "task_id", "info_hash",
    )
    summary = {
        key: payload[key]
        for key in allowed
        if key in payload and payload[key] not in (None, "")
    }
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("task_id", "info_hash", "count", "file_id", "cid"):
            if key in data and data[key] not in (None, ""):
                summary[f"data.{key}"] = data[key]
    return json.dumps(summary, ensure_ascii=False) if summary else "无状态字段"


def p115_call(label: str, method: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = method(*args, **kwargs)
        if not isinstance(result, dict):
            raise RuntimeError("115返回了无效响应")
        return result
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(502, f"{label}：{error}") from error


def is_115_share_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in ("http", "https")
        and (
            host == "115.com"
            or host.endswith(".115.com")
            or host == "115cdn.com"
            or host.endswith(".115cdn.com")
        )
        and parsed.path.startswith("/s/")
    )


def extract_share_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in (
        "list", "items", "shares", "records", "rows", "results",
        "resources", "tasks", "data",
    ):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def p115_folder_snapshot(client: Any, cid: str) -> set[tuple[str, str, str]]:
    result = p115_call(
        "读取115目标目录失败",
        client.fs_files,
        {
            "cid": cid,
            "limit": 200,
            "show_dir": 1,
            "cur": 1,
            "o": "user_ptime",
            "asc": 0,
        }
    )
    if not response_ok(result):
        raise HTTPException(502, response_message(result, "无法验证115目标目录"))
    return {
        (
            str(item.get("fid") or item.get("file_id") or item.get("cid") or ""),
            str(item.get("n") or item.get("file_name") or item.get("name") or ""),
            str(item.get("s") or item.get("file_size") or item.get("size") or ""),
        )
        for item in extract_share_items(result)
    }


def p115_offline_snapshot(client: Any) -> set[tuple[str, str, str]]:
    result = p115_call(
        "读取115云下载任务失败",
        client.clouddownload_task_list,
        {"page": 1, "page_size": 100},
    )
    if not response_ok(result):
        raise HTTPException(502, response_message(result, "无法验证115云下载任务"))
    return {
        (
            str(item.get("info_hash") or item.get("task_id") or item.get("id") or ""),
            str(item.get("url") or item.get("name") or item.get("file_name") or ""),
            str(item.get("status") or item.get("stat") or ""),
        )
        for item in extract_share_items(result)
    }


def wait_for_p115_change(snapshot: Any, before: set[Any]) -> bool:
    for _ in range(4):
        after = snapshot()
        if after - before:
            return True
        time.sleep(0.5)
    return False


def p115_share_item_id(item: dict[str, Any]) -> str:
    return str(item.get("fid") or item.get("file_id") or item.get("cid") or "")


def p115_share_item_name(item: dict[str, Any]) -> str:
    return str(item.get("n") or item.get("file_name") or item.get("name") or "")


def p115_share_item_is_dir(item: dict[str, Any]) -> bool:
    kind = str(
        item.get("file_type")
        or item.get("type")
        or item.get("category")
        or ""
    ).lower()
    return bool(
        item.get("is_dir")
        or item.get("is_folder")
        or kind in ("dir", "folder", "directory")
        or (item.get("cid") and not item.get("fid") and not item.get("file_id"))
    )


def p115_share_tree(
    client: Any,
    share_url: str,
    *,
    cid: int = 0,
    depth: int = 0,
    max_depth: int = 4,
    visited: Optional[set[str]] = None,
    parent_path: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    visited = visited or set()
    result = p115_call(
        "读取115分享文件失败",
        client.share_snap,
        cid,
        share_url=share_url,
    )
    if not response_ok(result):
        raise HTTPException(502, response_message(result, "无法读取115分享"))
    output: list[dict[str, Any]] = []
    for item in extract_share_items(result):
        entry = dict(item)
        entry["_share_id"] = p115_share_item_id(item)
        entry["_share_name"] = p115_share_item_name(item)
        entry["_share_depth"] = depth
        entry["_share_is_dir"] = p115_share_item_is_dir(item)
        entry["_share_path"] = (*parent_path, entry["_share_name"])
        output.append(entry)
        folder_id = entry["_share_id"]
        if (
            entry["_share_is_dir"]
            and folder_id
            and folder_id not in visited
            and depth < max_depth
        ):
            visited.add(folder_id)
            output.extend(
                p115_share_tree(
                    client,
                    share_url,
                    cid=int(folder_id),
                    depth=depth + 1,
                    max_depth=max_depth,
                    visited=visited,
                    parent_path=entry["_share_path"],
                )
            )
    return output


def select_missing_episode_files(
    items: list[dict[str, Any]],
    *,
    baseline_episode: int,
    wanted_episodes: Optional[set[int]] = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    wanted = wanted_episodes or set()
    for item in items:
        if item.get("_share_is_dir"):
            continue
        episode = parse_episode_spec(item.get("_share_name"))
        numbers = set(episode["episode_numbers"])
        if not numbers:
            continue
        if wanted:
            numbers &= wanted
        else:
            numbers = {number for number in numbers if number > baseline_episode}
        if numbers:
            selected.append(item)
    return selected


def p115_share_item_size(item: dict[str, Any]) -> int:
    for key in ("s", "file_size", "size", "bytes"):
        value = item.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            parsed = resource_size_bytes(value)
            if parsed > 0:
                return parsed
    return 0


def p115_share_item_sha1(item: dict[str, Any]) -> str:
    for key in ("sha1", "sha", "file_sha1", "fileSha1", "sha1_value"):
        value = str(item.get(key) or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    return ""


def pansave_proxy(proxy_url: str) -> Optional[dict[str, Any]]:
    value = str(proxy_url or "").strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"http://{value}")
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise HTTPException(400, "Telegram 代理仅支持 HTTP、SOCKS4 或 SOCKS5")
    if not parsed.hostname or not parsed.port:
        raise HTTPException(400, "Telegram 代理格式无效，请填写协议、地址和端口")
    proxy: dict[str, Any] = {
        "proxy_type": "http" if scheme == "https" else scheme,
        "addr": parsed.hostname,
        "port": parsed.port,
        "rdns": True,
    }
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def pansave_client(
    api_id: int,
    api_hash: str,
    session_string: str = "",
    proxy_url: str = "",
) -> Any:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as error:
        raise HTTPException(503, "当前镜像缺少 Telegram 用户会话依赖") from error
    return TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        proxy=pansave_proxy(proxy_url),
        receive_updates=False,
        auto_reconnect=False,
        connection_retries=2,
        request_retries=2,
        timeout=15,
    )


def pansave_login_settings() -> dict[str, Any]:
    with db() as connection:
        api_id_text = setting(connection, "pansave_telegram_api_id")
        api_hash_cipher = setting(connection, "pansave_telegram_api_hash_cipher")
        session_cipher = setting(connection, "pansave_telegram_session_cipher")
        return {
            "api_id": int(api_id_text or 0),
            "api_hash": decrypt_secret(api_hash_cipher) if api_hash_cipher else "",
            "phone": setting(connection, "pansave_telegram_phone"),
            "session": decrypt_secret(session_cipher) if session_cipher else "",
            "bot_username": setting(connection, "pansave_bot_username") or "pansavenb_bot",
            "proxy_url": setting(connection, "pansave_telegram_proxy"),
        }


def clean_pansave_bot_username(value: Any) -> str:
    username = str(value or "pansavenb_bot").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        raise HTTPException(400, "123机器人用户名格式无效")
    return username


async def pansave_send_link(share_url: str) -> dict[str, Any]:
    url = str(share_url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(400, "资源链接格式无效")
    settings = pansave_login_settings()
    if not (
        settings["api_id"]
        and settings["api_hash"]
        and settings["phone"]
        and settings["session"]
    ):
        raise HTTPException(503, "管理员尚未完成123 Telegram用户账号登录")
    client = pansave_client(
        settings["api_id"],
        settings["api_hash"],
        settings["session"],
        settings["proxy_url"],
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise HTTPException(401, "123 Telegram用户会话已失效，请重新登录")
        bot_username = clean_pansave_bot_username(settings["bot_username"])
        message = await client.send_message(bot_username, url)
        return {
            "bot_username": bot_username,
            "message_id": int(getattr(message, "id", 0) or 0),
        }
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(502, f"发送给123失败：{error}") from error
    finally:
        await client.disconnect()


async def deliver_to_pansave(
    *,
    user: dict[str, Any],
    share_url: str,
    source: str,
    resource_key: str,
    title: str = "",
    tmdb_id: int = 0,
    season_number: int = 0,
    episode_numbers: Optional[list[int]] = None,
) -> dict[str, Any]:
    result = await pansave_send_link(share_url)
    message = "资源链接已发送给123机器人"
    record_transfer(
        user_id=int(user["id"]),
        source=source,
        resource_key=resource_key,
        tmdb_id=tmdb_id,
        transfer_scope="manual",
        status="success",
        detail=message,
        season_number=season_number,
        episode_numbers=episode_numbers or [],
    )
    send_notifications(
        f"📨 资源已提交123\n\n"
        f"{title or '资源'} · {user['display_name']}\n"
        f"已发送给 @{result['bot_username']}"
    )
    return {
        "ok": True,
        "mode": "pansave",
        "message": message,
    }


def select_largest_missing_episode_files(
    items: list[dict[str, Any]],
    missing_episodes: set[int],
) -> tuple[list[dict[str, Any]], set[int]]:
    """Pick the largest media file for every missing episode.

    A single file may cover several explicitly named episodes. Subtitle files
    for the selected episodes are included as companions, while posters and
    unrelated extras remain excluded.
    """

    media_extensions = {
        ".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".webm"
    }
    subtitle_extensions = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
    best_by_episode: dict[int, dict[str, Any]] = {}
    subtitles: list[dict[str, Any]] = []
    for item in items:
        if item.get("_share_is_dir"):
            continue
        name = str(item.get("_share_name") or "")
        suffix = Path(name).suffix.lower()
        parsed = parse_episode_spec(name)
        episodes = set(parsed["episode_numbers"]) & missing_episodes
        if not episodes:
            continue
        if suffix in subtitle_extensions:
            subtitles.append(item)
            continue
        if suffix and suffix not in media_extensions:
            continue
        size = p115_share_item_size(item)
        for episode in episodes:
            current = best_by_episode.get(episode)
            if current is None or size > p115_share_item_size(current):
                best_by_episode[episode] = item

    selected: dict[str, dict[str, Any]] = {}
    for item in best_by_episode.values():
        selected[str(item.get("_share_id") or id(item))] = item
    selected_episodes = set(best_by_episode)
    for item in subtitles:
        parsed = parse_episode_spec(item.get("_share_name"))
        if set(parsed["episode_numbers"]) & selected_episodes:
            selected[str(item.get("_share_id") or id(item))] = item
    return list(selected.values()), selected_episodes


def transfer_completed(source: str, resource_key: str, transfer_scope: str) -> bool:
    with db() as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM resource_transfer_log "
                "WHERE source = ? AND resource_key = ? AND transfer_scope = ? "
                "AND status = 'success' LIMIT 1",
                (source, resource_key, transfer_scope),
            ).fetchone()
        )


def has_manual_transfer(tmdb_id: int, user_id: Optional[int] = None) -> bool:
    if tmdb_id <= 0:
        return False
    with db() as connection:
        query = (
            "SELECT 1 FROM resource_transfer_log "
            "WHERE tmdb_id = ? AND transfer_scope = 'manual' "
            "AND status = 'success'"
        )
        values: list[Any] = [tmdb_id]
        if user_id is not None:
            query += " AND user_id = ?"
            values.append(user_id)
        return bool(connection.execute(query + " LIMIT 1", values).fetchone())


def completed_episode_numbers(
    tmdb_id: int, season_number: int, episode_numbers: set[int]
) -> set[int]:
    if tmdb_id <= 0 or not episode_numbers:
        return set()
    placeholders = ",".join("?" for _ in episode_numbers)
    with db() as connection:
        rows = connection.execute(
            "SELECT episode_number FROM resource_transfer_log "
            "WHERE tmdb_id = ? AND season_number = ? AND status = 'success' "
            f"AND episode_number IN ({placeholders})",
            (tmdb_id, season_number, *sorted(episode_numbers)),
        ).fetchall()
    return {int(row["episode_number"]) for row in rows}


def record_transfer(
    *,
    user_id: int,
    source: str,
    resource_key: str,
    tmdb_id: int,
    transfer_scope: str,
    status: str,
    detail: str,
    follow_id: Optional[int] = None,
    season_number: int = 0,
    episode_numbers: Optional[list[int]] = None,
) -> None:
    numbers = episode_numbers or [0]
    with db() as connection:
        for episode_number in numbers:
            connection.execute(
                "INSERT OR IGNORE INTO resource_transfer_log("
                "user_id, follow_id, source, resource_key, tmdb_id, "
                "season_number, episode_number, transfer_scope, status, "
                "detail, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    follow_id,
                    source,
                    resource_key,
                    tmdb_id,
                    season_number,
                    int(episode_number),
                    transfer_scope,
                    status,
                    detail,
                    now_iso(),
                    now_iso(),
                ),
            )


def extract_dian_transfer_links(data: dict[str, Any]) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    visited: set[int] = set()

    def add_link(value: str) -> None:
        link = value.strip()
        if link and link not in seen:
            links.append(link)
            seen.add(link)

    def add(value: Any) -> None:
        if isinstance(value, dict):
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
            share_code = str(
                value.get("share_code")
                or value.get("sharecode")
                or value.get("shareCode")
                or ""
            ).strip()
            receive_code = str(
                value.get("receive_code")
                or value.get("receiveCode")
                or value.get("password")
                or ""
            ).strip()
            share_kind = str(
                value.get("share_kind")
                or value.get("share_type")
                or ""
            ).strip().lower()
            if share_code and (
                share_kind == "115"
                or "115" in share_kind
                or bool(receive_code)
            ):
                if is_115_share_url(share_code):
                    share_url = share_code
                else:
                    normalized_code = re.sub(
                        r"^(?:https?://(?:[^/]+\.)?115(?:cdn)?\.com)?/s/",
                        "",
                        share_code,
                        flags=re.I,
                    ).split("?", 1)[0].strip("/")
                    share_url = (
                        f"https://115.com/s/{quote(normalized_code, safe='')}"
                        if normalized_code
                        else ""
                    )
                if (
                    share_url
                    and receive_code
                    and "password=" not in share_url.lower()
                ):
                    separator = "&" if "?" in share_url else "?"
                    share_url += (
                        f"{separator}password={quote(receive_code, safe='')}"
                    )
                if share_url:
                    add_link(share_url)
            for nested in value.values():
                add(nested)
            return
        if isinstance(value, list):
            identity = id(value)
            if identity in visited:
                return
            visited.add(identity)
            for item in value:
                add(item)
            return
        text = str(value or "").strip()
        if text.startswith(("{", "[")):
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                add(decoded)
                return
        for line in text.splitlines():
            link = line.strip()
            if (
                link
                and link.lower().startswith(
                    ("http://", "https://", "ed2k://", "magnet:", "ftp://")
                )
            ):
                add_link(link)

    add(data)
    return links


def compact_episode_numbers(numbers: set[int]) -> str:
    ordered = sorted(number for number in numbers if number > 0)
    if not ordered:
        return ""
    ranges: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}–{previous}")
    return "、".join(ranges)


def parse_episode_spec(text: Any) -> dict[str, Any]:
    """Parse only explicit episode markers; resolutions and years are ignored."""
    value = str(text or "").strip()
    seasons: set[int] = set()
    episodes: set[int] = set()
    complete_words = bool(re.search(r"(全集|合集|全\d+\s*集|完结)", value, re.I))

    for match in re.finditer(r"全\s*(\d{1,4})\s*集", value, re.I):
        total = int(match.group(1))
        if 0 < total <= 10000:
            episodes.update(range(1, total + 1))

    for match in re.finditer(
        r"(?i)\bS(\d{1,3})\s*E(\d{1,4})"
        r"(?:\s*[-–~至]\s*(?:S(\d{1,3})\s*)?E?(\d{1,4}))?",
        value,
    ):
        start_season = int(match.group(1))
        end_season = int(match.group(3) or start_season)
        seasons.update((start_season, end_season))
        start = int(match.group(2))
        end = int(match.group(4) or start)
        if start_season == end_season and 0 < start <= end <= 10000:
            episodes.update(range(start, end + 1))
        elif 0 < start <= 10000 and 0 < end <= 10000:
            # A cross-season range cannot be expanded without knowing the
            # episode count of each season, so retain only its explicit ends.
            episodes.update((start, end))

    for match in re.finditer(
        r"(?i)\bE(\d{1,4})(?:\s*[-–~至]\s*E?(\d{1,4}))\b",
        value,
    ):
        start, end = int(match.group(1)), int(match.group(2))
        if 0 < start <= end <= 10000:
            episodes.update(range(start, end + 1))

    for match in re.finditer(
        r"(?:第\s*)?(\d{1,4})(?:\s*[-–~至]\s*(?:第\s*)?(\d{1,4}))?\s*集",
        value,
    ):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if 0 < start <= end <= 10000:
            episodes.update(range(start, end + 1))

    for match in re.finditer(r"(\d{1,4})\s*[-–~至]\s*(\d{1,4})\s*不缺集", value):
        start, end = int(match.group(1)), int(match.group(2))
        if 0 < start <= end <= 10000:
            episodes.update(range(start, end + 1))

    for match in re.finditer(r"第\s*(\d{1,3})\s*季", value):
        seasons.add(int(match.group(1)))

    episode_start = min(episodes) if episodes else 0
    episode_end = max(episodes) if episodes else 0
    is_pack = bool(
        len(episodes) > 1
        or complete_words
        or re.search(r"(不缺集|持续更新|长期更新|季全|整季)", value, re.I)
    )
    label = ""
    if episodes:
        episode_text = compact_episode_numbers(episodes)
        if len(seasons) == 1:
            label = f"第{next(iter(seasons))}季 · 第{episode_text}集"
        else:
            label = f"第{episode_text}集"
    elif complete_words:
        label = "全集/合集"
    return {
        "season_numbers": sorted(seasons),
        "episode_numbers": sorted(episodes),
        "season_number": min(seasons) if seasons else 1,
        "episode_start": episode_start,
        "episode_end": episode_end,
        "is_pack": is_pack,
        "is_complete_series": complete_words,
        "episode_label": label,
        "safe_single_episode": len(episodes) == 1 and not is_pack,
    }


def normalize_hdhive_resource(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("resource")
    if not isinstance(details, dict):
        details = item.get("details")
    if not isinstance(details, dict):
        details = {}
    title_candidates = (
        item.get("resource_title"),
        item.get("share_title"),
        item.get("remark"),
        item.get("description"),
        item.get("content"),
        details.get("title"),
        details.get("remark"),
        details.get("description"),
        item.get("title"),
    )
    title = next(
        (
            str(value).strip()
            for value in title_candidates
            if value is not None and str(value).strip()
        ),
        "影巢资源",
    )
    episode = parse_episode_spec(title)

    def joined(value: Any, fallback: str = "") -> str:
        if isinstance(value, list):
            clean = [str(entry) for entry in value if str(entry).strip()]
            return " · ".join(clean) or fallback
        return str(value or fallback)

    def first_value(*keys: str) -> Any:
        for source in (item, details):
            for key in keys:
                value = source.get(key)
                if value is not None and str(value).strip():
                    return value
        return ""

    subtitle = " · ".join(
        value
        for value in (
            joined(first_value("subtitle_language", "subtitle_lang")),
            joined(first_value("subtitle_type", "subtitles", "subtitle")),
        )
        if value
    )
    publisher = item.get("user")
    if not isinstance(publisher, dict):
        publisher = {}
    official_values = (
        item.get("is_official"),
        item.get("official"),
        item.get("is_official_group"),
        publisher.get("is_official"),
        publisher.get("official"),
        publisher.get("is_official_group"),
        publisher.get("official_group"),
    )
    publisher_labels = (
        publisher.get("role"),
        publisher.get("group"),
        publisher.get("group_name"),
        publisher.get("badge"),
        publisher.get("badges"),
        publisher.get("label"),
        publisher.get("labels"),
    )
    publisher_text = " ".join(
        (
            " ".join(str(entry) for entry in value)
            if isinstance(value, list)
            else str(value or "")
        )
        for value in publisher_labels
    ).lower()
    is_official_group = any(value is True for value in official_values) or any(
        marker in publisher_text
        for marker in ("官组", "官方", "official")
    )
    unlock_points = item.get("unlock_points")
    vip_free = is_official_group or unlock_points == 0 or bool(item.get("is_unlocked"))
    pan_type = str(item.get("pan_type") or details.get("pan_type") or "").strip()
    share_kind = str(
        item.get("share_kind")
        or item.get("share_type")
        or details.get("share_kind")
        or details.get("share_type")
        or ""
    ).strip().lower()
    offline_type = str(
        item.get("offline_type")
        or item.get("link_type")
        or details.get("offline_type")
        or details.get("link_type")
        or ""
    ).strip().lower()
    raw_link = str(
        item.get("url")
        or item.get("link")
        or details.get("url")
        or details.get("link")
        or ""
    ).strip()
    is_offline = (
        share_kind == "offline"
        or offline_type in ("ed2k", "magnet")
        or raw_link.lower().startswith(("ed2k://", "magnet:"))
        or pan_type.strip().lower() in ("ed2k", "magnet", "offline", "离线")
    )
    share_type_label = (
        "ED2K"
        if offline_type == "ed2k" or raw_link.lower().startswith("ed2k://")
        else "磁力"
        if offline_type == "magnet" or raw_link.lower().startswith("magnet:")
        else "离线"
        if is_offline
        else pan_type or "网盘"
    )
    return {
        "provider": "hdhive",
        "slug": str(item.get("slug") or ""),
        "title": title,
        "res": joined(
            first_value("video_resolution", "resolution", "quality"),
            "规格待确认",
        ),
        "codec": joined(
            first_value("video_codec", "codec", "video_encoding"),
            "编码待解锁后确认",
        ),
        "hdr": joined(
            first_value("dynamic_range", "hdr", "hdr_type"),
        ),
        "audio": joined(
            first_value("audio_info", "audio_codec", "audio"),
            "音轨待解锁后确认",
        ),
        "chn_sub": "中" in subtitle or "简" in subtitle,
        "subtitle": subtitle or "字幕信息未知",
        "size_gb": str(first_value("share_size", "size", "file_size") or "未知"),
        "source": joined(item.get("source"), "影巢"),
        "files": episode["episode_label"] or "标题未标明具体集数",
        "episode_label": episode["episode_label"],
        "episode_start": episode["episode_start"],
        "episode_end": episode["episode_end"],
        "season_number": episode["season_number"],
        "episode_numbers": episode["episode_numbers"],
        "is_pack": episode["is_pack"],
        "is_complete_series": episode["is_complete_series"],
        "safe_single_episode": episode["safe_single_episode"],
        "share_type_label": share_type_label,
        "pan_type": pan_type,
        "share_kind": share_kind,
        "offline_type": offline_type,
        "is_offline": is_offline,
        "unlock_points": unlock_points,
        "is_unlocked": bool(item.get("is_unlocked")),
        "is_official_group": is_official_group,
        "vip_free": vip_free,
        "publisher": publisher,
        "media_url": str(item.get("media_url") or ""),
        "media_slug": str(item.get("media_slug") or ""),
        "hot": "-",
    }


def hdhive_resource_is_supported(resource: dict[str, Any]) -> bool:
    """Only expose resources that 115 can receive directly or download offline."""

    pan_type = str(
        resource.get("pan_type") or resource.get("share_type_label") or ""
    ).strip().lower()
    return "115" in pan_type or bool(resource.get("is_offline"))


def normalize_supported_hdhive_resources(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resources = [
        canonical_resource(normalize_hdhive_resource(item), "hdhive")
        for item in items
    ]
    return [
        resource for resource in resources
        if hdhive_resource_is_supported(resource)
    ]


def resource_size_bytes(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number if number > 1024 * 1024 else number * 1024**3)
    text = str(value).strip().upper().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(TB|T|GB|G|MB|M|KB|K|B)?", text)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2) or "GB"
    factor = {
        "TB": 1024**4,
        "T": 1024**4,
        "GB": 1024**3,
        "G": 1024**3,
        "MB": 1024**2,
        "M": 1024**2,
        "KB": 1024,
        "K": 1024,
        "B": 1,
    }[unit]
    return int(number * factor)


def resource_size_label(value: Any) -> str:
    size_bytes = resource_size_bytes(value)
    if size_bytes <= 0:
        return "容量未知"
    if size_bytes >= 1024**4:
        amount = size_bytes / 1024**4
        unit = "TB"
    else:
        amount = size_bytes / 1024**3
        unit = "GB"
    precision = 0 if amount >= 100 else 1 if amount >= 10 else 2
    text = f"{amount:.{precision}f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def title_media_value(title: str, field: str) -> str:
    text = str(title or "")
    upper = text.upper()
    patterns: dict[str, list[tuple[str, str]]] = {
        "res": [
            (r"(?<!\d)(?:2160P|4K)(?!\d)", "4K"),
            (r"(?<!\d)1080P(?!\d)", "1080P"),
            (r"(?<!\d)720P(?!\d)", "720P"),
        ],
        "codec": [
            (r"(?:H[.\s_-]?265|X265|HEVC)", "H.265/HEVC"),
            (r"(?:H[.\s_-]?264|X264|AVC)", "H.264/AVC"),
            (r"(?:^|[.\s_-])AV1(?:$|[.\s_-])", "AV1"),
        ],
        "hdr": [
            (r"(?:DOLBY[.\s_-]*VISION|DOVI|(?:^|[.\s_-])DV(?:$|[.\s_-]))", "Dolby Vision"),
            (r"HDR10\+", "HDR10+"),
            (r"HDR10", "HDR10"),
            (r"(?:^|[.\s_-])SDR(?:$|[.\s_-])", "SDR"),
        ],
        "audio": [
            (r"(?:TRUEHD.*ATMOS|ATMOS.*TRUEHD)", "TrueHD Atmos"),
            (r"(?:DTS[.\s_-]*HD|DTSHD)", "DTS-HD"),
            (r"(?:^|[.\s_-])DTS(?:$|[.\s_-]|\d)", "DTS"),
            (r"(?:E[.\s_-]*AC3|DDP)", "E-AC-3"),
            (r"(?:^|[.\s_-])AC3(?:$|[.\s_-])", "AC-3"),
            (r"(?:^|[.\s_-])AAC(?:$|[.\s_-]|\d)", "AAC"),
        ],
    }
    for pattern, label in patterns.get(field, []):
        if re.search(pattern, upper):
            return label
    return ""


def canonical_resource(
    resource: dict[str, Any],
    provider: str,
) -> dict[str, Any]:
    item = dict(resource)
    title = str(item.get("title") or "未命名资源").strip()
    sources: dict[str, str] = {}

    def canonical_field(key: str, unknown_markers: tuple[str, ...]) -> str:
        current = str(item.get(key) or "").strip()
        if current and not any(marker in current for marker in unknown_markers):
            sources[key] = "api"
            return title_media_value(current, key) or current
        inferred = title_media_value(title, key)
        if inferred:
            sources[key] = "title"
            return inferred
        sources[key] = "unknown"
        return {
            "res": "规格未标明",
            "codec": "编码未标明",
            "hdr": "",
            "audio": "音轨未标明",
        }[key]

    item["provider"] = provider
    item["title"] = title
    item["res"] = canonical_field("res", ("未知", "待确认"))
    item["codec"] = canonical_field("codec", ("未知", "待解锁", "待确认"))
    item["hdr"] = canonical_field("hdr", ("未知", "待确认"))
    item["audio"] = canonical_field("audio", ("未知", "待解锁", "待确认"))

    subtitle = str(item.get("subtitle") or "").strip()
    subtitle_api_text = "" if "未知" in subtitle else subtitle
    subtitle_text = f"{subtitle_api_text} {title}".strip()
    subtitle_patterns = (
        ("简中", r"(?:简中|简体(?:中文)?|\bCHS\b)"),
        ("繁中", r"(?:繁中|繁体(?:中文)?|\bCHT\b)"),
        ("简韩", r"(?:简韩|简体韩文|简体中文[+／/]?韩文)"),
        ("繁韩", r"(?:繁韩|繁体韩文|繁体中文[+／/]?韩文)"),
        ("简英", r"(?:简英|简体英文|简体中文[+／/]?英文)"),
        ("繁英", r"(?:繁英|繁体英文|繁体中文[+／/]?英文)"),
        ("中英", r"(?:中英|中文[+／/]?英文)"),
        ("内封", r"(?:内封|封装字幕|软字幕)"),
        ("内嵌", r"(?:内嵌|硬字幕|硬字)"),
        ("外挂", r"(?:外挂字幕|外挂中字)"),
    )
    subtitle_details = [
        label
        for label, pattern in subtitle_patterns
        if re.search(pattern, subtitle_text, re.I)
    ]
    # HDHive titles commonly abbreviate two tracks as “简繁” or
    # “简/繁韩”; expand those spellings to the same labels used on its site.
    if re.search(r"简(?:体)?[+／/]?繁(?:体)?|简繁", subtitle_text):
        subtitle_details = ["简中", "繁中", *subtitle_details]
    if re.search(r"简[+／/]繁韩", subtitle_text):
        subtitle_details.extend(("简韩", "繁韩"))
    subtitle_detail_order = tuple(label for label, _pattern in subtitle_patterns)
    subtitle_details = [
        label for label in subtitle_detail_order if label in subtitle_details
    ]
    title_has_chinese_subtitle = bool(
        re.search(r"(中字|中文字幕|简中|繁中|简繁|简韩|繁韩|简英|繁英|CHS|CHT)", title, re.I)
    )
    has_chinese_subtitle = bool(item.get("chn_sub")) or title_has_chinese_subtitle
    if subtitle_details:
        subtitle_label = " · ".join(subtitle_details)
        sources["subtitle"] = "api" if subtitle_api_text else "title"
    elif has_chinese_subtitle:
        subtitle_label = "中文字幕"
        sources["subtitle"] = "api" if item.get("chn_sub") else "title"
    elif subtitle_api_text:
        subtitle_label = subtitle_api_text
        sources["subtitle"] = "api"
    else:
        subtitle_label = "字幕未标明"
        sources["subtitle"] = "unknown"
    item["chn_sub"] = has_chinese_subtitle
    item["subtitle_label"] = subtitle_label

    item["size_bytes"] = resource_size_bytes(item.get("size_gb"))
    item["size_label"] = resource_size_label(item.get("size_gb"))
    sources["size"] = "api" if item["size_bytes"] else "unknown"

    provided_episode_label = str(item.get("episode_label") or "").strip()
    parsed = parse_episode_spec(
        " ".join(
            str(value or "")
            for value in (item.get("episode_label"), item.get("files"), title)
        )
    )
    existing_numbers = [
        int(number)
        for number in item.get("episode_numbers") or []
        if str(number).isdigit() and int(number) > 0
    ]
    if not existing_numbers and parsed["episode_numbers"]:
        item["episode_numbers"] = parsed["episode_numbers"]
        item["episode_start"] = parsed["episode_start"]
        item["episode_end"] = parsed["episode_end"]
        item["season_number"] = parsed["season_number"]
        item["episode_label"] = provided_episode_label or parsed["episode_label"]
        item["is_pack"] = parsed["is_pack"]
        item["safe_single_episode"] = parsed["safe_single_episode"]
        sources["episodes"] = "title"
    else:
        item["episode_numbers"] = existing_numbers
        sources["episodes"] = "api" if existing_numbers else "unknown"
    item["files"] = (
        str(item.get("files") or "").strip()
        or str(item.get("episode_label") or "").strip()
        or "集数未标明"
    )
    item["field_sources"] = sources
    return item


def hdhive_resource_priority(resource: dict[str, Any]) -> tuple[int, int, int, int]:
    episode_count = len(resource.get("episode_numbers") or [])
    return (
        2
        if resource.get("is_official_group")
        else 1
        if resource.get("vip_free")
        else 0,
        resource_size_bytes(resource.get("size_gb")),
        1 if resource.get("is_pack") or episode_count > 1 else 0,
        episode_count,
    )


def hdhive_movie_resource_is_playable(resource: dict[str, Any]) -> bool:
    """Reject disc-image releases that ordinary family players cannot open."""

    text = " ".join(
        str(resource.get(key) or "")
        for key in ("title", "files", "source", "format", "container")
    ).lower()
    return not bool(
        re.search(r"(?:^|[\s._-])iso(?:$|[\s._-])|蓝光原盘|uhd原盘|bdmv", text)
    )


def hdhive_movie_resource_priority(resource: dict[str, Any]) -> tuple[int, int, int]:
    """For movies, playable files win and then the largest version is preferred."""

    return (
        1 if hdhive_movie_resource_is_playable(resource) else 0,
        resource_size_bytes(resource.get("size_gb")),
        1 if resource.get("is_official_group") or resource.get("vip_free") else 0,
    )


def hdhive_response_data(result: dict[str, Any]) -> Any:
    return result.get("data", result) if isinstance(result, dict) else result


def hdhive_subscription_target(
    share_result: dict[str, Any],
    expected_tmdb_id: int,
    expected_media_type: str = "tv",
) -> dict[str, Any]:
    data = hdhive_response_data(share_result)
    if not isinstance(data, dict):
        raise HTTPException(502, "影巢分享详情没有返回可订阅的媒体信息")
    media = data.get("media")
    if not isinstance(media, dict):
        raise HTTPException(502, "这个影巢资源没有关联可订阅的电视剧")

    # The OpenAPI contract requires an integer target_id and a matching
    # movie:{id}/tv:{id} target_key. Share details document `data.media`, but
    # deployments have returned the relationship ID at different levels.
    # Prefer the explicit subscription contract, then media-specific IDs.
    candidates: list[dict[str, Any]] = [media, data]
    for parent in (media, data):
        for key in (
            expected_media_type,
            "media_resource",
            "resource",
            "share",
        ):
            nested = parent.get(key)
            if isinstance(nested, dict) and nested not in candidates:
                candidates.append(nested)

    target_id = 0
    target_key = ""
    for candidate in candidates:
        value = str(candidate.get("target_key") or "").strip().lower()
        match = re.fullmatch(r"(movie|tv):(\d+)", value)
        if match and match.group(1) == expected_media_type:
            target_id = int(match.group(2))
            target_key = f"{expected_media_type}:{target_id}"
            break

    id_keys = (
        ("tv_id", "tvId", "series_id", "seriesId")
        if expected_media_type == "tv"
        else ("movie_id", "movieId", "film_id", "filmId")
    )
    if target_id <= 0:
        for candidate in candidates:
            for key in (*id_keys, "media_id", "mediaId", "target_id"):
                try:
                    target_id = int(candidate.get(key) or 0)
                except (TypeError, ValueError):
                    target_id = 0
                if target_id > 0:
                    break
            if target_id > 0:
                break

    # A generic media.id is valid only on the documented media object. The
    # top-level data.id is the share ID and must never be used as a target.
    if target_id <= 0:
        try:
            target_id = int(media.get("id") or 0)
        except (TypeError, ValueError):
            target_id = 0
    if target_id > 0 and not target_key:
        target_key = f"{expected_media_type}:{target_id}"

    media_type = str(
        media.get("media_type")
        or media.get("type")
        or media.get("kind")
        or expected_media_type
    ).lower()
    normalized_type = (
        "tv" if media_type in ("tv", "series", "television") else
        "movie" if media_type in ("movie", "film") else ""
    )
    if normalized_type != expected_media_type:
        raise HTTPException(400, "影巢资源类型与当前想看项目不一致")

    try:
        returned_tmdb_id = int(media.get("tmdb_id") or 0)
    except (TypeError, ValueError):
        returned_tmdb_id = 0
    if returned_tmdb_id and returned_tmdb_id != expected_tmdb_id:
        raise HTTPException(400, "影巢资源关联的影片与当前想看项目不一致")
    if target_id <= 0:
        raise HTTPException(502, "影巢分享详情缺少影片内部编号，无法创建订阅")

    return {
        "target_type": "media_resource",
        "target_id": target_id,
        "target_key": target_key,
        "title": str(
            media.get("title")
            or media.get("name")
            or data.get("title")
            or "影巢影片资源"
        ).strip(),
    }


def hdhive_subscription_target_from_page(
    page_html: str,
    expected_tmdb_id: int,
    expected_media_type: str,
    title: str = "",
) -> dict[str, Any]:
    normalized = str(page_html or "").replace('\\"', '"')
    if not re.search(
        rf'"tmdb_id"\s*:\s*"?{int(expected_tmdb_id)}"?',
        normalized,
    ):
        raise HTTPException(502, "影巢影片页面与当前 TMDB 影片不一致")

    for match in re.finditer(
        r'\{[^{}]{0,800}"target_type"\s*:\s*"media_resource"[^{}]{0,800}\}',
        normalized,
    ):
        try:
            candidate = json.loads(match.group(0))
            target_id = int(candidate.get("target_id") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        target_key = str(candidate.get("target_key") or "").strip().lower()
        key_match = re.fullmatch(r"(movie|tv):(\d+)", target_key)
        if (
            target_id > 0
            and key_match
            and key_match.group(1) == expected_media_type
            and int(key_match.group(2)) == target_id
        ):
            return {
                "target_type": "media_resource",
                "target_id": target_id,
                "target_key": target_key,
                "title": str(title or "影巢影片资源").strip(),
            }
    raise HTTPException(502, "影巢影片页面没有返回可用的原生订阅目标")


def cached_hdhive_media_target(
    media_type: str,
    tmdb_id: int,
) -> Optional[dict[str, Any]]:
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM hdhive_media_targets "
            "WHERE media_type = ? AND tmdb_id = ?",
            (media_type, tmdb_id),
        ).fetchone()
    if not row:
        return None
    return {
        "target_type": "media_resource",
        "target_id": int(row["target_id"]),
        "target_key": str(row["target_key"]),
        "title": str(row["title"] or "影巢影片资源"),
        "media_url": str(row["media_url"] or ""),
    }


def cache_hdhive_media_target(
    media_type: str,
    tmdb_id: int,
    target: dict[str, Any],
    media_url: str = "",
) -> None:
    with db() as connection:
        connection.execute(
            "INSERT INTO hdhive_media_targets("
            "media_type, tmdb_id, target_id, target_key, media_url, title, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(media_type, tmdb_id) DO UPDATE SET "
            "target_id = excluded.target_id, target_key = excluded.target_key, "
            "media_url = excluded.media_url, title = excluded.title, "
            "updated_at = excluded.updated_at",
            (
                media_type,
                tmdb_id,
                int(target["target_id"]),
                str(target["target_key"]),
                str(media_url or ""),
                str(target.get("title") or ""),
                now_iso(),
            ),
        )


def hdhive_media_page_url(
    resource: dict[str, Any],
    share_result: dict[str, Any],
    expected_media_type: str,
) -> str:
    values: list[str] = [str(resource.get("media_url") or "").strip()]
    resource_media_slug = str(resource.get("media_slug") or "").strip()
    if resource_media_slug:
        values.append(f"/{expected_media_type}/{resource_media_slug}")

    data = hdhive_response_data(share_result)
    media = data.get("media") if isinstance(data, dict) else None
    if isinstance(media, dict):
        values.extend(
            str(media.get(key) or "").strip()
            for key in ("media_url", "url", "href")
        )
        media_slug = str(
            media.get("media_slug") or media.get("slug") or ""
        ).strip()
        if media_slug:
            values.append(f"/{expected_media_type}/{media_slug}")

    for value in values:
        if not value:
            continue
        parsed = urlparse(value)
        page_path = parsed.path if parsed.scheme or parsed.netloc else value
        if re.fullmatch(
            rf"/{re.escape(expected_media_type)}/[A-Za-z0-9_-]+/?",
            page_path,
        ):
            return value
    return ""


def cached_hdhive_follow_resource(
    follow_id: int,
    slug: str,
) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute(
            "SELECT payload_json FROM tv_follow_resources "
            "WHERE follow_id = ? AND source = 'hdhive' AND resource_key = ?",
            (follow_id, slug),
        ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def hdhive_created_subscription_id(
    result: dict[str, Any], target_key: str
) -> int:
    data = hdhive_response_data(result)
    if isinstance(data, dict):
        try:
            subscription_id = int(data.get("id") or data.get("subscription_id") or 0)
        except (TypeError, ValueError):
            subscription_id = 0
        if subscription_id > 0:
            return subscription_id

    listed = hdhive_call(
        "subscriptions",
        target_type="media_resource",
        status="active",
        q=target_key,
        page=1,
        page_size=20,
    )
    for item in extract_share_items(listed):
        if str(item.get("target_key") or "") != target_key:
            continue
        try:
            subscription_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            subscription_id = 0
        if subscription_id > 0:
            return subscription_id
    raise HTTPException(502, "影巢已接受订阅，但没有返回订阅编号")


def dian_number_values(value: Any, *, allow_zero: bool = False) -> set[int]:
    """Parse Dian's CSV/range fields, including arrays returned by v2."""
    if isinstance(value, (list, tuple, set)):
        values: set[int] = set()
        for entry in value:
            values.update(dian_number_values(entry, allow_zero=allow_zero))
        return values
    if value is None or isinstance(value, dict):
        return set()
    text = str(value).strip().replace("，", ",").replace("–", "-").replace("~", "-")
    if not text:
        return set()
    values = set()
    for part in text.split(","):
        part = part.strip()
        range_match = re.fullmatch(r"(\d+)\s*(?:-|至)\s*(\d+)", part)
        if range_match:
            start, end = map(int, range_match.groups())
            if start > end:
                start, end = end, start
            if end - start <= 500:
                values.update(range(start, end + 1))
            continue
        if re.fullmatch(r"\d+", part):
            values.add(int(part))
    minimum = 0 if allow_zero else 1
    return {number for number in values if number >= minimum}


def episode_label_from_files(file_names: list[str], default_season: int) -> str:
    episodes_by_season: dict[int, set[int]] = {}
    for file_name in file_names:
        text = str(file_name or "")
        text_season = default_season
        matched_spans: list[tuple[int, int]] = []
        for match in re.finditer(
            r"(?i)S(\d{1,2})[.\s_-]*E(\d{1,3})"
            r"(?:\s*(?:-|~|–|至)\s*(?:S\d{1,2}[.\s_-]*)?E?(\d{1,3}))?",
            text,
        ):
            season = int(match.group(1))
            text_season = season
            start = int(match.group(2))
            end = int(match.group(3) or start)
            if 0 < start <= end <= 999 and end - start <= 500:
                episodes_by_season.setdefault(season, set()).update(
                    range(start, end + 1)
                )
            matched_spans.append(match.span())

        remaining = "".join(
            character
            for index, character in enumerate(text)
            if not any(start <= index < end for start, end in matched_spans)
        )
        for value in re.findall(r"(?i)(?:^|[.\s_-])E(\d{1,3})(?=[.\s_-]|$)", remaining):
            episodes_by_season.setdefault(text_season, set()).add(int(value))
        for value in re.findall(r"第\s*(\d{1,3})\s*集", remaining):
            episodes_by_season.setdefault(text_season, set()).add(int(value))

    groups = []
    for season, episodes in sorted(episodes_by_season.items()):
        episode_text = compact_episode_numbers(episodes)
        if not episode_text:
            continue
        if season >= 0:
            groups.append(f"第{season}季 · 第{episode_text}集")
        else:
            groups.append(f"第{episode_text}集")
    return "；".join(groups)


def dian_episode_label(
    pick: Any,
    title: str,
    file_names: Optional[list[str]] = None,
) -> str:
    # The public v2 list response uses ``episodes`` on share cards, while
    # create/edit payloads use ``episodes_csv``. Accept both (and array forms).
    season_values = dian_number_values(
        pick("seasons_csv", "seasons", "season_numbers", "season_list"),
        allow_zero=True,
    )
    episode_values = dian_number_values(
        pick("episodes", "episodes_csv", "episode_numbers", "episode_list")
    )
    if season_values or episode_values:
        parts = []
        if season_values:
            ordered_seasons = sorted(season_values)
            if len(season_values) == 1:
                parts.append(f"第{ordered_seasons[0]}季")
            else:
                parts.append(f"第{'、'.join(map(str, ordered_seasons))}季")
        if episode_values:
            episode_text = compact_episode_numbers(episode_values)
            parts.append(f"第{episode_text}集")
        return " · ".join(parts)

    season = pick("season_number", "season", "season_no", "season_num")
    try:
        default_season = int(season)
    except (TypeError, ValueError):
        default_season = -1
    file_label = episode_label_from_files(file_names or [], default_season)
    if file_label:
        return file_label

    explicit = pick(
        "episode_label", "episode_summary", "episodes_summary",
        "episode_range_text", "episode_text",
    )
    if explicit:
        text = str(explicit).strip()
        if "集" in text:
            return text

    episode_start = pick(
        "episode_start", "start_episode", "episode_from",
        "first_episode", "episode_number",
    )
    episode_end = pick(
        "episode_end", "end_episode", "episode_to", "last_episode",
    )
    episode_count = pick(
        "episode_count", "episodes_count", "total_episodes", "file_count",
    )

    season_match = re.search(
        r"(?i)(?:^|[.\s_-])S(\d{1,2})(?=E?\d|[.\s_-]|$)",
        title,
    )
    episode_matches = [
        int(value)
        for value in re.findall(r"(?i)E(\d{1,3})", title)
    ]
    if not season and season_match:
        season = int(season_match.group(1))
    if not episode_start and episode_matches:
        episode_start = min(episode_matches)
    if not episode_end and len(episode_matches) > 1:
        episode_end = max(episode_matches)

    def number(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    season_number = number(season)
    start_number = number(episode_start)
    end_number = number(episode_end)
    count_number = number(episode_count)
    if start_number and not end_number and count_number:
        end_number = start_number + count_number - 1

    parts = []
    if season_number:
        parts.append(f"第{season_number}季")
    if start_number and end_number > start_number:
        parts.append(f"第{start_number}–{end_number}集")
    elif start_number:
        parts.append(f"第{start_number}集")
    elif count_number:
        parts.append(f"共{count_number}集")
    return " · ".join(parts)


def dian_share_type_label(pick: Any) -> str:
    share_kind = str(pick("share_kind", "share_type") or "").strip().lower()
    offline_type = str(pick("offline_type", "link_type") or "").strip().lower()
    link = str(
        pick("url", "offline_url", "share_url", "url_115", "share_code") or ""
    ).strip().lower()
    is_offline = share_kind == "offline" or bool(offline_type) or link.startswith(
        ("ed2k://", "magnet:")
    )
    if is_offline:
        if offline_type == "ed2k" or link.startswith("ed2k://"):
            return "ED2K"
        if offline_type == "magnet" or link.startswith("magnet:"):
            return "磁力"
        return offline_type.upper() if offline_type else "离线"
    if (
        share_kind == "115"
        or pick("url_115", "receive_code")
        or "115.com/" in link
        or "115cdn.com/" in link
    ):
        return "115"
    return ""


def normalize_dian_resource(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten Dian's share wrapper into the fields used by the member UI."""
    nested_resource = item.get("resource")
    resource = nested_resource if isinstance(nested_resource, dict) else {}
    nested_share = item.get("share")
    share = nested_share if isinstance(nested_share, dict) else {}

    dictionaries: list[dict[str, Any]] = [item, resource, share]
    seen = {id(value) for value in dictionaries}
    position = 0
    while position < len(dictionaries):
        source = dictionaries[position]
        position += 1
        for value in source.values():
            nested_values = value if isinstance(value, list) else [value]
            for nested in nested_values:
                if isinstance(nested, dict) and id(nested) not in seen:
                    dictionaries.append(nested)
                    seen.add(id(nested))

    def pick(*keys: str, sources: Optional[list[dict[str, Any]]] = None) -> Any:
        for source in sources or dictionaries:
            for key in keys:
                value = source.get(key)
                if value is not None and value != "":
                    return value
        return None

    tag_values: list[str] = []
    for source in dictionaries:
        tags = source.get("tags", source.get("tag"))
        if isinstance(tags, dict):
            if id(tags) not in seen:
                dictionaries.append(tags)
                seen.add(id(tags))
        elif isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict):
                    if id(tag) not in seen:
                        dictionaries.append(tag)
                        seen.add(id(tag))
                elif tag:
                    tag_values.append(str(tag))
        elif tags:
            tag_values.append(str(tags))
    tag_text = " ".join(tag_values)

    raw_size = pick("size_gb", "total_size_gb")
    if raw_size is None:
        raw_size = pick(
            "size", "total_size", "size_bytes", "total_size_bytes",
            "file_size", "bytes",
        )
        if isinstance(raw_size, (int, float)) and raw_size > 1024 * 1024:
            raw_size = round(raw_size / 1024 / 1024 / 1024, 1)

    subtitle_value = pick(
        "chn_sub", "chinese_subtitle", "has_chinese_subtitle",
        "subtitle_chinese", "is_chinese_subtitle",
    )
    subtitle_detail = pick(
        "subtitle_info", "subtitle_language", "subtitle_lang",
        "subtitle_type", "subtitles", "subtitle",
    )
    if isinstance(subtitle_value, str):
        has_chinese_subtitle = subtitle_value.strip().lower() in {
            "1", "true", "yes", "y", "是", "有",
        }
    else:
        has_chinese_subtitle = bool(subtitle_value)
    if not has_chinese_subtitle and tag_text:
        has_chinese_subtitle = any(
            marker in tag_text.lower()
            for marker in ("中字", "中文字幕", "简中", "繁中", "chinese sub")
        )
    if not subtitle_detail and any(
        marker in tag_text
        for marker in ("中字", "中文字幕", "简中", "繁中", "简韩", "繁韩", "内封", "内嵌")
    ):
        subtitle_detail = tag_text

    # Share IDs live on the outer wrapper while media details commonly live
    # inside ``resource``. Prefer the inner resource's descriptive title.
    title = (
        pick(
            "title", "name", "display_name", "resource_name",
            sources=[resource],
        )
        or pick("title", "name", "display_name", "resource_name")
        or ""
    )
    files = pick(
        "files", "file_summary", "episode_summary", "file_count",
        "episode_count",
    )
    raw_file_list = pick("file_list", "files_list", "filenames", "file_names")
    if raw_file_list is None and isinstance(files, (list, dict)):
        raw_file_list = files
    file_names: list[str] = []
    if isinstance(raw_file_list, str):
        try:
            decoded_file_list = json.loads(raw_file_list)
        except (TypeError, ValueError):
            decoded_file_list = None
        if isinstance(decoded_file_list, list):
            raw_file_list = decoded_file_list
        else:
            file_names.extend(
                line.strip()
                for line in raw_file_list.splitlines()
                if line.strip()
            )
    def collect_file_names(value: Any) -> None:
        if isinstance(value, dict):
            name = (
                value.get("name")
                or value.get("file_name")
                or value.get("filename")
                or value.get("title")
            )
            if name:
                file_names.append(str(name))
            for key, nested in value.items():
                if key not in {"name", "file_name", "filename", "title"}:
                    collect_file_names(nested)
        elif isinstance(value, (list, tuple)):
            for entry in value:
                collect_file_names(entry)
        elif value:
            file_names.append(str(value))

    if isinstance(raw_file_list, (list, dict)):
        collect_file_names(raw_file_list)
    episode_title = (
        pick("file_name", "filename")
        or pick("offline_title", "title_override")
        or title
    )
    episode_label = dian_episode_label(pick, str(episode_title), file_names)
    if not episode_label and isinstance(files, str) and "集" in files:
        episode_label = files.strip()
    if not episode_label and isinstance(files, (int, float)):
        episode_label = dian_episode_label(
            lambda *keys, **_kwargs: files if "episode_count" in keys else None,
            str(title),
        )

    normalized = dict(item)
    normalized.update(
        {
            "share_id": pick("share_id", sources=[item, share])
            or pick("id", sources=[share, item]),
            "resource_id": pick("resource_id", sources=[item, resource])
            or pick("id", sources=[resource, item]),
            "title": title,
            "res": pick(
                "res", "resolution", "quality", "definition",
                "video_resolution", "video_quality",
            ),
            "codec": pick(
                "codec", "video_codec", "vcodec", "encode", "encoding",
            ),
            "hdr": pick("hdr", "video_hdr", "dynamic_range"),
            "audio": pick(
                "audio", "audio_info", "audio_codec", "audio_track",
                "soundtrack",
            ),
            "chn_sub": has_chinese_subtitle,
            "subtitle": subtitle_detail,
            "size_gb": raw_size,
            "source": pick("source", "source_tag", "origin", "channel"),
            "files": files,
            "episode_label": episode_label,
            "share_type_label": dian_share_type_label(pick),
            "hot": pick(
                "hot", "heat", "hotness", "score", "views", "view_count",
            ),
            "dian_share_code": pick(
                "share_code", "sharecode", "shareCode",
                "unlock_code", "unlockCode",
            ),
        }
    )
    return normalized


def dian_resource_is_supported(resource: dict[str, Any]) -> bool:
    """Only expose 115 shares and links that 115 can download offline."""

    share_type = str(resource.get("share_type_label") or "").strip().lower()
    return "115" in share_type or share_type in {"ed2k", "磁力", "离线"}


def normalize_supported_dian_resources(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resources = [
        canonical_resource(normalize_dian_resource(item), "dian")
        for item in items
    ]
    return [
        resource for resource in resources
        if dian_resource_is_supported(resource)
    ]


def session_user(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as connection:
        row = connection.execute(
            "SELECT u.id, u.username, u.display_name, u.role, u.active, "
            "u.storage_destination "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash, now_iso()),
        ).fetchone()
    return dict(row) if row and row["active"] else None


def require_user(movie_session: Optional[str]) -> dict[str, Any]:
    user = session_user(movie_session)
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def require_admin(movie_session: Optional[str]) -> dict[str, Any]:
    user = require_user(movie_session)
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def serialize_request(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["poster_url"] = tmdb_image_proxy_url(item.get("poster_path"))
    item["status_name"] = STATUS_NAMES.get(item["status"], item["status"])
    return item


def emby_api_url(base_url: str, path: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.lower().endswith("/emby"):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/emby/{path.lstrip('/')}"


def storage_destination(value: Any) -> str:
    return "p123" if str(value or "") == "p123" else "p115"


def emby_credentials(destination: str = "p115") -> tuple[str, str]:
    destination = storage_destination(destination)
    keys = (
        ("p123_emby_url", "p123_emby_api_key")
        if destination == "p123"
        else ("emby_url", "emby_api_key")
    )
    values = cached_settings(*keys)
    return values[keys[0]], values[keys[1]]


def destination_emby_ids(
    destination: str,
    *,
    force: bool = False,
    prefer_cached: bool = False,
) -> set[int]:
    if storage_destination(destination) == "p123":
        return emby_library_tmdb_ids(
            force=force,
            prefer_cached=prefer_cached,
            destination="p123",
        )
    if force:
        return emby_library_tmdb_ids(force=True)
    if prefer_cached:
        return emby_library_tmdb_ids(prefer_cached=True)
    return emby_library_tmdb_ids()


def destination_episode_progress(
    destination: str,
    tmdb_id: int,
    *,
    known_in_library: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    if storage_destination(destination) == "p123":
        return emby_series_episode_progress(
            tmdb_id,
            known_in_library=known_in_library,
            force=force,
            destination="p123",
        )
    return emby_series_episode_progress(
        tmdb_id,
        known_in_library=known_in_library,
        force=force,
    )


def episode_progress_label(
    season_number: int,
    episode_number: int,
    prefix: str,
) -> str:
    if episode_number <= 0:
        return ""
    if season_number > 1:
        return f"{prefix}第{season_number}季第{episode_number}集"
    return f"{prefix}第{episode_number}集"


def emby_series_episode_progress(
    tmdb_id: int,
    known_in_library: bool = False,
    force: bool = False,
    destination: str = "p115",
) -> dict[str, Any]:
    base_url, api_key = emby_credentials(destination)
    if not base_url or not api_key or tmdb_id <= 0:
        return {}

    cache_key = hashlib.sha256(
        f"{base_url}|{api_key}|tv|{tmdb_id}".encode()
    ).hexdigest()
    now = time.monotonic()
    with CACHE_LOCK:
        cached = EMBY_EPISODE_CACHE.get(cache_key)
        if not force and cached and cached[0] > now:
            return dict(cached[1])

    headers = {"X-Emby-Token": api_key, "Accept": "application/json"}
    series_params: dict[str, Any] = {
        "Recursive": "true",
        "IncludeItemTypes": "Series",
        "Fields": "ProviderIds",
        "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
        "Limit": 10,
    }
    try:
        series_response = requests.get(
            emby_api_url(base_url, "/Items"),
            headers=headers,
            params=series_params,
            timeout=20,
        )
        series_response.raise_for_status()
        series_items = series_response.json().get("Items", [])
        if not series_items and known_in_library:
            series_params.pop("AnyProviderIdEquals", None)
            series_params["Limit"] = 10000
            series_response = requests.get(
                emby_api_url(base_url, "/Items"),
                headers=headers,
                params=series_params,
                timeout=20,
            )
            series_response.raise_for_status()
            series_items = series_response.json().get("Items", [])

        series_id = ""
        for item in series_items:
            provider_ids = item.get("ProviderIds") or {}
            raw_id = provider_ids.get("Tmdb") or provider_ids.get("TMDB")
            if str(raw_id or "") == str(tmdb_id):
                series_id = str(item.get("Id") or item.get("id") or "")
                break
        if not series_id:
            with CACHE_LOCK:
                EMBY_EPISODE_CACHE.pop(cache_key, None)
            return {}

        episodes_response = requests.get(
            emby_api_url(base_url, f"/Shows/{quote(series_id, safe='')}/Episodes"),
            headers=headers,
            params={
                "Fields": "ParentIndexNumber,IndexNumber,IsMissing,LocationType",
                "IsMissing": "false",
                "StartIndex": 0,
                "Limit": 10000,
            },
            timeout=20,
        )
        episodes_response.raise_for_status()
        latest_season = 0
        latest_episode = 0
        episode_numbers_by_season: dict[int, set[int]] = {}
        for episode in episodes_response.json().get("Items", []):
            if episode.get("IsMissing") is True or episode.get("LocationType") == "Virtual":
                continue
            season_number = int(episode.get("ParentIndexNumber") or 0)
            episode_number = int(episode.get("IndexNumber") or 0)
            if season_number > 0 and episode_number > 0:
                episode_numbers_by_season.setdefault(season_number, set()).add(
                    episode_number
                )
            if episode_number > 0 and (season_number, episode_number) > (
                latest_season,
                latest_episode,
            ):
                latest_season, latest_episode = season_number, episode_number
        if latest_episode <= 0:
            with CACHE_LOCK:
                EMBY_EPISODE_CACHE.pop(cache_key, None)
            return {}
        result = {
            "emby_latest_season_number": latest_season,
            "emby_latest_episode_number": latest_episode,
            "emby_episode_label": episode_progress_label(
                latest_season,
                latest_episode,
                "已入库至",
            ),
            "emby_episode_numbers": {
                str(season): sorted(numbers)
                for season, numbers in sorted(episode_numbers_by_season.items())
            },
        }
        with CACHE_LOCK:
            EMBY_EPISODE_CACHE[cache_key] = (now + 300, dict(result))
        return result
    except (requests.RequestException, ValueError, TypeError):
        return {}


def emby_library_tmdb_ids(
    force: bool = False,
    prefer_cached: bool = False,
    destination: str = "p115",
) -> set[int]:
    destination = storage_destination(destination)
    base_url, api_key = emby_credentials(destination)
    if not base_url or not api_key:
        return set()
    library_cache = EMBY_LIBRARY_CACHES[destination]
    cache_key = hashlib.sha256(f"{base_url}|{api_key}".encode()).hexdigest()
    now = time.monotonic()
    with CACHE_LOCK:
        if (
            not force
            and library_cache["key"] == cache_key
            and library_cache["expires"] > now
        ):
            return set(library_cache["ids"])
        if not force and prefer_cached:
            stale_ids = (
                set(library_cache["ids"])
                if library_cache["key"] == cache_key
                else set()
            )
            should_refresh = not library_cache["refreshing"]
            if should_refresh:
                library_cache["refreshing"] = True
            if should_refresh:
                Thread(
                    target=refresh_emby_library_cache,
                    args=(destination,),
                    name="emby-cache-refresh",
                    daemon=True,
                ).start()
            return stale_ids
    try:
        response = requests.get(
            emby_api_url(base_url, "/Items"),
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "ProviderIds",
                "Limit": 10000,
            },
            timeout=20,
        )
        response.raise_for_status()
        values: set[int] = set()
        for item in response.json().get("Items", []):
            provider_ids = item.get("ProviderIds") or {}
            raw_id = provider_ids.get("Tmdb") or provider_ids.get("TMDB")
            if str(raw_id or "").isdigit():
                values.add(int(raw_id))
        with CACHE_LOCK:
            library_cache.update(
                {"key": cache_key, "expires": now + 300, "ids": set(values)}
            )
        return values
    except (requests.RequestException, ValueError, TypeError):
        with CACHE_LOCK:
            if library_cache["key"] == cache_key:
                return set(library_cache["ids"])
        return set()


def refresh_emby_library_cache(destination: str = "p115") -> None:
    try:
        emby_library_tmdb_ids(force=True, destination=destination)
    finally:
        with CACHE_LOCK:
            EMBY_LIBRARY_CACHES[storage_destination(destination)]["refreshing"] = False


def sync_emby_requests(
    force: bool = False,
    destination: Optional[str] = None,
) -> int:
    destinations = [storage_destination(destination)] if destination else ["p115", "p123"]
    removed = 0
    for current in destinations:
        tmdb_ids = emby_library_tmdb_ids(force=force, destination=current)
        if not tmdb_ids:
            continue
        placeholders = ",".join("?" for _ in tmdb_ids)
        with db() as connection:
            rows = connection.execute(
                f"SELECT r.id, r.title, r.year, u.display_name "
                f"FROM movie_requests r JOIN users u ON u.id = r.user_id "
                f"WHERE r.tmdb_id IN ({placeholders}) "
                f"AND u.storage_destination = ?",
                (*tmdb_ids, current),
            ).fetchall()
            if rows:
                ids = [int(row["id"]) for row in rows]
                marks = ",".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM movie_requests WHERE id IN ({marks})",
                    ids,
                )
        for row in rows:
            send_notifications(
                f"✅ 已入库并清除求片 · {'123' if current == 'p123' else '115'} Emby\n\n"
                f"{row['title']} ({row['year']})\n申请人：{row['display_name']}"
            )
        removed += len(rows)
    return removed


def normalize_search_text(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def cache_tmdb_search_catalog(items: list[dict[str, Any]]) -> None:
    rows = []
    timestamp = time.time()
    for item in items:
        media_type = str(item.get("media_type") or "")
        try:
            tmdb_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            tmdb_id = 0
        if media_type not in ("movie", "tv") or tmdb_id <= 0:
            continue
        titles = (
            item.get("title"),
            item.get("name"),
            item.get("original_title"),
            item.get("original_name"),
        )
        search_text = " ".join(
            value for value in (normalize_search_text(title) for title in titles) if value
        )
        if not search_text:
            continue
        try:
            popularity = float(item.get("popularity") or 0)
        except (TypeError, ValueError):
            popularity = 0
        rows.append(
            (
                media_type,
                tmdb_id,
                search_text,
                popularity,
                json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                timestamp,
            )
        )
    if not rows:
        return
    with db() as connection:
        connection.executemany(
            "INSERT INTO tmdb_search_catalog("
            "media_type, tmdb_id, search_text, popularity, payload_json, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(media_type, tmdb_id) DO UPDATE SET "
            "search_text = excluded.search_text, popularity = excluded.popularity, "
            "payload_json = excluded.payload_json, updated_at = excluded.updated_at",
            rows,
        )
        connection.execute(
            "DELETE FROM tmdb_search_catalog WHERE updated_at < ?",
            (timestamp - 365 * 86400,),
        )


def local_tmdb_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    normalized = normalize_search_text(query)
    if not normalized:
        return []
    with db() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM tmdb_search_catalog "
            "WHERE search_text LIKE ? "
            "ORDER BY popularity DESC, updated_at DESC LIMIT ?",
            (f"%{normalized}%", limit),
        ).fetchall()
    results = []
    for row in rows:
        try:
            item = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            results.append(item)
    return results


def search_db_context(
    user_id: int,
    query: str,
) -> tuple[list[dict[str, Any]], set[tuple[str, int]], set[int]]:
    normalized = normalize_search_text(query)
    with db() as connection:
        catalog_rows = (
            connection.execute(
                "SELECT payload_json FROM tmdb_search_catalog "
                "WHERE search_text LIKE ? "
                "ORDER BY popularity DESC, updated_at DESC LIMIT 20",
                (f"%{normalized}%",),
            ).fetchall()
            if normalized
            else []
        )
        followed_items = {
            (str(row[0] or "tv"), int(row[1]))
            for row in connection.execute(
                "SELECT media_type, tmdb_id FROM tv_follows "
                "WHERE user_id = ? AND active = 1",
                (user_id,),
            ).fetchall()
        }
        transferred_items = {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT tmdb_id FROM resource_transfer_log "
                "WHERE transfer_scope = 'manual' "
                "AND status = 'success' AND tmdb_id > 0",
            ).fetchall()
        }
    catalog_items = []
    for row in catalog_rows:
        try:
            item = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            catalog_items.append(item)
    return catalog_items, followed_items, transferred_items


def schedule_tmdb_refresh(
    path: str,
    params: dict[str, Any],
    timeout: Any,
    cache_key: str,
) -> None:
    with CACHE_LOCK:
        if cache_key in TMDB_REFRESHING:
            return
        TMDB_REFRESHING.add(cache_key)

    def worker() -> None:
        try:
            data = tmdb_get(path, params, timeout=timeout, force_refresh=True)
            if path == "/search/multi":
                cache_tmdb_search_catalog(
                    [
                        item for item in data.get("results", [])
                        if item.get("media_type") in ("movie", "tv")
                    ]
                )
        finally:
            with CACHE_LOCK:
                TMDB_REFRESHING.discard(cache_key)

    Thread(
        target=worker,
        name="tmdb-cache-refresh",
        daemon=True,
    ).start()


def tmdb_get(
    path: str,
    params: dict[str, Any],
    timeout: Any = 15,
    force_refresh: bool = False,
) -> dict[str, Any]:
    credential = cached_setting("tmdb_token")
    if not credential:
        raise HTTPException(503, "管理员还没有配置 TMDB 凭证")
    original_params = dict(params)
    params = dict(original_params)
    headers = {"Accept": "application/json"}
    if credential.startswith("eyJ") or len(credential) > 80:
        headers["Authorization"] = f"Bearer {credential}"
    else:
        params["api_key"] = credential
    cache_params = {key: value for key, value in params.items() if key != "api_key"}
    cache_key = hashlib.sha256(
        json.dumps(
            [path, cache_params, hashlib.sha256(credential.encode()).hexdigest()],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    now = time.monotonic()
    stale = None
    with CACHE_LOCK:
        cached = TMDB_RESPONSE_CACHE.get(cache_key)
        if cached and cached[1] <= now:
            TMDB_RESPONSE_CACHE.pop(cache_key, None)
            cached = None
        if cached:
            fresh_until, _, cached_data = cached
            stale = cached_data
            if not force_refresh and fresh_until > now:
                return cached_data
    if cached is None:
        with db() as connection:
            disk_cache = connection.execute(
                "SELECT payload_json, expires_at, updated_at FROM tmdb_cache "
                "WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if disk_cache:
            try:
                disk_data = json.loads(disk_cache["payload_json"])
            except (TypeError, ValueError):
                disk_data = None
            if isinstance(disk_data, dict):
                remaining = float(disk_cache["expires_at"]) - time.time()
                stale_remaining = (
                    float(disk_cache["updated_at"]) + TMDB_STALE_SECONDS - time.time()
                )
                if stale_remaining > 0:
                    stale = disk_data
                    with CACHE_LOCK:
                        TMDB_RESPONSE_CACHE[cache_key] = (
                            time.monotonic() + max(0, remaining),
                            time.monotonic() + stale_remaining,
                            disk_data,
                        )
                    if not force_refresh and remaining > 0:
                        return disk_data
    if stale is not None and not force_refresh:
        schedule_tmdb_refresh(path, original_params, timeout, cache_key)
        return stale
    try:
        response = TMDB_HTTP.get(
            f"https://api.themoviedb.org/3{path}",
            params=params,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        cache_seconds = (
            TMDB_SEARCH_FRESH_SECONDS
            if path == "/search/multi"
            else 3600
            if "append_to_response" in cache_params
            else 300
        )
        with CACHE_LOCK:
            if len(TMDB_RESPONSE_CACHE) >= 512:
                expired = [
                    key
                    for key, value in TMDB_RESPONSE_CACHE.items()
                    if value[1] <= now
                ]
                for key in expired:
                    TMDB_RESPONSE_CACHE.pop(key, None)
                if len(TMDB_RESPONSE_CACHE) >= 512:
                    TMDB_RESPONSE_CACHE.pop(next(iter(TMDB_RESPONSE_CACHE)))
            TMDB_RESPONSE_CACHE[cache_key] = (
                now + cache_seconds,
                now + TMDB_STALE_SECONDS,
                data,
            )
        timestamp = time.time()
        with db() as connection:
            connection.execute(
                "INSERT INTO tmdb_cache(cache_key, payload_json, expires_at, updated_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "payload_json = excluded.payload_json, "
                "expires_at = excluded.expires_at, updated_at = excluded.updated_at",
                (
                    cache_key,
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    timestamp + cache_seconds,
                    timestamp,
                ),
            )
            connection.execute(
                "DELETE FROM tmdb_cache WHERE updated_at < ?",
                (timestamp - 7 * 86400,),
            )
        return data
    except requests.RequestException as error:
        if stale is not None:
            return stale
        code = getattr(error.response, "status_code", "")
        if code in (401, 403):
            raise HTTPException(502, "TMDB 凭证无效，请管理员检查设置") from error
        raise HTTPException(502, "暂时无法连接 TMDB，请稍后重试") from error


def tmdb_media_item(
    item: dict[str, Any], media_type: str, library_ids: set[int]
) -> dict[str, Any]:
    date = item.get("release_date") or item.get("first_air_date") or ""
    title = item.get("title") or item.get("name") or "未命名"
    original = item.get("original_title") or item.get("original_name") or ""
    poster_path = item.get("poster_path") or ""
    tmdb_id = int(item.get("id") or 0)
    result = {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "original_title": original,
        "year": str(date)[:4],
        "overview": item.get("overview") or "暂无简介",
        "poster_path": poster_path,
        "poster_url": tmdb_image_proxy_url(poster_path),
        "rating": round(float(item.get("vote_average") or 0), 1),
        "vote_count": int(item.get("vote_count") or 0),
        "in_library": tmdb_id in library_ids,
    }
    if media_type == "tv":
        last_episode = item.get("last_episode_to_air") or {}
        last_season_number = int(last_episode.get("season_number") or 0)
        last_episode_number = int(last_episode.get("episode_number") or 0)
        next_episode = item.get("next_episode_to_air") or {}
        next_season_number = int(next_episode.get("season_number") or 0)
        next_episode_number = int(next_episode.get("episode_number") or 0)
        result.update(
            {
                "last_aired_season_number": last_season_number,
                "last_aired_episode_number": last_episode_number,
                "latest_episode_label": episode_progress_label(
                    last_season_number,
                    last_episode_number,
                    "更新至",
                ),
                "next_episode_season_number": next_season_number,
                "next_episode_number": next_episode_number,
                "next_episode_air_date": str(next_episode.get("air_date") or ""),
                "next_episode_label": episode_progress_label(
                    next_season_number,
                    next_episode_number,
                    "下一集为",
                ),
            }
        )
        raw_status = str(item.get("status") or "").strip()
        if raw_status == "Ended":
            result.update({"series_status": "ended", "series_status_label": "全剧已完结"})
        elif raw_status == "Canceled":
            result.update({"series_status": "canceled", "series_status_label": "已取消"})
        elif raw_status:
            season_number = last_season_number
            season_episode_count = 0
            for season in item.get("seasons") or []:
                if int(season.get("season_number") or 0) == season_number:
                    season_episode_count = int(season.get("episode_count") or 0)
                    break
            next_is_same_season = (
                season_number > 0
                and next_season_number == season_number
            )
            if (
                season_number > 0
                and season_episode_count > 0
                and last_episode_number >= season_episode_count
                and not next_is_same_season
            ):
                result.update(
                    {
                        "series_status": "season_ended",
                        "series_status_label": f"第{season_number}季已完结",
                    }
                )
            else:
                label = f"第{season_number}季未完结" if season_number > 0 else "未完结"
                result.update(
                    {"series_status": "ongoing", "series_status_label": label}
                )
    return result


def enrich_tmdb_tv_statuses(items: list[dict[str, Any]]) -> None:
    tv_items = [
        item for item in items
        if item.get("media_type") == "tv" and int(item.get("id") or 0) > 0
    ]
    if not tv_items:
        return

    def fetch(item: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        tmdb_id = int(item.get("id") or 0)
        try:
            detail = tmdb_get(f"/tv/{tmdb_id}", {"language": "zh-CN"})
            return tmdb_id, {
                "status": detail.get("status"),
                "last_episode_to_air": detail.get("last_episode_to_air"),
                "next_episode_to_air": detail.get("next_episode_to_air"),
                "seasons": detail.get("seasons") or [],
            }
        except HTTPException:
            return tmdb_id, {}

    statuses: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(6, len(tv_items))) as executor:
        futures = [executor.submit(fetch, item) for item in tv_items]
        for future in as_completed(futures):
            tmdb_id, status_fields = future.result()
            if status_fields.get("status"):
                statuses[tmdb_id] = status_fields
    for item in tv_items:
        status_fields = statuses.get(int(item.get("id") or 0))
        if status_fields:
            item.update(status_fields)


def douban_get(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Read public data used by Douban's own mobile web pages."""
    try:
        response = requests.get(
            f"https://m.douban.com/rexxar/api/v2{path}",
            params=params or {},
            headers={
                "Accept": "application/json",
                "Referer": "https://m.douban.com/",
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Mobile Safari/537.36",
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise HTTPException(502, "豆瓣榜单暂时无法连接，请稍后重试") from error


def douban_media_item(item: dict[str, Any], media_type: str) -> dict[str, Any]:
    cover = item.get("cover") or {}
    pic = item.get("pic") or {}
    poster_url = cover.get("url") or item.get("cover_url") or pic.get("large") or ""
    rating = item.get("rating") or {}
    return {
        "source": "douban",
        "douban_id": str(item.get("id") or ""),
        "tmdb_id": 0,
        "media_type": media_type,
        "title": item.get("title") or "未命名",
        "original_title": item.get("original_title") or "",
        "year": str(item.get("year") or "")[:4],
        "overview": item.get("intro") or item.get("card_subtitle") or item.get("info") or "暂无简介",
        "poster_path": "",
        "poster_url": f"/api/douban/poster?url={quote(poster_url, safe='')}" if poster_url else "",
        "rating": round(float(rating.get("value") or 0), 1),
        "vote_count": int(rating.get("count") or 0),
        "in_library": False,
    }


def telegram_request(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db() as connection:
        token = setting(connection, "telegram_token")
        proxy_url = setting(connection, "telegram_proxy")
    if not token:
        return {}
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            proxies=proxies,
            timeout=30 if method == "getUpdates" else 12,
        )
        response.raise_for_status()
        data = response.json()
        return data if data.get("ok") else {}
    except requests.RequestException:
        return {}


def configure_telegram_menu() -> None:
    telegram_request(
        "setMyCommands",
        {
            "commands": [
                {"command": "requests", "description": "求片需求"},
                {"command": "completed", "description": "完成情况"},
                {"command": "hdhive", "description": "影巢账号"},
                {"command": "dian", "description": "癫影账号"},
                {"command": "notice", "description": "发布片库公告"},
                {"command": "clear_notice", "description": "清除片库公告"},
                {"command": "menu", "description": "显示菜单"},
            ]
        },
    )


def send_telegram(text: str) -> None:
    with db() as connection:
        chat_id = setting(connection, "telegram_chat_id")
    if not chat_id:
        return
    # Remove the old persistent reply keyboard. Bot commands remain available
    # through Telegram's native blue menu button configured by setMyCommands.
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"remove_keyboard": True},
    }
    telegram_request("sendMessage", payload)


def wecom_api_url(base_url: str, path: str) -> str:
    return f"{base_url.strip().rstrip('/')}/{path.lstrip('/')}"


def wecom_access_token(force: bool = False) -> str:
    with db() as connection:
        corp_id = setting(connection, "wecom_corp_id")
        secret = setting(connection, "wecom_secret")
        api_base = setting(connection, "wecom_api_base") or "https://wx.weige1999.xin"
    if not corp_id or not secret:
        return ""
    cache_key = hashlib.sha256(f"{api_base}|{corp_id}|{secret}".encode()).hexdigest()
    now = time.monotonic()
    with WECOM_TOKEN_LOCK:
        if (
            not force
            and WECOM_TOKEN_CACHE["key"] == cache_key
            and WECOM_TOKEN_CACHE["expires"] > now
        ):
            return str(WECOM_TOKEN_CACHE["token"])
    try:
        response = requests.get(
            wecom_api_url(api_base, "/cgi-bin/gettoken"),
            params={"corpid": corp_id, "corpsecret": secret},
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return ""
    token = str(data.get("access_token") or "")
    if not token:
        return ""
    expires_in = max(60, int(data.get("expires_in") or 7200))
    with WECOM_TOKEN_LOCK:
        WECOM_TOKEN_CACHE.update(
            {"key": cache_key, "token": token, "expires": now + expires_in - 120}
        )
    return token


def wecom_request(
    path: str,
    payload: dict[str, Any],
    *,
    retry: bool = True,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    token = wecom_access_token()
    if not token:
        return {}
    with db() as connection:
        api_base = setting(connection, "wecom_api_base") or "https://wx.weige1999.xin"
    try:
        response = requests.post(
            wecom_api_url(api_base, path),
            params={"access_token": token, **(params or {})},
            json=payload,
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return {}
    if retry and int(data.get("errcode") or 0) in (40014, 42001):
        wecom_access_token(force=True)
        return wecom_request(path, payload, retry=False, params=params)
    return data


def send_wecom(text: str, to_user: str = "") -> bool:
    with db() as connection:
        agent_id = setting(connection, "wecom_agent_id")
        recipient = to_user or setting(connection, "wecom_to_user") or "@all"
    if not agent_id:
        return False
    result = wecom_request(
        "/cgi-bin/message/send",
        {
            "touser": recipient,
            "msgtype": "text",
            "agentid": int(agent_id),
            "text": {"content": str(text)[:2048]},
            "safe": 0,
        },
    )
    return int(result.get("errcode", -1)) == 0


def send_notifications(text: str) -> None:
    send_telegram(text)
    send_wecom(text)


def send_notifications_async(text: str) -> None:
    """Send optional notifications without extending a completed web request."""
    def deliver() -> None:
        try:
            send_notifications(text)
        except Exception:
            pass

    Thread(target=deliver, name="movie-request-notification", daemon=True).start()


def wecom_menu_payload() -> dict[str, Any]:
    with db() as connection:
        site_url = setting(connection, "site_public_url") or "https://qp.weige1999.xin"
    return {
        "button": [
            {
                "name": "求片",
                "sub_button": [
                    {"type": "click", "name": "求片需求", "key": "requests"},
                    {"type": "click", "name": "完成情况", "key": "completed"},
                ],
            },
            {
                "name": "管理",
                "sub_button": [
                    {"type": "click", "name": "发布公告", "key": "notice"},
                    {"type": "click", "name": "清除公告", "key": "clear_notice"},
                ],
            },
            {
                "name": "账号",
                "sub_button": [
                    {"type": "click", "name": "影巢账号", "key": "hdhive"},
                    {"type": "click", "name": "癫影账号", "key": "dian"},
                    {"type": "view", "name": "打开映单", "url": site_url},
                ],
            },
        ]
    }


def configure_wecom_menu() -> bool:
    with db() as connection:
        agent_id = setting(connection, "wecom_agent_id")
    if not agent_id:
        return False
    result = wecom_request(
        "/cgi-bin/menu/create",
        wecom_menu_payload(),
        params={"agentid": int(agent_id)},
    )
    return int(result.get("errcode", -1)) == 0


def telegram_request_summary(completed: bool) -> str:
    with db() as connection:
        if completed:
            rows = connection.execute(
                "SELECT r.title, r.year, u.display_name FROM movie_requests r "
                "JOIN users u ON u.id = r.user_id WHERE r.status = 'available' "
                "ORDER BY r.updated_at DESC LIMIT 20"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT r.title, r.year, r.status, u.display_name FROM movie_requests r "
                "JOIN users u ON u.id = r.user_id "
                "WHERE r.status NOT IN ('available', 'rejected') "
                "ORDER BY r.created_at DESC LIMIT 20"
            ).fetchall()
    if not rows:
        return "✅ 暂无记录" if completed else "📭 暂无待处理的求片需求"
    if completed:
        lines = ["✅ 已入库 / 完成情况", ""]
        lines.extend(f"• {row['title']} ({row['year']}) · {row['display_name']}" for row in rows)
    else:
        lines = ["🎬 求片需求", ""]
        lines.extend(
            f"• {row['title']} ({row['year']}) · {row['display_name']} · {STATUS_NAMES.get(row['status'], row['status'])}"
            for row in rows
        )
    return "\n".join(lines)


def display_number(value: Any, fallback: str = "未知") -> str:
    if value is None or isinstance(value, bool):
        return fallback
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or fallback


def hdhive_account_summary() -> str:
    profile = hdhive_response_data(hdhive_call("me"))
    if not isinstance(profile, dict):
        raise HTTPException(502, "影巢没有返回账号信息")
    try:
        quota = hdhive_response_data(hdhive_call("quota"))
    except HTTPException:
        quota = {}
    if not isinstance(quota, dict):
        quota = {}

    level = str(profile.get("level") or "").lower()
    level_label = {
        "normal": "普通用户",
        "vip": "VIP",
        "forever_vip": "永久 VIP",
    }.get(level, str(profile.get("level") or "未知"))
    username = str(
        profile.get("username")
        or profile.get("nickname")
        or "未返回"
    )
    nickname = str(profile.get("nickname") or "").strip()
    account_name = (
        f"{nickname}（{username}）"
        if nickname and nickname != username
        else username
    )
    user_id = profile.get("id")
    if user_id is not None:
        account_name += f" · ID {user_id}"

    checked = "今日已签" if profile.get("checked_in_today") else "今日未签"
    weekly_unlimited = bool(profile.get("weekly_free_quota_unlimited"))
    weekly_total = profile.get("weekly_free_quota")
    weekly_remaining = profile.get("weekly_free_quota_remaining")
    weekly_label = (
        "不限额"
        if weekly_unlimited or weekly_remaining == -1
        else (
            f"{display_number(weekly_remaining)}/{display_number(weekly_total)}"
            if weekly_total is not None
            else display_number(weekly_remaining)
        )
    )
    endpoint_limit = quota.get("endpoint_limit")
    endpoint_remaining = quota.get("endpoint_remaining")
    api_quota = (
        f"{display_number(endpoint_remaining)}/{display_number(endpoint_limit)}"
        if endpoint_limit is not None
        else "平台动态分配"
    )
    status = "已封禁" if profile.get("is_blocked") else "正常"
    return "\n".join(
        [
            "🟠 影巢账号",
            "",
            f"账号：{account_name}",
            f"等级：{level_label}",
            f"状态：{status}",
            f"积分：{display_number(profile.get('points'))}",
            (
                f"签到：{checked} · 累计 "
                f"{display_number(profile.get('signin_days_total'), '0')} 天"
            ),
            f"分享数：{display_number(profile.get('share_num'), '0')}",
            f"周免费额度：{weekly_label}（剩余/总额）",
            f"奖励额度：{display_number(profile.get('bonus_quota'), '0')}",
            f"OpenAPI 额度：{api_quota}（剩余/上限）",
        ]
    )


def dian_account_summary() -> str:
    with db() as connection:
        configured = bool(
            setting(connection, "dian_base_url")
            and setting(connection, "dian_api_key")
        )
        enabled = setting(connection, "dian_signin_enabled") == "1"
        signin_time = setting(connection, "dian_signin_time") or "08:30"
        last_at = setting(connection, "dian_last_signin_at") or "暂无"
        last_status = setting(connection, "dian_last_signin_status") or "暂无"
        last_message = setting(connection, "dian_last_signin_message") or "暂无"
    return "\n".join(
        [
            "🟣 癫影账号",
            "",
            f"OpenAPI：{'已配置' if configured else '未配置'}",
            f"自动签到：{'已开启' if enabled else '已关闭'} · {signin_time}",
            f"最近签到：{last_at}",
            f"最近状态：{last_status}",
            f"结果：{last_message}",
        ]
    )


def handle_telegram_message(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    first, separator, argument = text.partition(" ")
    command = first.split("@", 1)[0].lower()
    argument = argument.strip() if separator else ""

    if text == "求片需求" or command == "/requests":
        send_telegram(telegram_request_summary(False))
    elif text == "完成情况" or command == "/completed":
        sync_emby_requests()
        send_telegram(telegram_request_summary(True))
    elif text == "影巢账号" or command == "/hdhive":
        try:
            send_telegram(hdhive_account_summary())
        except HTTPException as error:
            send_telegram(f"❌ 影巢账号读取失败\n\n原因：{error.detail}")
    elif text == "癫影账号" or command == "/dian":
        send_telegram(dian_account_summary())
    elif command == "/notice":
        if argument:
            notice = argument[:240]
            with db() as connection:
                set_setting(connection, "site_notice", notice)
                set_setting(connection, "telegram_notice_pending", "")
            send_telegram(f"📢 片库公告已发布\n\n{notice}")
        else:
            with db() as connection:
                set_setting(connection, "telegram_notice_pending", "1")
            send_telegram("请发送公告内容，你的下一条普通消息会显示在网页上。")
    elif command == "/clear_notice":
        with db() as connection:
            set_setting(connection, "site_notice", "")
            set_setting(connection, "telegram_notice_pending", "")
        send_telegram("✅ 片库公告已清除")
    elif command in ("/start", "/menu"):
        send_telegram(
            "请点击左下角“菜单”，可以查看求片需求、完成情况、影巢/癫影账号，"
            "或发布片库公告。"
        )
    elif not text.startswith("/"):
        with db() as connection:
            pending = setting(connection, "telegram_notice_pending") == "1"
            if pending:
                notice = text[:240]
                set_setting(connection, "site_notice", notice)
                set_setting(connection, "telegram_notice_pending", "")
        if not pending:
            return False
        send_telegram(f"📢 片库公告已发布\n\n{notice}")
    else:
        return False
    return True


def wecom_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    values = sorted([token, timestamp, nonce, encrypted])
    return hashlib.sha1("".join(values).encode()).hexdigest()


def wecom_decrypt(encrypted: str, aes_key: str, corp_id: str) -> str:
    try:
        key = base64.b64decode(aes_key + "=")
        ciphertext = base64.b64decode(encrypted)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(256).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        size = struct.unpack("!I", plain[16:20])[0]
        message = plain[20:20 + size]
        receiver = plain[20 + size:].decode()
        if receiver != corp_id:
            raise ValueError("企业ID不匹配")
        return message.decode()
    except Exception as error:
        raise HTTPException(400, "企业微信回调解密失败") from error


def handle_wecom_command(command: str, sender: str) -> bool:
    text = str(command or "").strip().lower().lstrip("/")
    with db() as connection:
        admin_user = setting(connection, "wecom_admin_userid")
    if admin_user and sender != admin_user:
        send_wecom("❌ 当前企业微信账号没有映单管理权限", sender)
        return True
    if text in ("requests", "求片需求"):
        send_wecom(telegram_request_summary(False), sender)
    elif text in ("completed", "完成情况"):
        sync_emby_requests()
        send_wecom(telegram_request_summary(True), sender)
    elif text in ("hdhive", "影巢账号"):
        try:
            send_wecom(hdhive_account_summary(), sender)
        except HTTPException as error:
            send_wecom(f"❌ 影巢账号读取失败\n\n原因：{error.detail}", sender)
    elif text in ("dian", "癫影账号"):
        send_wecom(dian_account_summary(), sender)
    elif text in ("notice", "发布公告"):
        with db() as connection:
            set_setting(connection, f"wecom_notice_pending:{sender}", "1")
        send_wecom("请发送公告内容，你的下一条文字消息会显示在网页上。", sender)
    elif text in ("clear_notice", "清除公告"):
        with db() as connection:
            set_setting(connection, "site_notice", "")
            set_setting(connection, f"wecom_notice_pending:{sender}", "")
        send_wecom("✅ 片库公告已清除", sender)
    else:
        with db() as connection:
            pending = setting(connection, f"wecom_notice_pending:{sender}") == "1"
            if pending:
                notice = str(command)[:240]
                set_setting(connection, "site_notice", notice)
                set_setting(connection, f"wecom_notice_pending:{sender}", "")
        if not pending:
            send_wecom("请使用企业微信应用底部菜单操作映单。", sender)
            return False
        send_wecom(f"📢 片库公告已发布\n\n{notice}", sender)
    return True


def telegram_poll_loop() -> None:
    global TELEGRAM_OFFSET
    while True:
        with db() as connection:
            token = setting(connection, "telegram_token")
            allowed_chat = setting(connection, "telegram_chat_id")
        if not token or not allowed_chat:
            time.sleep(5)
            continue
        data = telegram_request(
            "getUpdates",
            {"offset": TELEGRAM_OFFSET, "timeout": 20, "allowed_updates": ["message"]},
        )
        for update in data.get("result", []):
            TELEGRAM_OFFSET = max(TELEGRAM_OFFSET, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = str(message.get("text") or "").strip()
            if chat_id != str(allowed_chat):
                continue
            handle_telegram_message(text)
        if not data:
            time.sleep(3)


def emby_sync_loop() -> None:
    while True:
        try:
            sync_emby_requests()
        except Exception:
            pass
        time.sleep(300)


def dian_signin_loop() -> None:
    while True:
        try:
            now = datetime.now()
            with db() as connection:
                enabled = setting(connection, "dian_signin_enabled") == "1"
                schedule = setting(connection, "dian_signin_time") or "08:30"
                mode = setting(connection, "dian_signin_mode") or "normal"
                last_day = setting(connection, "dian_last_signin_day")
                configured = bool(
                    setting(connection, "dian_api_key")
                    and setting(connection, "dian_base_url")
                )
            if (
                enabled
                and configured
                and last_day != now.date().isoformat()
                and now.strftime("%H:%M") >= schedule
            ):
                perform_dian_signin(mode, source="auto")
        except Exception:
            pass
        time.sleep(30)


def maybe_perform_hdhive_signin() -> bool:
    now = datetime.now()
    with db() as connection:
        row = hdhive_oauth_row(connection)
        enabled = setting(connection, "hdhive_signin_enabled") == "1"
        schedule = setting(connection, "hdhive_signin_time") or "08:35"
        mode = setting(connection, "hdhive_signin_mode") or "normal"
        last_day = setting(connection, "hdhive_last_signin_day")
        configured = bool(
            row["access_token_cipher"]
            and row["app_secret_cipher"]
            and row["status"] in ("connected", "rate_limited")
        )
    if (
        enabled
        and configured
        and last_day != now.date().isoformat()
        and now.strftime("%H:%M") >= schedule
    ):
        perform_hdhive_signin(mode, source="auto")
        return True
    return False


def cache_follow_resources(
    follow_id: int,
    source: str,
    resources: list[dict[str, Any]],
) -> None:
    observed_at = now_iso()
    with db() as connection:
        for index, resource in enumerate(resources):
            resource_key = str(
                resource.get("slug")
                or resource.get("resource_id")
                or resource.get("share_id")
                or index
            )
            connection.execute(
                "INSERT INTO tv_follow_resources("
                "follow_id, source, resource_key, payload_json, observed_at"
                ") VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(follow_id, source, resource_key) DO UPDATE SET "
                "payload_json = excluded.payload_json, "
                "observed_at = excluded.observed_at",
                (
                    follow_id,
                    source,
                    resource_key,
                    json.dumps(resource, ensure_ascii=False),
                    observed_at,
                ),
            )


def transferred_episode_set(tmdb_id: int, season_number: int) -> set[int]:
    with db() as connection:
        rows = connection.execute(
            "SELECT episode_number FROM resource_transfer_log "
            "WHERE tmdb_id = ? AND season_number = ? AND status = 'success' "
            "AND episode_number > 0",
            (tmdb_id, season_number),
        ).fetchall()
    return {int(row["episode_number"]) for row in rows}


def auto_replenish_hdhive_follow(
    follow_id: int,
    resources: Optional[list[dict[str, Any]]] = None,
    notify_noop: bool = False,
) -> dict[str, Any]:
    """Fill exact missing episodes from HDHive, largest multi-episode shares first."""

    with db() as connection:
        follow = connection.execute(
            "SELECT f.*, u.storage_destination FROM tv_follows f "
            "JOIN users u ON u.id = f.user_id WHERE f.id = ? AND f.active = 1",
            (follow_id,),
        ).fetchone()
        target_cid = setting(connection, "p115_target_cid") or "0"
    if not follow:
        return {"transferred": [], "message": "追更已停止"}
    if follow["storage_destination"] == "p123":
        return {
            "transferred": [],
            "message": "123模式只在手动选中资源时发送链接，不自动补集",
        }

    tmdb_id = int(follow["tmdb_id"])
    progress = destination_episode_progress(
        follow["storage_destination"],
        tmdb_id,
        known_in_library=True,
    )
    season_number = int(
        progress.get("emby_latest_season_number")
        or follow["last_seen_season"]
        or follow["baseline_season"]
        or 1
    )
    by_season = progress.get("emby_episode_numbers") or {}
    present = {
        int(value)
        for value in by_season.get(str(season_number), [])
        if int(value) > 0
    }
    present.update(transferred_episode_set(tmdb_id, season_number))

    if resources is None:
        result = hdhive_call("resources", "tv", tmdb_id)
        resources = normalize_supported_hdhive_resources(
            extract_share_items(result)
        )
    resources = [
        resource for resource in resources
        if hdhive_resource_is_supported(resource)
    ]
    cache_follow_resources(follow_id, "hdhive", resources)
    ordered = sorted(resources, key=hdhive_resource_priority, reverse=True)
    client = p115_client()
    transferred: set[int] = set()
    used_resources: list[str] = []

    # Limit the number of possible unlocks for a single notification. Official
    # group/VIP-free resources are preferred, then larger shares. Pack status
    # only breaks a size tie.
    for resource in ordered[:12]:
        slug = str(resource.get("slug") or "").strip()
        if not slug:
            continue
        advertised = {
            int(value)
            for value in resource.get("episode_numbers") or []
            if int(value) > 0
        }
        if advertised and advertised.issubset(present):
            continue
        try:
            unlocked = hdhive_call("unlock", slug)
            data = unlocked.get("data", unlocked)
            share_url = (
                str(data.get("full_url") or data.get("url") or "").strip()
                if isinstance(data, dict)
                else ""
            )
            if not is_115_share_url(share_url):
                continue
            tree = p115_share_tree(client, share_url)
            available: set[int] = set()
            for item in tree:
                if item.get("_share_is_dir"):
                    continue
                available.update(
                    int(value)
                    for value in parse_episode_spec(item.get("_share_name"))[
                        "episode_numbers"
                    ]
                    if int(value) > 0
                )
            missing = available - present
            if not missing:
                continue
            selected, selected_episodes = select_largest_missing_episode_files(
                tree,
                missing,
            )
            selected_ids = [
                str(item.get("_share_id") or "")
                for item in selected
                if item.get("_share_id")
            ]
            if not selected_ids or not selected_episodes:
                continue
            before_files = p115_folder_snapshot(client, target_cid)
            received = p115_call(
                "接收115分享失败",
                client.share_receive,
                {"file_id": ",".join(selected_ids), "cid": target_cid},
                share_url=share_url,
            )
            if not response_ok(received):
                continue
            if not wait_for_p115_change(
                lambda: p115_folder_snapshot(client, target_cid),
                before_files,
            ):
                continue
            message = (
                f"自动补齐第{compact_episode_numbers(selected_episodes)}集，"
                f"按每集最大文件选择"
            )
            record_transfer(
                user_id=int(follow["user_id"]),
                source="hdhive",
                resource_key=slug,
                tmdb_id=tmdb_id,
                transfer_scope="auto_missing",
                status="success",
                detail=message,
                follow_id=follow_id,
                season_number=season_number,
                episode_numbers=sorted(selected_episodes),
            )
            present.update(selected_episodes)
            transferred.update(selected_episodes)
            priority_label = (
                "官组 · VIP免积分"
                if resource.get("is_official_group")
                else "当前账号免积分"
                if resource.get("vip_free")
                else "普通资源"
            )
            used_resources.append(
                f"{priority_label}｜{resource.get('title') or slug}｜"
                f"{resource.get('size_gb') or '大小未知'}"
            )
        except HTTPException:
            continue

    checked_at = now_iso()
    if transferred:
        latest = max(transferred)
        message = (
            f"已自动补齐第{compact_episode_numbers(transferred)}集；"
            "多集资源优先，同集选择最大文件"
        )
        with db() as connection:
            connection.execute(
                "UPDATE tv_follows SET last_transferred_season = ?, "
                "last_transferred_episode = MAX(last_transferred_episode, ?), "
                "last_seen_season = ?, "
                "last_seen_episode = MAX(last_seen_episode, ?), "
                "last_checked_at = ?, last_message = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    season_number,
                    latest,
                    season_number,
                    latest,
                    checked_at,
                    message,
                    checked_at,
                    follow_id,
                ),
            )
        resource_summary = "\n".join(
            f"• {resource}" for resource in used_resources
        )
        send_notifications(
            f"✅ 影巢追更自动转存成功\n\n"
            f"剧集：{follow['title']}\n"
            f"补齐：第{compact_episode_numbers(transferred)}集\n"
            f"季数：第{season_number}季\n"
            f"规则：官组优先 → 当前账号免积分 → 文件最大\n"
            f"资源：\n{resource_summary}\n"
            f"115：已确认转存完成"
        )
    else:
        message = "已检查影巢更新，没有找到可安全识别并补齐的缺失集"
        with db() as connection:
            connection.execute(
                "UPDATE tv_follows SET last_checked_at = ?, "
                "last_message = ?, updated_at = ? WHERE id = ?",
                (checked_at, message, checked_at, follow_id),
            )
        if notify_noop:
            send_notifications(
                f"ℹ️ 影巢追更本次未转存\n\n"
                f"剧集：{follow['title']}\n"
                f"结果：{message}\n"
                f"说明：未覆盖已有集数，也未转存无法识别集数的文件"
            )
    return {
        "transferred": sorted(transferred),
        "resources": used_resources,
        "message": message,
    }


def refresh_hdhive_subscribed_follows() -> int:
    with db() as connection:
        follows = connection.execute(
            "SELECT * FROM tv_follows WHERE active = 1 "
            "AND hdhive_subscription_id IS NOT NULL"
        ).fetchall()
    changed = 0
    for follow in follows:
        media_type = str(follow["media_type"] or "tv")
        result = hdhive_call("resources", media_type, int(follow["tmdb_id"]))
        resources = normalize_supported_hdhive_resources(
            extract_share_items(result)
        )
        if media_type == "movie":
            resources = [
                resource for resource in resources
                if hdhive_movie_resource_is_playable(resource)
            ]
            resources.sort(key=hdhive_movie_resource_priority, reverse=True)
            cache_follow_resources(int(follow["id"]), "hdhive", resources)
            with db() as connection:
                connection.execute(
                    "UPDATE tv_follows SET last_checked_at = ?, "
                    "last_message = ?, updated_at = ? WHERE id = ?",
                    (
                        now_iso(),
                        (
                            "已检查影巢电影资源；ISO/BDMV 已排除，"
                            "可播放版本按文件大小排序"
                        ),
                        now_iso(),
                        follow["id"],
                    ),
                )
            continue
        cache_follow_resources(int(follow["id"]), "hdhive", resources)
        candidates: set[tuple[int, int]] = set()
        for resource in resources:
            season = int(
                resource.get("season_number")
                or follow["last_seen_season"]
                or 1
            )
            for episode in resource.get("episode_numbers") or []:
                if int(episode) > 0:
                    candidates.add((season, int(episode)))
        current = (
            int(follow["last_seen_season"] or follow["baseline_season"] or 1),
            int(follow["last_seen_episode"] or follow["baseline_episode"] or 0),
        )
        latest = max(candidates, default=current)
        checked_at = now_iso()
        if latest > current:
            changed += 1
            message = (
                f"影巢订阅发现第{latest[1]}集资源；"
                "请通过影巢机器人发送的链接手动转存"
            )
            with db() as connection:
                connection.execute(
                    "UPDATE tv_follows SET last_seen_season = ?, "
                    "last_seen_episode = ?, last_checked_at = ?, "
                    "last_message = ?, updated_at = ? WHERE id = ?",
                    (
                        latest[0],
                        latest[1],
                        checked_at,
                        message,
                        checked_at,
                        follow["id"],
                    ),
                )
            send_notifications(
                f"🆕 影巢追更提醒\n\n{follow['title']}\n"
                f"发现第{latest[1]}集资源，请打开影巢机器人链接手动转存。"
            )
        else:
            with db() as connection:
                connection.execute(
                    "UPDATE tv_follows SET last_checked_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (checked_at, checked_at, follow["id"]),
                )
    return changed


def poll_hdhive_follow_messages() -> int:
    with db() as connection:
        active_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM tv_follows WHERE active = 1 "
                "AND hdhive_subscription_id IS NOT NULL"
            ).fetchone()[0]
        )
    if not active_count:
        return 0
    result = hdhive_call(
        "messages",
        type="subscription",
        status="unread",
        page=1,
        page_size=50,
    )
    items = extract_share_items(result)
    created = 0
    message_ids: list[int] = []
    for item in items:
        message_key = str(
            item.get("id")
            or item.get("message_id")
            or hashlib.sha256(
                json.dumps(item, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        )
        event_type = str(item.get("event_type") or item.get("type") or "")
        with db() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO hdhive_message_log("
                "message_key, event_type, payload_json, created_at"
                ") VALUES(?, ?, ?, ?)",
                (
                    message_key,
                    event_type,
                    json.dumps(item, ensure_ascii=False),
                    now_iso(),
                ),
            )
        if cursor.rowcount:
            created += 1
        try:
            message_id = int(item.get("id") or item.get("message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        if message_id > 0:
            message_ids.append(message_id)
    if created:
        refresh_hdhive_subscribed_follows()
    if message_ids:
        hdhive_call("mark_messages_read", sorted(set(message_ids)))
    return created


def hdhive_follow_loop() -> None:
    while True:
        try:
            maybe_perform_hdhive_signin()
        except Exception:
            pass
        try:
            with db() as connection:
                row = hdhive_oauth_row(connection)
                enabled = setting(connection, "hdhive_poll_enabled") != "0"
                interval = max(
                    900,
                    int(setting(connection, "hdhive_poll_interval") or 1800),
                )
                last_poll = setting(connection, "hdhive_last_poll_at")
                configured = bool(
                    row["access_token_cipher"]
                    and row["app_secret_cipher"]
                    and row["status"] in ("connected", "rate_limited")
                )
            last_time = (
                datetime.fromisoformat(last_poll).timestamp()
                if last_poll
                else 0
            )
            if (
                HDHIVE_MESSAGE_POLLING_ENABLED
                and enabled
                and configured
                and time.time() - last_time >= interval
            ):
                poll_hdhive_follow_messages()
                with db() as connection:
                    set_setting(connection, "hdhive_last_poll_at", now_iso())
        except Exception:
            pass
        time.sleep(60)


@APP.on_event("startup")
def startup() -> None:
    init_db()
    Thread(target=configure_telegram_menu, name="telegram-menu", daemon=True).start()
    Thread(target=configure_wecom_menu, name="wecom-menu", daemon=True).start()
    Thread(target=telegram_poll_loop, name="telegram-bot", daemon=True).start()
    Thread(target=emby_sync_loop, name="emby-sync", daemon=True).start()
    Thread(target=dian_signin_loop, name="dian-signin", daemon=True).start()
    Thread(target=hdhive_follow_loop, name="hdhive-follow", daemon=True).start()


@APP.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_PATH)


@APP.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@APP.get("/api/wecom/callback", response_class=PlainTextResponse)
def verify_wecom_callback(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> str:
    with db() as connection:
        token = setting(connection, "wecom_callback_token")
        aes_key = setting(connection, "wecom_encoding_aes_key")
        corp_id = setting(connection, "wecom_corp_id")
    if not token or not aes_key or not corp_id:
        raise HTTPException(503, "企业微信回调尚未配置")
    if not hmac.compare_digest(
        wecom_signature(token, timestamp, nonce, echostr),
        msg_signature,
    ):
        raise HTTPException(403, "企业微信回调签名无效")
    return wecom_decrypt(echostr, aes_key, corp_id)


@APP.post("/api/wecom/callback", response_class=PlainTextResponse)
async def receive_wecom_callback(
    request: Request,
    msg_signature: str,
    timestamp: str,
    nonce: str,
) -> str:
    with db() as connection:
        token = setting(connection, "wecom_callback_token")
        aes_key = setting(connection, "wecom_encoding_aes_key")
        corp_id = setting(connection, "wecom_corp_id")
    if not token or not aes_key or not corp_id:
        raise HTTPException(503, "企业微信回调尚未配置")
    try:
        outer = ET.fromstring(await request.body())
        encrypted = str(outer.findtext("Encrypt") or "")
    except ET.ParseError as error:
        raise HTTPException(400, "企业微信回调格式无效") from error
    if not encrypted or not hmac.compare_digest(
        wecom_signature(token, timestamp, nonce, encrypted),
        msg_signature,
    ):
        raise HTTPException(403, "企业微信回调签名无效")
    try:
        message = ET.fromstring(wecom_decrypt(encrypted, aes_key, corp_id))
    except ET.ParseError as error:
        raise HTTPException(400, "企业微信消息格式无效") from error
    sender = str(message.findtext("FromUserName") or "")
    message_type = str(message.findtext("MsgType") or "").lower()
    if message_type == "event":
        command = str(message.findtext("EventKey") or "")
    elif message_type == "text":
        command = str(message.findtext("Content") or "")
    else:
        return "success"
    if sender and command:
        message_key = str(message.findtext("MsgId") or "") or hashlib.sha256(
            "|".join(
                [
                    sender,
                    str(message.findtext("CreateTime") or ""),
                    message_type,
                    command,
                ]
            ).encode()
        ).hexdigest()
        with db() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO wecom_message_log(message_key, created_at) "
                "VALUES(?, ?)",
                (message_key, now_iso()),
            )
        if not cursor.rowcount:
            return "success"
        handle_wecom_command(command, sender)
    return "success"


@APP.get("/api/tmdb/image/{size}/{image_path:path}")
def tmdb_image(
    size: str,
    image_path: str,
    movie_session: Optional[str] = Cookie(default=None),
) -> FileResponse:
    require_user(movie_session)
    clean_path = str(image_path or "").strip().lstrip("/")
    if (
        size not in TMDB_IMAGE_SIZES
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", clean_path)
        or not clean_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ):
        raise HTTPException(400, "TMDB 图片地址无效")

    image_url = f"https://image.tmdb.org/t/p/{size}/{clean_path}"
    suffix = Path(clean_path).suffix.lower() or ".jpg"
    cache_dir = DATA_DIR / "tmdb-images" / size
    cache_path = cache_dir / (
        hashlib.sha256(image_url.encode()).hexdigest() + suffix
    )
    headers = {"Cache-Control": "private, max-age=604800"}
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return FileResponse(cache_path, headers=headers)

    try:
        response = requests.get(image_url, timeout=15)
        response.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(502, "TMDB 图片暂时无法读取") from error
    content_type = str(response.headers.get("Content-Type") or "")
    if not content_type.startswith("image/") or not response.content:
        raise HTTPException(502, "TMDB 图片返回了无效内容")
    if len(response.content) > 20 * 1024 * 1024:
        raise HTTPException(502, "TMDB 图片文件过大")

    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(
        f"{cache_path.name}.{secrets.token_hex(6)}.tmp"
    )
    temporary.write_bytes(response.content)
    os.replace(temporary, cache_path)
    return FileResponse(cache_path, media_type=content_type, headers=headers)


@APP.get("/api/bootstrap")
def bootstrap(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    with db() as connection:
        ready = bool(connection.execute("SELECT 1 FROM users LIMIT 1").fetchone())
    return {"needs_setup": not ready, "user": session_user(movie_session)}


@APP.get("/api/notice")
def site_notice(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, str]:
    require_user(movie_session)
    with db() as connection:
        return {"text": setting(connection, "site_notice")}


@APP.post("/api/bootstrap")
async def setup(request: Request, response: Response) -> dict[str, Any]:
    payload = await request.json()
    with db() as connection:
        if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise HTTPException(409, "系统已经初始化")
        username = clean_username(payload.get("username"))
        password = clean_password(payload.get("password"))
        display_name = str(payload.get("display_name") or username).strip()[:40]
        cursor = connection.execute(
            "INSERT INTO users(username, display_name, password_hash, role, created_at) "
            "VALUES(?, ?, ?, 'admin', ?)",
            (username, display_name, hash_password(password), now_iso()),
        )
        set_setting(connection, "tmdb_token", payload.get("tmdb_token"))
        token = secrets.token_urlsafe(32)
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?, ?, ?)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                cursor.lastrowid,
                (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(),
            ),
        )
    response.set_cookie(
        "movie_session", token, httponly=True, samesite="lax", max_age=SESSION_DAYS * 86400
    )
    return {"ok": True}


@APP.post("/api/login")
async def login(request: Request, response: Response) -> dict[str, Any]:
    payload = await request.json()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if not row or not row["active"] or not verify_password(password, row["password_hash"]):
            raise HTTPException(401, "账号或密码不正确")
        token = secrets.token_urlsafe(32)
        connection.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),)
        )
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?, ?, ?)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                row["id"],
                (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(),
            ),
        )
    response.set_cookie(
        "movie_session", token, httponly=True, samesite="lax", max_age=SESSION_DAYS * 86400
    )
    return {"ok": True}


@APP.post("/api/logout")
def logout(response: Response, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, bool]:
    if movie_session:
        with db() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (hashlib.sha256(movie_session.encode()).hexdigest(),),
            )
    response.delete_cookie("movie_session")
    return {"ok": True}


@APP.get("/api/search")
def search(
    q: str,
    movie_session: Optional[str] = Cookie(default=None),
    refresh: bool = False,
) -> dict[str, Any]:
    total_started = time.perf_counter()
    user = require_user(movie_session)
    query = q.strip()
    if len(query) < 1:
        return {"results": [], "timing_ms": {"total": 0}}
    database_started = time.perf_counter()
    local_items, followed_items, transferred_items = search_db_context(
        int(user["id"]),
        query,
    )
    database_ms = round((time.perf_counter() - database_started) * 1000)
    tmdb_started = time.perf_counter()
    if local_items and not refresh:
        data = {"results": local_items}
        result_source = "local"
    else:
        data = tmdb_get(
            "/search/multi",
            {
                "query": query,
                "language": "zh-CN",
                "include_adult": "false",
                "page": 1,
            },
            timeout=(3, 6),
            force_refresh=refresh,
        )
        source_items = [
            item for item in data.get("results", [])
            if item.get("media_type") in ("movie", "tv")
        ]
        cache_tmdb_search_catalog(source_items)
        result_source = "tmdb"
    tmdb_ms = round((time.perf_counter() - tmdb_started) * 1000)
    # Search must not wait for a full Emby library scan. The cached IDs are
    # enough to paint results immediately; an expired cache refreshes behind
    # the scenes.
    emby_started = time.perf_counter()
    library_ids = destination_emby_ids(
        user["storage_destination"], prefer_cached=True
    )
    emby_ms = round((time.perf_counter() - emby_started) * 1000)
    source_items = [
        item for item in data.get("results", [])
        if item.get("media_type") in ("movie", "tv")
    ][:20]
    results = []
    for item in source_items:
        media_type = item.get("media_type")
        results.append(tmdb_media_item(item, media_type, library_ids))
    for item in results:
        item["is_following"] = (
            (str(item.get("media_type") or ""), int(item.get("tmdb_id") or 0))
            in followed_items
        )
        item["has_manual_transfer"] = (
            int(item.get("tmdb_id") or 0) in transferred_items
        )
    return {
        "results": results,
        "source": result_source,
        "refresh_recommended": result_source == "local",
        "timing_ms": {
            "tmdb": tmdb_ms,
            "emby": emby_ms,
            "database": database_ms,
            "total": round((time.perf_counter() - total_started) * 1000),
        },
    }


@APP.get("/api/charts/{chart_name}")
def charts(chart_name: str, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    user = require_user(movie_session)
    douban_config = {
        "douban_movies": ("/subject_collection/movie_hot_gaia/items", "movie", "豆瓣热门电影"),
        "douban_tv": ("/subject_collection/tv_hot/items", "tv", "豆瓣热门剧集"),
    }
    if chart_name in douban_config:
        path, media_type, title = douban_config[chart_name]
        data = douban_get(
            path,
            {"start": 0, "count": 20, "items_only": 1, "for_mobile": 1},
        )
        items = data.get("subject_collection_items") or []
        return {
            "title": title,
            "source": "douban",
            "results": [douban_media_item(item, media_type) for item in items[:20]],
        }
    chart_config = {
        "trending": ("/trending/all/week", None, "本周热门"),
        "movies": ("/movie/popular", "movie", "热门电影"),
        "tv": ("/tv/popular", "tv", "热门剧集"),
    }
    if chart_name not in chart_config:
        raise HTTPException(404, "没有找到这个榜单")
    path, fixed_type, title = chart_config[chart_name]
    data = tmdb_get(path, {"language": "zh-CN", "page": 1})
    library_ids = destination_emby_ids(
        user["storage_destination"], prefer_cached=True
    )
    results = []
    for item in data.get("results", []):
        media_type = fixed_type or item.get("media_type")
        if media_type not in ("movie", "tv"):
            continue
        results.append(tmdb_media_item(item, media_type, library_ids))
    return {"title": title, "results": results[:20]}


@APP.get("/api/douban/resolve/{media_type}/{douban_id}")
def resolve_douban(
    media_type: str,
    douban_id: str,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_user(movie_session)
    if media_type not in ("movie", "tv") or not douban_id.isdigit():
        raise HTTPException(400, "豆瓣影片信息无效")
    subject = douban_get(f"/{media_type}/{douban_id}")
    title = subject.get("original_title") or subject.get("title") or ""
    fallback_title = subject.get("title") or ""
    year = str(subject.get("year") or "")[:4]
    if not title:
        raise HTTPException(404, "豆瓣条目缺少片名，无法匹配 TMDB")
    params: dict[str, Any] = {
        "query": title,
        "language": "zh-CN",
        "include_adult": "false",
        "page": 1,
    }
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    data = tmdb_get(f"/search/{media_type}", params)
    candidates = data.get("results") or []
    if not candidates and fallback_title and fallback_title != title:
        params["query"] = fallback_title
        data = tmdb_get(f"/search/{media_type}", params)
        candidates = data.get("results") or []
    if not candidates:
        raise HTTPException(404, "这条豆瓣影片暂时无法准确匹配到 TMDB，不能提交")
    exact_year = []
    for candidate in candidates:
        date = candidate.get("release_date") or candidate.get("first_air_date") or ""
        if year and str(date)[:4] == year:
            exact_year.append(candidate)
    match = exact_year[0] if exact_year else candidates[0]
    tmdb_id = int(match.get("id") or 0)
    if tmdb_id <= 0:
        raise HTTPException(404, "这条豆瓣影片暂时无法准确匹配到 TMDB，不能提交")
    return {"tmdb_id": tmdb_id, "media_type": media_type}


@APP.get("/api/douban/poster")
def douban_poster(
    url: str,
    movie_session: Optional[str] = Cookie(default=None),
) -> Response:
    require_user(movie_session)
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (hostname == "doubanio.com" or hostname.endswith(".doubanio.com"))
        or not parsed.path.startswith("/view/photo/")
    ):
        raise HTTPException(400, "豆瓣海报地址无效")
    try:
        image = requests.get(
            url,
            headers={
                "Referer": "https://m.douban.com/",
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Mobile Safari/537.36",
            },
            timeout=15,
        )
        image.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(502, "豆瓣海报暂时无法读取") from error
    content_type = image.headers.get("Content-Type", "image/jpeg")
    if not content_type.startswith("image/"):
        raise HTTPException(502, "豆瓣海报返回了无效内容")
    return Response(
        content=image.content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@APP.get("/api/details/{media_type}/{tmdb_id}")
def media_details(
    media_type: str,
    tmdb_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "影片信息无效")
    data = tmdb_get(
        f"/{media_type}/{tmdb_id}",
        {
            "language": "zh-CN",
            "append_to_response": "credits,videos,recommendations",
        },
    )
    if int(data.get("id") or 0) != tmdb_id:
        raise HTTPException(404, "TMDB 没有找到这部影片")
    library_ids = destination_emby_ids(
        user["storage_destination"], prefer_cached=True
    )
    basic = tmdb_media_item(data, media_type, library_ids)
    credits = data.get("credits") or {}
    cast = [
        {
            "name": person.get("name") or "",
            "character": person.get("character") or "",
        }
        for person in credits.get("cast", [])[:8]
        if person.get("name")
    ]
    directors = [
        person.get("name")
        for person in credits.get("crew", [])
        if person.get("job") in ("Director", "Series Director") and person.get("name")
    ][:3]
    if media_type == "tv" and not directors:
        directors = [person.get("name") for person in data.get("created_by", []) if person.get("name")][:3]
    videos = (data.get("videos") or {}).get("results", [])
    trailer = next(
        (
            video
            for video in videos
            if video.get("site") == "YouTube"
            and video.get("type") in ("Trailer", "Teaser")
            and video.get("key")
        ),
        None,
    )
    recommendations = []
    for item in (data.get("recommendations") or {}).get("results", [])[:8]:
        recommendations.append(tmdb_media_item(item, media_type, library_ids))
    basic.update(
        {
            "backdrop_url": (
                tmdb_image_proxy_url(data.get("backdrop_path"), "original")
                if data.get("backdrop_path")
                else ""
            ),
            "genres": [genre.get("name") for genre in data.get("genres", []) if genre.get("name")],
            "runtime": int(data.get("runtime") or 0),
            "episode_runtime": int((data.get("episode_run_time") or [0])[0] or 0),
            "number_of_seasons": int(data.get("number_of_seasons") or 0),
            "number_of_episodes": int(data.get("number_of_episodes") or 0),
            "tagline": data.get("tagline") or "",
            "directors": directors,
            "cast": cast,
            "trailer_url": f"https://www.youtube.com/watch?v={trailer['key']}" if trailer else "",
            "recommendations": recommendations,
        }
    )
    with db() as connection:
        follow = connection.execute(
            "SELECT hdhive_subscription_id FROM tv_follows "
            "WHERE user_id = ? AND tmdb_id = ? AND active = 1",
            (user["id"], tmdb_id),
        ).fetchone()
        manual_transfer = connection.execute(
            "SELECT 1 FROM resource_transfer_log "
            "WHERE user_id = ? AND tmdb_id = ? AND transfer_scope = 'manual' "
            "AND status = 'success' LIMIT 1",
            (user["id"], tmdb_id),
        ).fetchone()
    basic["is_following"] = follow is not None
    basic["hdhive_subscribed"] = bool(
        follow and follow["hdhive_subscription_id"]
    )
    basic["has_manual_transfer"] = bool(manual_transfer)
    return basic


@APP.get("/api/emby/episode-progress/{tmdb_id}")
def emby_episode_progress(
    tmdb_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    if tmdb_id <= 0:
        raise HTTPException(400, "剧集编号无效")
    destination = user["storage_destination"]
    library_ids = destination_emby_ids(destination, force=True)
    progress = destination_episode_progress(
        destination,
        tmdb_id,
        known_in_library=tmdb_id in library_ids,
        force=True,
    )
    return {
        "in_library": bool(progress.get("emby_latest_episode_number")),
        "emby_latest_season_number": 0,
        "emby_latest_episode_number": 0,
        "emby_episode_label": "",
        "emby_episode_numbers": {},
        **progress,
    }


def hdhive_public_status() -> dict[str, Any]:
    with db() as connection:
        row = hdhive_oauth_row(connection)
        poll_enabled = (
            HDHIVE_MESSAGE_POLLING_ENABLED
            and setting(connection, "hdhive_poll_enabled") != "0"
        )
        poll_interval = max(
            900, int(setting(connection, "hdhive_poll_interval") or 1800)
        )
        last_poll = setting(connection, "hdhive_last_poll_at")
        signin_enabled = setting(connection, "hdhive_signin_enabled") == "1"
        signin_time = setting(connection, "hdhive_signin_time") or "08:35"
        signin_mode = setting(connection, "hdhive_signin_mode") or "normal"
        last_signin_at = setting(connection, "hdhive_last_signin_at")
        last_signin_mode = setting(connection, "hdhive_last_signin_mode")
        last_signin_status = setting(connection, "hdhive_last_signin_status")
        last_signin_message = setting(connection, "hdhive_last_signin_message")
    configured = bool(row["client_id"] and row["app_secret_cipher"])
    connected = bool(configured and row["access_token_cipher"])
    status = row["status"]
    if not configured:
        status = "waiting_approval"
    elif not connected:
        status = "ready_to_authorize"
    elif status not in ("rate_limited", "error"):
        status = "connected"
    labels = {
        "waiting_approval": "等待审核",
        "ready_to_authorize": "等待授权",
        "connected": "已连接",
        "rate_limited": "已限流，等待恢复",
        "error": "连接异常",
    }
    return {
        "status": status,
        "status_label": labels.get(status, status),
        "configured": configured,
        "connected": connected,
        "client_id": row["client_id"],
        "scopes": row["scopes"] or HDHIVE_SCOPES,
        "redirect_uri": row["redirect_uri"],
        "proxy_configured": bool(
            row["proxy_url_cipher"] or os.getenv("HDHIVE_PROXY_URL", "").strip()
        ),
        "proxy_label": (
            "云服务器固定出口"
            if row["proxy_url_cipher"] or os.getenv("HDHIVE_PROXY_URL", "").strip()
            else "尚未配置固定出口"
        ),
        "authorized_at": row["authorized_at"],
        "token_expires_at": row["token_expires_at"],
        "last_error": row["last_error"],
        "poll_enabled": poll_enabled,
        "poll_interval": poll_interval,
        "last_poll_at": last_poll,
        "signin_enabled": signin_enabled,
        "signin_time": signin_time,
        "signin_mode": signin_mode,
        "last_signin_at": last_signin_at,
        "last_signin_mode": last_signin_mode,
        "last_signin_status": last_signin_status,
        "last_signin_message": last_signin_message,
    }


@APP.get("/api/admin/hdhive/status")
def hdhive_admin_status(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    return hdhive_public_status()


@APP.patch("/api/admin/hdhive/config")
async def update_hdhive_config(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    redirect_uri = str(payload.get("redirect_uri") or "").strip()
    proxy_url = str(payload.get("proxy_url") or "").strip()
    signin_mode = str(payload.get("signin_mode") or "").strip()
    signin_time = str(payload.get("signin_time") or "").strip()
    if signin_mode and signin_mode not in ("normal", "lucky"):
        raise HTTPException(400, "影巢签到模式无效")
    if signin_time:
        try:
            datetime.strptime(signin_time, "%H:%M")
        except ValueError as error:
            raise HTTPException(400, "影巢签到时间格式无效") from error
    for label, value in (("回调地址", redirect_uri), ("固定出口代理", proxy_url)):
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise HTTPException(400, f"{label}格式无效")
    with db() as connection:
        row = hdhive_oauth_row(connection)
        client_id = str(payload.get("client_id") or row["client_id"]).strip()
        secret_cipher = row["app_secret_cipher"]
        proxy_cipher = row["proxy_url_cipher"]
        if str(payload.get("app_secret") or "").strip():
            secret_cipher = encrypt_secret(payload["app_secret"])
        if proxy_url:
            proxy_cipher = encrypt_secret(proxy_url)
        scopes = str(payload.get("scopes") or row["scopes"] or HDHIVE_SCOPES).strip()
        new_redirect = redirect_uri or row["redirect_uri"]
        status = (
            "connected"
            if row["access_token_cipher"]
            else "ready_to_authorize"
            if client_id and secret_cipher
            else "waiting_approval"
        )
        connection.execute(
            "UPDATE hdhive_oauth SET client_id = ?, app_secret_cipher = ?, "
            "scopes = ?, redirect_uri = ?, proxy_url_cipher = ?, status = ?, "
            "last_error = '', updated_at = ? WHERE id = 1",
            (
                client_id,
                secret_cipher,
                scopes,
                new_redirect,
                proxy_cipher,
                status,
                now_iso(),
            ),
        )
        if "poll_enabled" in payload:
            set_setting(
                connection,
                "hdhive_poll_enabled",
                "1"
                if HDHIVE_MESSAGE_POLLING_ENABLED and payload["poll_enabled"]
                else "0",
            )
        if payload.get("poll_interval"):
            interval = max(900, min(86400, int(payload["poll_interval"])))
            set_setting(connection, "hdhive_poll_interval", interval)
        if "signin_enabled" in payload:
            set_setting(
                connection,
                "hdhive_signin_enabled",
                "1" if payload["signin_enabled"] else "0",
            )
        if signin_time:
            set_setting(connection, "hdhive_signin_time", signin_time)
        if signin_mode:
            set_setting(connection, "hdhive_signin_mode", signin_mode)
    return {"ok": True, **hdhive_public_status()}


@APP.post("/api/admin/hdhive/checkin")
async def hdhive_checkin(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    with db() as connection:
        mode = str(
            payload.get("mode")
            or setting(connection, "hdhive_signin_mode")
            or "normal"
        )
    result = perform_hdhive_signin(mode, source="manual")
    return {
        "ok": True,
        "message": signin_result_message(result, "影巢签到成功"),
        "result": result,
    }


@APP.post("/api/admin/hdhive/oauth/start")
async def start_hdhive_oauth(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, str]:
    require_admin(movie_session)
    with db() as connection:
        row = hdhive_oauth_row(connection)
        redirect_uri = row["redirect_uri"] or str(
            request.base_url.replace(path="/api/hdhive/oauth/callback", query="")
        )
        state = secrets.token_urlsafe(36)
        connection.execute(
            "UPDATE hdhive_oauth SET state_hash = ?, state_expires_at = ?, "
            "redirect_uri = ?, updated_at = ? WHERE id = 1",
            (
                hashlib.sha256(state.encode()).hexdigest(),
                (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                redirect_uri,
                now_iso(),
            ),
        )
        client_id = row["client_id"]
        scopes = row["scopes"] or HDHIVE_SCOPES
    client = hdhive_client(require_authorized=False)
    return {
        "authorize_url": client.build_authorize_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scopes,
            state=state,
        )
    }


@APP.get("/api/hdhive/oauth/callback")
def hdhive_oauth_callback(code: str = "", state: str = "") -> RedirectResponse:
    if not code or not state:
        raise HTTPException(400, "影巢授权回调缺少 code 或 state")
    with db() as connection:
        row = hdhive_oauth_row(connection)
        valid_state = hmac.compare_digest(
            row["state_hash"],
            hashlib.sha256(state.encode()).hexdigest(),
        )
        expires_at = (
            datetime.fromisoformat(row["state_expires_at"])
            if row["state_expires_at"]
            else datetime.min.replace(tzinfo=timezone.utc)
        )
        redirect_uri = row["redirect_uri"]
    if not valid_state or expires_at < datetime.now(timezone.utc):
        raise HTTPException(400, "影巢授权状态已失效，请重新发起授权")
    try:
        tokens = hdhive_client(require_authorized=False).exchange_code(
            code, redirect_uri
        )
        hdhive_save_tokens(tokens)
        with db() as connection:
            connection.execute(
                "UPDATE hdhive_oauth SET state_hash = '', state_expires_at = '', "
                "updated_at = ? WHERE id = 1",
                (now_iso(),),
            )
    except (HDHiveOpenAPIError, ValueError) as error:
        with db() as connection:
            connection.execute(
                "UPDATE hdhive_oauth SET status = 'error', last_error = ?, "
                "updated_at = ? WHERE id = 1",
                (str(error), now_iso()),
            )
        raise HTTPException(502, f"影巢授权失败：{error}") from error
    return RedirectResponse(url="/?hdhive=connected", status_code=303)


@APP.post("/api/admin/hdhive/disconnect")
def disconnect_hdhive(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    try:
        client = hdhive_client()
        if client.refresh_token:
            client.revoke_refresh_token()
    except Exception:
        pass
    with db() as connection:
        connection.execute(
            "UPDATE hdhive_oauth SET access_token_cipher = '', "
            "refresh_token_cipher = '', authorized_at = '', token_expires_at = '', "
            "status = 'ready_to_authorize', last_error = '', updated_at = ? "
            "WHERE id = 1",
            (now_iso(),),
        )
    return {"ok": True}


@APP.get("/api/hdhive/resources/{media_type}/{tmdb_id}")
def hdhive_resources(
    media_type: str,
    tmdb_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_user(movie_session)
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "影片编号无效")
    result = hdhive_call("resources", media_type, tmdb_id)
    data = result.get("data", [])
    if isinstance(data, dict):
        items = extract_share_items({"data": data})
    elif isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
    else:
        items = []
    return {
        "resources": normalize_supported_hdhive_resources(items),
        "meta": result.get("meta", {}),
    }


def serialize_follow(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["active"] = bool(item["active"])
    item["hdhive_subscribed"] = bool(item.get("hdhive_subscription_id"))
    item["poster_url"] = tmdb_image_proxy_url(item["poster_path"], "w342")
    item["baseline_label"] = episode_progress_label(
        int(item["baseline_season"]),
        int(item["baseline_episode"]),
        "已从",
    )
    item["latest_label"] = episode_progress_label(
        int(item["last_seen_season"]),
        int(item["last_seen_episode"]),
        "已看到",
    )
    return item


def refresh_follow_emby_baseline(follow_id: int) -> sqlite3.Row:
    with db() as connection:
        row = connection.execute(
            "SELECT f.*, u.storage_destination FROM tv_follows f "
            "JOIN users u ON u.id = f.user_id WHERE f.id = ?", (follow_id,)
        ).fetchone()
    destination = storage_destination(row["storage_destination"]) if row else "p115"
    base_url, api_key = emby_credentials(destination)
    configured = bool(base_url and api_key)
    if not row or str(row["media_type"] or "tv") != "tv" or not configured:
        return row

    tmdb_id = int(row["tmdb_id"])
    library_ids = destination_emby_ids(destination, force=True)
    progress = (
        destination_episode_progress(
            destination,
            tmdb_id,
            known_in_library=True,
            force=True,
        )
        if tmdb_id in library_ids
        else {}
    )
    season_number = int(progress.get("emby_latest_season_number") or 1)
    episode_number = int(progress.get("emby_latest_episode_number") or 0)
    with db() as connection:
        connection.execute(
            "UPDATE tv_follows SET baseline_season = ?, baseline_episode = ?, "
            "updated_at = ? WHERE id = ?",
            (season_number, episode_number, now_iso(), follow_id),
        )
        return connection.execute(
            "SELECT * FROM tv_follows WHERE id = ?", (follow_id,)
        ).fetchone()


@APP.get("/api/follows")
def list_follows(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    with db() as connection:
        query = (
            "SELECT f.*, u.display_name, u.username FROM tv_follows f "
            "JOIN users u ON u.id = f.user_id "
        )
        values: tuple[Any, ...] = ()
        if user["role"] != "admin":
            query += "WHERE f.active = 1 AND f.user_id = ? "
            values = (user["id"],)
        else:
            query += "WHERE f.active = 1 "
        query += "ORDER BY f.active DESC, f.updated_at DESC"
        rows = connection.execute(query, values).fetchall()
    return {"follows": [serialize_follow(row) for row in rows]}


@APP.post("/api/follows")
async def create_follow(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    payload = await request.json()
    tmdb_id = int(payload.get("tmdb_id") or 0)
    media_type = str(payload.get("media_type") or "tv").strip().lower()
    subscription_slug = str(payload.get("slug") or "").strip()
    subscription_resource = payload.get("resource")
    if not isinstance(subscription_resource, dict):
        subscription_resource = None
    if tmdb_id <= 0 or media_type not in ("movie", "tv"):
        raise HTTPException(400, "影片编号无效")
    detail = tmdb_get(
        f"/{media_type}/{tmdb_id}",
        {"language": "zh-CN", "append_to_response": "external_ids"},
    )
    if int(detail.get("id") or 0) != tmdb_id:
        raise HTTPException(400, "TMDB 没有返回对应影片")
    known_in_library = (
        tmdb_id in destination_emby_ids(
            user["storage_destination"], prefer_cached=True
        )
        if media_type == "tv"
        else False
    )
    if (
        media_type == "tv"
        and not known_in_library
        and not (
            has_manual_transfer(tmdb_id, int(user["id"]))
            if user["storage_destination"] == "p123"
            else has_manual_transfer(tmdb_id)
        )
    ):
        raise HTTPException(409, "请先手动转存初始版本，再开启影巢追更")
    progress = (
        destination_episode_progress(
            user["storage_destination"],
            tmdb_id,
            known_in_library=known_in_library,
        )
        if media_type == "tv"
        else {}
    )
    baseline_season = int(progress.get("emby_latest_season_number") or 1)
    baseline_episode = int(progress.get("emby_latest_episode_number") or 0)
    title = str(
        detail.get("name")
        or detail.get("title")
        or detail.get("original_name")
        or detail.get("original_title")
        or "未命名影片"
    )
    first_air = str(detail.get("first_air_date") or detail.get("release_date") or "")
    with db() as connection:
        connection.execute(
            "INSERT INTO tv_follows("
            "user_id, tmdb_id, media_type, title, original_title, year, poster_path, "
            "baseline_season, baseline_episode, last_seen_season, "
            "last_seen_episode, created_at, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, tmdb_id) DO UPDATE SET active = 1, "
            "media_type = excluded.media_type, title = excluded.title, "
            "original_title = excluded.original_title, "
            "year = excluded.year, poster_path = excluded.poster_path, "
            "baseline_season = MAX(tv_follows.baseline_season, excluded.baseline_season), "
            "baseline_episode = MAX(tv_follows.baseline_episode, excluded.baseline_episode), "
            "updated_at = excluded.updated_at",
            (
                user["id"],
                tmdb_id,
                media_type,
                title,
                str(detail.get("original_name") or detail.get("original_title") or ""),
                first_air[:4],
                str(detail.get("poster_path") or ""),
                baseline_season,
                baseline_episode,
                baseline_season,
                baseline_episode,
                now_iso(),
                now_iso(),
            ),
        )
        row = connection.execute(
            "SELECT f.*, u.display_name, u.username FROM tv_follows f "
            "JOIN users u ON u.id = f.user_id "
            "WHERE f.user_id = ? AND f.tmdb_id = ?",
            (user["id"], tmdb_id),
        ).fetchone()
    bind_error = ""
    if subscription_slug:
        try:
            row = bind_hdhive_follow_subscription(
                int(row["id"]),
                subscription_slug,
                subscription_resource,
            )
        except HTTPException as error:
            bind_error = str(error.detail)
            with db() as connection:
                connection.execute(
                    "UPDATE tv_follows SET last_message = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        f"影巢原生订阅未开启：{bind_error}",
                        now_iso(),
                        row["id"],
                    ),
                )
                row = connection.execute(
                    "SELECT f.*, u.display_name, u.username FROM tv_follows f "
                    "JOIN users u ON u.id = f.user_id WHERE f.id = ?",
                    (row["id"],),
                ).fetchone()
    else:
        bind_error = "缺少影巢订阅资源，请刷新详情后重试"
    return {
        "ok": True,
        "follow": serialize_follow(row),
        "native_subscription_error": bind_error,
    }


def bind_hdhive_follow_subscription(
    follow_id: int,
    slug: str,
    resource: Optional[dict[str, Any]] = None,
) -> sqlite3.Row:
    slug = str(slug or "").strip()
    if not slug:
        raise HTTPException(400, "请选择一个影巢长期更新资源")
    with db() as connection:
        follow = connection.execute(
            "SELECT * FROM tv_follows WHERE id = ? AND active = 1",
            (follow_id,),
        ).fetchone()
    if not follow:
        raise HTTPException(404, "没有找到这条有效追更")

    tmdb_id = int(follow["tmdb_id"])
    media_type = str(follow["media_type"] or "tv")
    target = cached_hdhive_media_target(media_type, tmdb_id)
    resolved_media_url = str((target or {}).get("media_url") or "")
    target_was_cached = target is not None
    if target is None:
        share_result = hdhive_call("share", slug)
        try:
            target = hdhive_subscription_target(
                share_result,
                tmdb_id,
                media_type,
            )
        except HTTPException as error:
            if str(error.detail) != "影巢分享详情缺少影片内部编号，无法创建订阅":
                raise
            selected = resource or cached_hdhive_follow_resource(follow_id, slug)
            media_url = hdhive_media_page_url(selected, share_result, media_type)
            if not media_url:
                resources_result = hdhive_call("resources", media_type, tmdb_id)
                resources = normalize_supported_hdhive_resources(
                    extract_share_items(resources_result)
                )
                cache_follow_resources(follow_id, "hdhive", resources)
                selected = next(
                    (
                        item
                        for item in resources
                        if str(item.get("slug") or "") == slug
                    ),
                    {},
                )
                media_url = hdhive_media_page_url(
                    selected,
                    share_result,
                    media_type,
                )
            if not media_url:
                raise HTTPException(
                    502,
                    "影巢资源没有返回影片页面，无法解析原生订阅目标",
                ) from error
            target = hdhive_subscription_target_from_page(
                hdhive_media_page(media_url),
                tmdb_id,
                media_type,
                str(follow["title"] or ""),
            )
            resolved_media_url = media_url
    created = hdhive_call(
        "create_subscription",
        target_type=target["target_type"],
        target_id=target["target_id"],
        target_key=target["target_key"],
    )
    if not target_was_cached:
        cache_hdhive_media_target(
            media_type,
            tmdb_id,
            target,
            resolved_media_url,
        )
    subscription_id = hdhive_created_subscription_id(created, target["target_key"])

    previous_id = int(follow["hdhive_subscription_id"] or 0)
    if previous_id > 0 and previous_id != subscription_id:
        try:
            hdhive_call("delete_subscription", previous_id)
        except HTTPException:
            pass

    message = f"已开启影巢原生订阅：{target['title']}"
    with db() as connection:
        connection.execute(
            "UPDATE tv_follows SET hdhive_subscription_id = ?, "
            "last_message = ?, last_checked_at = ?, updated_at = ? WHERE id = ?",
            (subscription_id, message, now_iso(), now_iso(), follow_id),
        )
        row = connection.execute(
            "SELECT f.*, u.display_name, u.username FROM tv_follows f "
            "JOIN users u ON u.id = f.user_id WHERE f.id = ?",
            (follow_id,),
        ).fetchone()
    return row


@APP.post("/api/follows/{follow_id}/hdhive-subscription")
async def create_hdhive_follow_subscription(
    follow_id: int,
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    row = bind_hdhive_follow_subscription(
        follow_id,
        str(payload.get("slug") or ""),
    )
    return {"ok": True, "follow": serialize_follow(row)}


@APP.delete("/api/follows/{follow_id}/hdhive-subscription")
def delete_hdhive_follow_subscription(
    follow_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    with db() as connection:
        follow = connection.execute(
            "SELECT * FROM tv_follows WHERE id = ?", (follow_id,)
        ).fetchone()
    if not follow:
        raise HTTPException(404, "没有找到这条追更")
    subscription_id = int(follow["hdhive_subscription_id"] or 0)
    if subscription_id > 0:
        hdhive_call("delete_subscription", subscription_id)
    with db() as connection:
        connection.execute(
            "UPDATE tv_follows SET hdhive_subscription_id = NULL, "
            "last_message = ?, updated_at = ? WHERE id = ?",
            ("影巢原生订阅已取消，本地追更仍保留", now_iso(), follow_id),
        )
    return {"ok": True}


@APP.delete("/api/follows/{follow_id}")
def delete_follow(
    follow_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM tv_follows WHERE id = ?", (follow_id,)
        ).fetchone()
        if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
            raise HTTPException(404, "没有找到这条追更")
        subscription_id = int(row["hdhive_subscription_id"] or 0)
        remaining = (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM tv_follows "
                    "WHERE active = 1 AND id != ? AND hdhive_subscription_id = ?",
                    (follow_id, subscription_id),
                ).fetchone()[0]
            )
            if subscription_id > 0
            else 0
        )
    if subscription_id > 0 and remaining == 0:
        hdhive_call("delete_subscription", subscription_id)
    with db() as connection:
        connection.execute(
            "UPDATE tv_follows SET active = 0, hdhive_subscription_id = NULL, "
            "updated_at = ? WHERE id = ?",
            (now_iso(), follow_id),
        )
    return {"ok": True}


@APP.get("/api/follows/{follow_id}/resources")
def follow_resources(
    follow_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM tv_follows WHERE id = ?", (follow_id,)
        ).fetchone()
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(404, "没有找到这条追更")
    row = refresh_follow_emby_baseline(follow_id)
    hdhive_items: list[dict[str, Any]] = []
    dian_items: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    media_type = str(row["media_type"] or "tv")
    try:
        hdhive_items = hdhive_resources(media_type, row["tmdb_id"], movie_session)[
            "resources"
        ]
        if media_type == "movie":
            hdhive_items = [
                item for item in hdhive_items
                if hdhive_movie_resource_is_playable(item)
            ]
            hdhive_items.sort(key=hdhive_movie_resource_priority, reverse=True)
        cache_follow_resources(follow_id, "hdhive", hdhive_items)
    except HTTPException as error:
        errors["hdhive"] = str(error.detail)
    try:
        dian_items = dian_resources(media_type, row["tmdb_id"], None, movie_session)[
            "resources"
        ]
        if media_type == "movie":
            dian_items = [
                item for item in dian_items
                if hdhive_movie_resource_is_playable(item)
            ]
            dian_items.sort(
                key=lambda item: resource_size_bytes(item.get("size_gb")),
                reverse=True,
            )
        cache_follow_resources(follow_id, "dian", dian_items)
    except HTTPException as error:
        errors["dian"] = str(error.detail)
    return {
        "follow": serialize_follow(row),
        "hdhive_resources": hdhive_items,
        "dian_resources": dian_items,
        "errors": errors,
        "source_order": ["hdhive", "dian"],
    }


@APP.get("/api/dian/resources/{media_type}/{tmdb_id}")
def dian_resources(
    media_type: str,
    tmdb_id: int,
    season: Optional[int] = None,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_user(movie_session)
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "影片编号无效")
    payload: dict[str, Any] = {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "page": 1,
        "size": 30,
        "sort": "hot",
    }
    # Dian treats season=0 as an explicit S0/specials filter, not "all seasons".
    # Omit it for the normal title-level lookup and only send it when requested.
    if media_type == "tv" and season is not None:
        payload["season"] = max(0, season)
    result = dian_call("list_shares", payload)
    return {
        "resources": normalize_supported_dian_resources(
            extract_share_items(result)
        )
    }


@APP.post("/api/hdhive/transfer")
async def hdhive_transfer(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    payload = await request.json()
    slug = str(payload.get("slug") or "").strip()
    tmdb_id = int(payload.get("tmdb_id") or 0)
    media_type = str(payload.get("media_type") or "")
    transfer_scope = "manual"
    if not slug or media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "影巢资源信息无效")

    unlocked = hdhive_call("unlock", slug)
    data = unlocked.get("data", unlocked)
    if not isinstance(data, dict):
        raise HTTPException(502, "影巢解锁结果格式无效")
    share_url = str(data.get("full_url") or data.get("url") or "").strip()
    if not share_url:
        raise HTTPException(502, "影巢解锁后没有返回资源链接")

    title_spec = parse_episode_spec(payload.get("resource_title"))
    wanted_episodes = set(title_spec["episode_numbers"])
    selected_episode_numbers = sorted(wanted_episodes)
    if user.get("storage_destination") == "p123":
        return await deliver_to_pansave(
            user=user,
            share_url=share_url,
            source="hdhive",
            resource_key=slug,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            tmdb_id=tmdb_id,
            season_number=int(title_spec["season_number"]),
            episode_numbers=selected_episode_numbers,
        )

    client = p115_client()
    with db() as connection:
        target_cid = setting(connection, "p115_target_cid") or "0"

    if not is_115_share_url(share_url):
        queued = p115_call(
            "提交115离线任务失败",
            client.clouddownload_task_add_url,
            {"url": share_url, "wp_path_id": target_cid},
        )
        if not response_ok(queued):
            raise HTTPException(502, response_message(queued, "115离线任务提交失败"))
        mode = "offline"
        message = "已提交115离线下载，正在后台处理"
    else:
        snap = p115_call(
            "读取115分享失败",
            client.share_snap,
            0,
            share_url=share_url,
        )
        if not response_ok(snap):
            raise HTTPException(502, response_message(snap, "无法读取115分享"))
        selected_ids = [
            p115_share_item_id(item)
            for item in extract_share_items(snap)
            if p115_share_item_id(item)
        ]
        if not selected_ids:
            raise HTTPException(502, "115分享中没有找到可转存内容")
        received = p115_call(
            "接收115分享失败",
            client.share_receive,
            {"file_id": ",".join(selected_ids), "cid": target_cid},
            share_url=share_url,
        )
        if not response_ok(received):
            raise HTTPException(502, response_message(received, "115转存失败"))
        mode = "share"
        message = "已提交到115，正在后台处理"

    record_transfer(
        user_id=int(user["id"]),
        source="hdhive",
        resource_key=slug,
        tmdb_id=tmdb_id,
        transfer_scope=transfer_scope,
        status="success",
        detail=message,
        season_number=int(title_spec["season_number"]),
        episode_numbers=sorted(set(selected_episode_numbers)),
    )
    send_notifications_async(
        f"☁️ 影巢资源已提交\n\n{payload.get('title') or '影片'} · {user['display_name']}\n{message}"
    )
    return {"ok": True, "mode": mode, "message": message}


@APP.post("/api/dian/transfer")
async def dian_transfer(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    payload = await request.json()
    share_id = int(payload.get("share_id") or 0)
    resource_id = int(payload.get("resource_id") or 0)
    if share_id <= 0 or resource_id <= 0:
        raise HTTPException(400, "资源信息无效")
    resource_key = f"{share_id}:{resource_id}"
    media_type = str(payload.get("media_type") or "")
    tmdb_id = int(payload.get("tmdb_id") or 0)
    transfer_scope = "manual"
    title_spec = parse_episode_spec(payload.get("resource_title"))
    wanted_episodes = set(title_spec["episode_numbers"])
    selected_episode_numbers = sorted(wanted_episodes)
    unlocked = dian_call("unlock", {"share_id": share_id, "resource_id": resource_id})
    unlocked_data = unlocked.get("data", unlocked)
    data = unlocked_data if isinstance(unlocked_data, dict) else {"url": unlocked_data}
    unlock_payload = data.get("payload") if "payload" in data else data
    links = extract_dian_transfer_links({"payload": unlock_payload})
    if not links:
        payload_type = type(unlock_payload).__name__
        payload_fields = (
            ", ".join(sorted(str(key) for key in unlock_payload.keys())) or "无"
            if isinstance(unlock_payload, dict)
            else "非对象"
        )
        raise HTTPException(
            502,
            "癫影 unlock 返回的 payload 中没有可用链接；"
            f"payload 类型：{payload_type}；payload 字段：{payload_fields}",
        )
    share_url = links[0]
    if user.get("storage_destination") == "p123":
        return await deliver_to_pansave(
            user=user,
            share_url=share_url,
            source="dian",
            resource_key=resource_key,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            tmdb_id=tmdb_id,
            season_number=int(title_spec["season_number"]),
            episode_numbers=selected_episode_numbers,
        )

    client = p115_client()
    with db() as connection:
        target_cid = setting(connection, "p115_target_cid") or "0"
    if not is_115_share_url(share_url):
        before_tasks = p115_offline_snapshot(client)
        if len(links) == 1:
            queued = p115_call(
                "提交115离线任务失败",
                client.clouddownload_task_add_url,
                {"url": share_url, "wp_path_id": target_cid}
            )
        else:
            offline_payload = {
                f"url[{index}]": link
                for index, link in enumerate(links)
            }
            offline_payload["wp_path_id"] = target_cid
            queued = p115_call(
                "批量提交115离线任务失败",
                client.clouddownload_task_add_urls,
                offline_payload,
            )
        if not response_ok(queued):
            raise HTTPException(502, response_message(queued, "115离线任务提交失败"))
        if not wait_for_p115_change(
            lambda: p115_offline_snapshot(client),
            before_tasks,
        ):
            raise HTTPException(
                502,
                "115接口没有创建云下载任务；返回：" + response_summary(queued),
            )
        send_notifications(
            f"☁️ 115离线任务已提交\n\n{payload.get('title') or '影片'} · {user['display_name']}"
        )
        record_transfer(
            user_id=int(user["id"]),
            source="dian",
            resource_key=resource_key,
            tmdb_id=tmdb_id,
            transfer_scope=transfer_scope,
            status="success",
            detail=f"已加入115离线下载（{len(links)}个任务）",
            season_number=int(title_spec["season_number"]),
            episode_numbers=selected_episode_numbers,
        )
        return {
            "ok": True,
            "mode": "offline",
            "message": f"已加入115离线下载（{len(links)}个任务），完成后会出现在所选目录",
        }

    before_files = p115_folder_snapshot(client, target_cid)
    snap = p115_call(
        "读取115分享失败",
        client.share_snap,
        0,
        share_url=share_url,
    )
    if not response_ok(snap):
        raise HTTPException(502, response_message(snap, "无法读取115分享"))
    items = extract_share_items(snap)
    file_ids = ",".join(
        str(item.get("fid") or item.get("file_id") or item.get("cid"))
        for item in items
        if item.get("fid") or item.get("file_id") or item.get("cid")
    )
    if not file_ids:
        raise HTTPException(502, "115分享中没有找到可转存内容")
    received = p115_call(
        "接收115分享失败",
        client.share_receive,
        {"file_id": file_ids, "cid": target_cid},
        share_url=share_url,
    )
    if not response_ok(received):
        raise HTTPException(502, response_message(received, "115转存失败"))
    if not wait_for_p115_change(
        lambda: p115_folder_snapshot(client, target_cid),
        before_files,
    ):
        raise HTTPException(
            502,
            "115接口没有把文件写入目标目录；返回：" + response_summary(received),
        )
    send_notifications(
        f"☁️ 115转存已提交\n\n{payload.get('title') or '影片'} · {user['display_name']}"
    )
    record_transfer(
        user_id=int(user["id"]),
        source="dian",
        resource_key=resource_key,
        tmdb_id=tmdb_id,
        transfer_scope=transfer_scope,
        status="success",
        detail="已手动转存所选资源",
        season_number=int(title_spec["season_number"]),
        episode_numbers=sorted(set(selected_episode_numbers)),
    )
    return {
        "ok": True,
        "mode": "share",
        "message": "已转存到115所选目录",
    }


@APP.get("/api/requests")
def list_requests(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    user = require_user(movie_session)
    sync_emby_requests()
    query = (
        "SELECT r.*, u.display_name, u.username FROM movie_requests r "
        "JOIN users u ON u.id = r.user_id "
    )
    values: tuple[Any, ...] = ()
    if user["role"] != "admin":
        query += "WHERE r.user_id = ? "
        values = (user["id"],)
    query += "ORDER BY CASE r.status WHEN 'pending' THEN 0 WHEN 'searching' THEN 1 "
    query += "WHEN 'approved' THEN 2 ELSE 3 END, r.created_at DESC"
    with db() as connection:
        rows = connection.execute(query, values).fetchall()
    return {"requests": [serialize_request(row) for row in rows]}


@APP.post("/api/requests")
async def create_request(request: Request, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    user = require_user(movie_session)
    payload = await request.json()
    media_type = str(payload.get("media_type") or "")
    tmdb_id = int(payload.get("tmdb_id") or 0)
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "影片信息无效")
    # Do not trust movie metadata sent by the browser. Fetch it again from TMDB so
    # every saved request is tied to a real, canonical TMDB movie or TV record.
    canonical = tmdb_get(f"/{media_type}/{tmdb_id}", {"language": "zh-CN"})
    if int(canonical.get("id") or 0) != tmdb_id:
        raise HTTPException(400, "TMDB 没有找到这部影片")
    title = canonical.get("title") or canonical.get("name") or "未命名"
    original_title = canonical.get("original_title") or canonical.get("original_name") or ""
    date = canonical.get("release_date") or canonical.get("first_air_date") or ""
    poster_path = canonical.get("poster_path") or ""
    overview = canonical.get("overview") or ""
    if (
        media_type == "movie"
        and tmdb_id in destination_emby_ids(user["storage_destination"])
    ):
        raise HTTPException(409, "这部影片已经在 Emby 媒体库里了")
    with db() as connection:
        duplicate = connection.execute(
            "SELECT id FROM movie_requests WHERE user_id = ? AND tmdb_id = ? "
            "AND media_type = ? AND status NOT IN ('rejected', 'available')",
            (user["id"], tmdb_id, media_type),
        ).fetchone()
        if duplicate:
            raise HTTPException(409, "这部影片已经申请过了")
        timestamp = now_iso()
        cursor = connection.execute(
            "INSERT INTO movie_requests(user_id, tmdb_id, media_type, title, original_title, "
            "year, poster_path, overview, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user["id"], tmdb_id, media_type,
                str(title)[:200],
                str(original_title)[:200],
                str(date)[:4],
                str(poster_path)[:200],
                str(overview)[:2000], timestamp, timestamp,
            ),
        )
        request_id = cursor.lastrowid
    kind = "电影" if media_type == "movie" else "剧集"
    send_notifications(
        f"🎬 新的求片需求\n\n{user['display_name']} 想看："
        f"{title} ({str(date)[:4]})\n"
        f"类型：{kind}\nTMDB：{tmdb_id}"
    )
    return {"ok": True, "id": request_id}


@APP.patch("/api/requests/{request_id}")
async def update_request(request_id: int, request: Request, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    status = str(payload.get("status") or "")
    if status not in STATUS_NAMES:
        raise HTTPException(400, "状态无效")
    note = str(payload.get("admin_note") or "").strip()[:500]
    with db() as connection:
        row = connection.execute(
            "SELECT r.*, u.display_name FROM movie_requests r JOIN users u ON u.id = r.user_id WHERE r.id = ?",
            (request_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "没有找到这条需求")
        connection.execute(
            "UPDATE movie_requests SET status = ?, admin_note = ?, updated_at = ? WHERE id = ?",
            (status, note, now_iso(), request_id),
        )
    message = f"📌 求片状态更新\n\n{row['title']} → {STATUS_NAMES[status]}\n申请人：{row['display_name']}"
    if note:
        message += f"\n回复：{note}"
    send_notifications(message)
    return {"ok": True}


@APP.delete("/api/requests/{request_id}")
def delete_request(request_id: int, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    user = require_user(movie_session)
    with db() as connection:
        row = connection.execute(
            "SELECT r.*, u.display_name AS requester_name FROM movie_requests r "
            "JOIN users u ON u.id = r.user_id WHERE r.id = ?",
            (request_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "没有找到这条需求")
        if user["role"] != "admin" and row["user_id"] != user["id"]:
            raise HTTPException(403, "只能删除自己提交的需求")
        connection.execute("DELETE FROM movie_requests WHERE id = ?", (request_id,))
    actor = "管理员删除了" if user["role"] == "admin" else f"{row['requester_name']} 取消了"
    send_notifications(f"🗑️ 求片需求已删除\n\n{actor}：{row['title']} ({row['year']})")
    return {"ok": True}


@APP.get("/api/admin/users")
def list_users(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    with db() as connection:
        rows = connection.execute(
            "SELECT id, username, display_name, role, active, "
            "storage_destination, created_at "
            "FROM users ORDER BY role, created_at"
        ).fetchall()
    return {"users": [dict(row) for row in rows]}


@APP.post("/api/admin/users")
async def create_user(request: Request, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    username = clean_username(payload.get("username"))
    password = clean_password(payload.get("password"))
    display_name = str(payload.get("display_name") or username).strip()[:40]
    storage_destination = str(payload.get("storage_destination") or "p115")
    if storage_destination not in ("p115", "p123"):
        raise HTTPException(400, "转存目标无效")
    try:
        with db() as connection:
            connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, "
                "storage_destination, created_at) "
                "VALUES(?, ?, ?, 'member', ?, ?)",
                (
                    username, display_name, hash_password(password),
                    storage_destination, now_iso(),
                ),
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "这个账号已经存在") from error
    return {"ok": True}


@APP.patch("/api/admin/users/{user_id}")
async def update_user(user_id: int, request: Request, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    admin = require_admin(movie_session)
    payload = await request.json()
    if user_id == admin["id"] and payload.get("active") is False:
        raise HTTPException(400, "不能停用当前管理员")
    fields, values = [], []
    if "username" in payload:
        fields.append("username = ?")
        values.append(clean_username(payload["username"]))
    if "display_name" in payload:
        display_name = str(payload["display_name"]).strip()[:40]
        if not display_name:
            raise HTTPException(400, "显示名称不能为空")
        fields.append("display_name = ?")
        values.append(display_name)
    if "active" in payload:
        fields.append("active = ?")
        values.append(1 if payload["active"] else 0)
    if "storage_destination" in payload:
        destination = str(payload["storage_destination"])
        if destination not in ("p115", "p123"):
            raise HTTPException(400, "转存目标无效")
        fields.append("storage_destination = ?")
        values.append(destination)
    password_changed = False
    if payload.get("password"):
        fields.append("password_hash = ?")
        values.append(hash_password(clean_password(payload["password"])))
        password_changed = True
    if not fields:
        return {"ok": True}
    values.append(user_id)
    try:
        with db() as connection:
            existing = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            if not existing:
                raise HTTPException(404, "没有找到这个账号")
            connection.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
            if password_changed:
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    except sqlite3.IntegrityError as error:
        raise HTTPException(409, "这个账号已经存在") from error
    return {"ok": True}


@APP.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    admin = require_admin(movie_session)
    if user_id == admin["id"]:
        raise HTTPException(400, "不能删除当前管理员")
    with db() as connection:
        user = connection.execute(
            "SELECT id, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(404, "没有找到这个账号")
        if user["role"] == "admin":
            raise HTTPException(400, "不能删除管理员账号")
        connection.execute("DELETE FROM movie_requests WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"ok": True}


@APP.get("/api/admin/settings")
def get_settings(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    with db() as connection:
        tmdb = setting(connection, "tmdb_token")
        telegram = setting(connection, "telegram_token")
        chat_id = setting(connection, "telegram_chat_id")
        emby_url = setting(connection, "emby_url")
        emby_key = setting(connection, "emby_api_key")
        p123_emby_url = setting(connection, "p123_emby_url")
        p123_emby_key = setting(connection, "p123_emby_api_key")
        telegram_proxy = setting(connection, "telegram_proxy")
        dian_base_url = setting(connection, "dian_base_url")
        dian_key = setting(connection, "dian_api_key")
        dian_signin_enabled = setting(connection, "dian_signin_enabled") == "1"
        dian_signin_time = setting(connection, "dian_signin_time") or "08:30"
        dian_signin_mode = setting(connection, "dian_signin_mode") or "normal"
        dian_last_signin_at = setting(connection, "dian_last_signin_at")
        dian_last_signin_mode = setting(connection, "dian_last_signin_mode") or ""
        dian_last_signin_status = setting(connection, "dian_last_signin_status") or ""
        dian_last_signin_message = setting(connection, "dian_last_signin_message") or ""
        p115_app = setting(connection, "p115_app") or "alipaymini"
        p115_target_cid = setting(connection, "p115_target_cid") or "0"
        p115_target_name = setting(connection, "p115_target_name") or "根目录"
        pansave_api_id = setting(connection, "pansave_telegram_api_id")
        pansave_api_hash_cipher = setting(
            connection, "pansave_telegram_api_hash_cipher"
        )
        pansave_phone = setting(connection, "pansave_telegram_phone")
        pansave_session_cipher = setting(
            connection, "pansave_telegram_session_cipher"
        )
        pansave_authorized = (
            setting(connection, "pansave_telegram_authorized") == "1"
        )
        pansave_bot_username = (
            setting(connection, "pansave_bot_username") or "pansavenb_bot"
        )
        pansave_proxy_url = setting(connection, "pansave_telegram_proxy")
        wecom_corp_id = setting(connection, "wecom_corp_id")
        wecom_agent_id = setting(connection, "wecom_agent_id")
        wecom_secret = setting(connection, "wecom_secret")
        wecom_to_user = setting(connection, "wecom_to_user") or "@all"
        wecom_api_base = setting(connection, "wecom_api_base") or "https://wx.weige1999.xin"
        wecom_admin_userid = setting(connection, "wecom_admin_userid")
        callback_token = setting(connection, "wecom_callback_token")
        encoding_key = setting(connection, "wecom_encoding_aes_key")
        site_public_url = setting(connection, "site_public_url") or "https://qp.weige1999.xin"
    return {
        "tmdb_configured": bool(tmdb),
        "telegram_configured": bool(telegram and chat_id),
        "telegram_chat_id": chat_id,
        "emby_configured": bool(emby_url and emby_key),
        "emby_url": emby_url,
        "p123_emby_configured": bool(p123_emby_url and p123_emby_key),
        "p123_emby_url": p123_emby_url,
        "telegram_proxy": telegram_proxy,
        "dian_configured": bool(dian_base_url and dian_key),
        "dian_base_url": dian_base_url,
        "dian_key_prefix": f"{dian_key[:8]}***" if dian_key else "",
        "dian_signin_enabled": dian_signin_enabled,
        "dian_signin_time": dian_signin_time,
        "dian_signin_mode": dian_signin_mode,
        "dian_last_signin_at": dian_last_signin_at,
        "dian_last_signin_mode": dian_last_signin_mode,
        "dian_last_signin_status": dian_last_signin_status,
        "dian_last_signin_message": dian_last_signin_message,
        "p115_app": p115_app,
        "p115_app_name": P115_APPS.get(p115_app, p115_app),
        "p115_apps": P115_APPS,
        "p115_configured": p115_cookie_path().exists(),
        "p115_target_cid": p115_target_cid,
        "p115_target_name": p115_target_name,
        "pansave_configured": bool(
            pansave_api_id and pansave_api_hash_cipher and pansave_phone
        ),
        "pansave_connected": bool(pansave_session_cipher and pansave_authorized),
        "pansave_telegram_api_id": pansave_api_id,
        "pansave_telegram_api_hash_configured": bool(pansave_api_hash_cipher),
        "pansave_telegram_phone": pansave_phone,
        "pansave_bot_username": pansave_bot_username,
        "pansave_telegram_proxy": pansave_proxy_url,
        "wecom_configured": bool(wecom_corp_id and wecom_agent_id and wecom_secret),
        "wecom_callback_configured": bool(callback_token and encoding_key),
        "wecom_corp_id": wecom_corp_id,
        "wecom_agent_id": wecom_agent_id,
        "wecom_to_user": wecom_to_user,
        "wecom_api_base": wecom_api_base,
        "wecom_admin_userid": wecom_admin_userid,
        "site_public_url": site_public_url,
        "wecom_callback_url": site_public_url.rstrip("/") + "/api/wecom/callback",
    }


@APP.patch("/api/admin/settings")
async def update_settings(request: Request, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    if payload.get("dian_signin_mode") not in (None, "", "normal", "lucky"):
        raise HTTPException(400, "签到模式无效")
    if payload.get("dian_signin_time"):
        try:
            datetime.strptime(str(payload["dian_signin_time"]), "%H:%M")
        except ValueError as error:
            raise HTTPException(400, "签到时间格式无效") from error
    if payload.get("p115_app") and payload["p115_app"] not in P115_APPS:
        raise HTTPException(400, "不支持这个115设备身份")
    if payload.get("wecom_agent_id"):
        try:
            if int(str(payload["wecom_agent_id"])) <= 0:
                raise ValueError
        except ValueError as error:
            raise HTTPException(400, "企业微信 AgentID 无效") from error
    if payload.get("wecom_encoding_aes_key") and len(
        str(payload["wecom_encoding_aes_key"]).strip()
    ) != 43:
        raise HTTPException(400, "企业微信 EncodingAESKey 必须为43位")
    with db() as connection:
        for key in (
            "tmdb_token", "telegram_token", "telegram_chat_id", "telegram_proxy",
            "emby_url", "emby_api_key", "dian_base_url", "dian_api_key",
            "p123_emby_url", "p123_emby_api_key",
            "dian_signin_time", "dian_signin_mode", "p115_app",
            "p115_target_cid", "p115_target_name",
            "wecom_corp_id", "wecom_agent_id",
            "wecom_secret", "wecom_to_user", "wecom_api_base",
            "wecom_admin_userid", "wecom_callback_token",
            "wecom_encoding_aes_key", "site_public_url",
        ):
            if key in payload and str(payload[key]).strip():
                set_setting(connection, key, payload[key])
        if "dian_signin_enabled" in payload:
            set_setting(connection, "dian_signin_enabled", "1" if payload["dian_signin_enabled"] else "0")
    configure_telegram_menu()
    configure_wecom_menu()
    return {"ok": True}


@APP.post("/api/admin/dian-signin")
async def dian_signin(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    with db() as connection:
        mode = str(payload.get("mode") or setting(connection, "dian_signin_mode") or "normal")
    result = perform_dian_signin(mode, source="manual")
    message = signin_result_message(result, "癫影签到成功")
    return {"ok": True, "message": message, "result": result}


def probe_tmdb() -> None:
    with db() as connection:
        credential = setting(connection, "tmdb_token")
    if not credential:
        raise HTTPException(503, "尚未配置 TMDB 凭证")
    params: dict[str, Any] = {}
    headers = {"Accept": "application/json"}
    if credential.startswith("eyJ") or len(credential) > 80:
        headers["Authorization"] = f"Bearer {credential}"
    else:
        params["api_key"] = credential
    response = TMDB_HTTP.get(
        "https://api.themoviedb.org/3/configuration",
        params=params,
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()


def probe_hdhive() -> None:
    hdhive_call("ping")


def probe_dian() -> None:
    dian_call(
        "list_shares",
        {
            "tmdb_id": 1,
            "media_type": "movie",
            "page": 1,
            "size": 1,
            "sort": "hot",
        },
    )


def integration_probe(service: str) -> dict[str, Any]:
    probes = {
        "tmdb": ("TMDB", probe_tmdb),
        "hdhive": ("影巢", probe_hdhive),
        "dian": ("癫影", probe_dian),
    }
    if service not in probes:
        raise HTTPException(404, "不支持这个接口测速")
    label, callback = probes[service]
    started = time.perf_counter()
    try:
        callback()
        return {
            "service": service,
            "label": label,
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": "连接正常",
        }
    except HTTPException as error:
        message = str(error.detail)
    except requests.Timeout:
        message = "连接超时，请检查代理线路"
    except requests.RequestException as error:
        message = f"连接失败：{error.__class__.__name__}"
    except Exception as error:
        message = f"检测失败：{error}"
    return {
        "service": service,
        "label": label,
        "ok": False,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "message": message,
    }


@APP.post("/api/admin/integrations/test/{service}")
def test_integration(
    service: str,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    return integration_probe(service)


@APP.get("/api/admin/integrations")
def integration_status(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    settings = get_settings(movie_session)
    p115_online = False
    if settings["p115_configured"]:
        try:
            p115_online = bool(p115_client().login_status())
        except Exception:
            pass
    return {
        **settings,
        "p115_online": p115_online,
        "p115_logged_in": p115_online,
    }


@APP.post("/api/admin/pansave/login/start")
async def pansave_login_start(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    try:
        api_id = int(str(payload.get("api_id") or "").strip())
    except ValueError as error:
        raise HTTPException(400, "Telegram API ID 必须是数字") from error
    api_hash = str(payload.get("api_hash") or "").strip()
    phone = re.sub(r"[\s()-]+", "", str(payload.get("phone") or ""))
    bot_username = clean_pansave_bot_username(payload.get("bot_username"))
    proxy_url = str(payload.get("proxy_url") or "").strip()
    if api_id <= 0:
        raise HTTPException(400, "Telegram API ID 无效")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise HTTPException(400, "Telegram API Hash 应为32位字符")
    if not re.fullmatch(r"\+\d{6,20}", phone):
        raise HTTPException(400, "Telegram 手机号需包含国家区号，例如 +8613800138000")
    pansave_proxy(proxy_url)
    client = pansave_client(api_id, api_hash, "", proxy_url)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        session_string = client.session.save()
    except Exception as error:
        raise HTTPException(502, f"Telegram 验证码发送失败：{error}") from error
    finally:
        await client.disconnect()
    with db() as connection:
        set_setting(connection, "pansave_telegram_api_id", str(api_id))
        set_setting(
            connection,
            "pansave_telegram_api_hash_cipher",
            encrypt_secret(api_hash),
        )
        set_setting(connection, "pansave_telegram_phone", phone)
        set_setting(
            connection,
            "pansave_telegram_session_cipher",
            encrypt_secret(session_string),
        )
        set_setting(
            connection,
            "pansave_telegram_phone_code_hash_cipher",
            encrypt_secret(str(sent.phone_code_hash)),
        )
        set_setting(connection, "pansave_bot_username", bot_username)
        set_setting(connection, "pansave_telegram_proxy", proxy_url)
        set_setting(connection, "pansave_telegram_authorized", "0")
    return {"ok": True, "message": "验证码已发送到 Telegram"}


@APP.post("/api/admin/pansave/login/verify")
async def pansave_login_verify(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    code = re.sub(r"\D", "", str(payload.get("code") or ""))
    password = str(payload.get("password") or "")
    if not re.fullmatch(r"\d{4,8}", code):
        raise HTTPException(400, "请输入 Telegram 验证码")
    with db() as connection:
        code_hash_cipher = setting(
            connection, "pansave_telegram_phone_code_hash_cipher"
        )
    settings = pansave_login_settings()
    if not code_hash_cipher or not settings["session"]:
        raise HTTPException(409, "请先发送 Telegram 验证码")
    client = pansave_client(
        settings["api_id"],
        settings["api_hash"],
        settings["session"],
        settings["proxy_url"],
    )
    try:
        from telethon.errors import SessionPasswordNeededError
        await client.connect()
        try:
            await client.sign_in(
                phone=settings["phone"],
                code=code,
                phone_code_hash=decrypt_secret(code_hash_cipher),
            )
        except SessionPasswordNeededError:
            if not password:
                raise HTTPException(409, "该账号已开启两步验证，请填写 Telegram 密码")
            await client.sign_in(password=password)
        if not await client.is_user_authorized():
            raise HTTPException(401, "Telegram 登录未完成")
        me = await client.get_me()
        session_string = client.session.save()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(502, f"Telegram 登录验证失败：{error}") from error
    finally:
        await client.disconnect()
    with db() as connection:
        set_setting(
            connection,
            "pansave_telegram_session_cipher",
            encrypt_secret(session_string),
        )
        set_setting(connection, "pansave_telegram_authorized", "1")
        connection.execute(
            "DELETE FROM settings WHERE key = "
            "'pansave_telegram_phone_code_hash_cipher'"
        )
    display = (
        f"@{me.username}" if getattr(me, "username", None)
        else str(getattr(me, "first_name", "") or settings["phone"])
    )
    return {"ok": True, "message": f"Telegram 用户账号已登录：{display}"}


@APP.post("/api/admin/pansave/test")
async def pansave_test(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    settings = pansave_login_settings()
    if not settings["session"]:
        raise HTTPException(503, "尚未登录 Telegram 用户账号")
    client = pansave_client(
        settings["api_id"],
        settings["api_hash"],
        settings["session"],
        settings["proxy_url"],
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise HTTPException(401, "Telegram 用户会话已失效，请重新登录")
        bot = await client.get_entity(
            clean_pansave_bot_username(settings["bot_username"])
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(502, f"无法找到123机器人：{error}") from error
    finally:
        await client.disconnect()
    username = str(getattr(bot, "username", "") or settings["bot_username"])
    return {"ok": True, "message": f"Telegram 会话正常，已找到 @{username}"}


@APP.post("/api/admin/p115/qrcode")
async def p115_qrcode(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    app_name = str(payload.get("app") or "alipaymini")
    if app_name not in P115_APPS:
        raise HTTPException(400, "不支持这个扫码设备")
    try:
        from p115client import P115Client
        import qrcode
    except ImportError as error:
        raise HTTPException(503, "当前镜像缺少115扫码依赖") from error
    token_response = P115Client.login_qrcode_token(app_name)
    token_data = token_response.get("data") or {}
    uid = str(token_data.get("uid") or "")
    if not uid:
        raise HTTPException(502, "115没有返回二维码")
    qr_content = token_data.get("qrcode") or f"https://115.com/scan/dg-{uid}"
    image = qrcode.make(qr_content)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    token_id = secrets.token_urlsafe(20)
    with QR_LOGIN_LOCK:
        QR_LOGIN_TOKENS[token_id] = {
            "token": dict(token_data),
            "app": app_name,
            "created": time.time(),
        }
    return {
        "token": token_id,
        "token_id": token_id,
        "image": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
        "qr_image": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
        "app_name": P115_APPS[app_name],
    }


@APP.get("/api/admin/p115/qrcode/{token_id}")
def p115_qrcode_status(
    token_id: str,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    try:
        from p115client import P115Client
    except ImportError as error:
        raise HTTPException(503, "当前镜像缺少 p115client") from error
    with QR_LOGIN_LOCK:
        state = QR_LOGIN_TOKENS.get(token_id)
    if not state or time.time() - state["created"] > 300:
        raise HTTPException(410, "二维码已过期，请重新生成")
    status_response = P115Client.login_qrcode_scan_status(state["token"])
    status = int((status_response.get("data") or {}).get("status", 0))
    if status != 2:
        names = {-2: "canceled", -1: "expired", 0: "waiting", 1: "scanned"}
        return {"status": names.get(status, "waiting"), "status_code": status}
    result = P115Client.login_qrcode_scan_result(
        state["token"]["uid"], app=state["app"]
    )
    cookie = (result.get("data") or {}).get("cookie") or {}
    if not cookie:
        raise HTTPException(502, "115登录成功但没有返回Cookie")
    cookie_text = (
        cookie
        if isinstance(cookie, str)
        else "; ".join(f"{key}={value}" for key, value in cookie.items())
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p115_cookie_path().write_text(cookie_text)
    p115_cookie_path().chmod(0o600)
    with db() as connection:
        set_setting(connection, "p115_app", state["app"])
    with QR_LOGIN_LOCK:
        QR_LOGIN_TOKENS.pop(token_id, None)
    return {"status": "signed_in", "status_code": 2, "ok": True}


@APP.get("/api/admin/p115/folders")
def p115_folders(
    cid: int = 0,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    result = p115_client().fs_files(
        {"cid": max(0, cid), "limit": 200, "show_dir": 1, "cur": 1}
    )
    if not response_ok(result):
        raise HTTPException(502, str(result.get("error") or result.get("message") or "无法读取115目录"))
    items = extract_share_items(result)
    folders = []
    for item in items:
        folder_id = item.get("cid") or item.get("file_id") or item.get("fid")
        is_dir = bool(
            item.get("is_dir")
            or item.get("fc") in (0, "0")
            or item.get("file_category") in (0, "0")
        )
        if folder_id and is_dir:
            folders.append({"cid": str(folder_id), "name": item.get("n") or item.get("file_name") or item.get("name") or "目录"})
    return {"folders": folders, "cid": str(cid)}


@APP.post("/api/admin/telegram-test")
def telegram_test(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, bool]:
    require_admin(movie_session)
    with db() as connection:
        if not setting(connection, "telegram_token") or not setting(connection, "telegram_chat_id"):
            raise HTTPException(400, "请先保存 Telegram Bot Token 和 Chat ID")
    configure_telegram_menu()
    send_telegram("✅ 映单：Telegram 通知测试成功\n\n原生机器人菜单已经启用，旧快捷键已移除。")
    return {"ok": True}


@APP.post("/api/admin/emby-test")
def emby_test(
    destination: str = "p115",
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    destination = storage_destination(destination)
    base_url, api_key = emby_credentials(destination)
    if not base_url or not api_key:
        raise HTTPException(400, "请先保存 Emby 地址和 API 密钥")
    try:
        response = requests.get(
            emby_api_url(base_url, "/System/Info"),
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            timeout=12,
        )
        response.raise_for_status()
        info = response.json()
        count = len(emby_library_tmdb_ids(force=True, destination=destination))
        return {
            "ok": True,
            "server_name": info.get("ServerName") or "Emby",
            "library_tmdb_count": count,
        }
    except requests.RequestException as error:
        raise HTTPException(502, "无法连接 Emby，请检查地址和 API 密钥") from error


@APP.post("/api/admin/emby-sync")
def emby_sync(
    destination: Optional[str] = None,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    if destination not in (None, "p115", "p123"):
        raise HTTPException(400, "Emby 类型无效")
    return {
        "ok": True,
        "removed": sync_emby_requests(force=True, destination=destination),
    }


@APP.post("/api/admin/wecom-test")
def wecom_test(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, bool]:
    require_admin(movie_session)
    if not send_wecom(
        "✅ 映单：企业微信通知测试成功\n\n"
        "115转存、123链接投递与对应 Emby 入库结果都会同步通知。"
    ):
        raise HTTPException(502, "企业微信发送失败，请检查 CorpID、AgentID、Secret 和转发地址")
    return {"ok": True}


@APP.post("/api/admin/wecom-menu")
def wecom_menu(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, bool]:
    require_admin(movie_session)
    if not configure_wecom_menu():
        raise HTTPException(502, "企业微信菜单创建失败，请检查应用权限和配置")
    return {"ok": True}


if __name__ == "__main__":
    init_db()
    uvicorn.run(APP, host="0.0.0.0", port=PORT)
