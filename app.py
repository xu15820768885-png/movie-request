#!/usr/bin/env python3
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import struct
import time
import base64
import asyncio
import copy
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
import uvicorn
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from requests.adapters import HTTPAdapter
from dian115_openapi import Dian115OpenAPI, OpenAPIError
from hdhive_openapi import HDHiveOpenAPI, HDHiveOpenAPIError, TokenSet
from workflow import (
    ACTIVE_JOB_STATES,
    JOB_STATE_LABELS,
    episode_numbers_from_json,
    episode_numbers_json,
    message_target_hints,
)


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "movie-request.db"
WEB_PATH = Path(__file__).parent / "web" / "index.html"
LIBRARY_NOTIFICATION_FALLBACK_PATH = (
    Path(__file__).parent / "web" / "assets" / "library-notification-fallback.png"
)
PORT = int(os.getenv("PORT", "5056"))
SESSION_DAYS = 30
# Native HDHive subscriptions are created on demand. Subscription messages are
# consumed only when the granted OAuth token contains both required scopes.
HDHIVE_MESSAGE_POLLING_ENABLED = True
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
APP.add_middleware(GZipMiddleware, minimum_size=1000)
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
RESOURCE_RESPONSE_CACHE: dict[tuple[str, str, str, int, int], tuple[float, dict[str, Any]]] = {}
RESOURCE_REQUEST_LOCKS: dict[tuple[str, str, str, int, int], Lock] = {}
RESOURCE_CACHE_SECONDS = 120
RESOURCE_CACHE_MAX_ITEMS = 256
HDHIVE_FILE_LIST_CACHE_SECONDS = 6 * 3600
HDHIVE_INVALID_FILE_LIST_CACHE_SECONDS = 24 * 3600
HDHIVE_TRANSIENT_FILE_LIST_CACHE_SECONDS = 15 * 60
HDHIVE_QUIET_SCAN_SECONDS = 6 * 3600
IMAGE_DOWNLOAD_LOCKS: dict[str, Lock] = {}
IMAGE_CACHE_CLEANUP_LOCK = Lock()
IMAGE_CACHE_LAST_CLEANUP = 0.0
IMAGE_CACHE_MAX_FILES = 3000
IMAGE_CACHE_TARGET_FILES = 2500
WECOM_TOKEN_CACHE: dict[str, Any] = {"key": "", "token": "", "expires": 0.0}
WECOM_TOKEN_LOCK = Lock()
EMBY_WEBHOOK_LOCK = Lock()
EMBY_WEBHOOK_PENDING: set[str] = set()
EMBY_WEBHOOK_ITEMS: dict[str, dict[str, str]] = {}
EMBY_WEBHOOK_GENERATIONS: dict[str, int] = {}
LOGGER = logging.getLogger("uvicorn.error")
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
    workflow_job_id = int(getattr(getattr(request, "state", None), "workflow_job_id", 0) or 0)
    if workflow_job_id:
        fail_workflow_job(workflow_job_id, error.detail)
    if request.url.path in ("/api/hdhive/transfer", "/api/dian/transfer"):
        user = session_user(request.cookies.get("movie_session"))
        processing_conflict = (
            int(error.status_code) == 409
            and "正在处理" in str(error.detail or "")
        )
        if user and not processing_conflict:
            await asyncio.to_thread(
                send_notifications,
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


def resource_cache_key(
    provider: str,
    media_type: str,
    tmdb_id: int,
    season: Optional[int] = None,
) -> tuple[str, str, str, int, int]:
    return (
        str(DB_PATH),
        provider,
        media_type,
        int(tmdb_id),
        int(season) if season is not None else -1,
    )


def cached_resource_response(
    provider: str,
    media_type: str,
    tmdb_id: int,
    season: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    key = resource_cache_key(provider, media_type, tmdb_id, season)
    now = time.monotonic()
    with CACHE_LOCK:
        cached = RESOURCE_RESPONSE_CACHE.get(key)
        if not cached:
            return None
        if cached[0] <= now:
            RESOURCE_RESPONSE_CACHE.pop(key, None)
            return None
        return copy.deepcopy(cached[1])


def resource_request_lock(
    provider: str,
    media_type: str,
    tmdb_id: int,
    season: Optional[int] = None,
) -> Lock:
    key = resource_cache_key(provider, media_type, tmdb_id, season)
    with CACHE_LOCK:
        lock = RESOURCE_REQUEST_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            RESOURCE_REQUEST_LOCKS[key] = lock
        return lock


def cache_resource_response(
    provider: str,
    media_type: str,
    tmdb_id: int,
    response: dict[str, Any],
    season: Optional[int] = None,
) -> dict[str, Any]:
    key = resource_cache_key(provider, media_type, tmdb_id, season)
    now = time.monotonic()
    with CACHE_LOCK:
        if len(RESOURCE_RESPONSE_CACHE) >= RESOURCE_CACHE_MAX_ITEMS:
            expired = [
                cache_key
                for cache_key, cached in RESOURCE_RESPONSE_CACHE.items()
                if cached[0] <= now
            ]
            for cache_key in expired:
                RESOURCE_RESPONSE_CACHE.pop(cache_key, None)
            while len(RESOURCE_RESPONSE_CACHE) >= RESOURCE_CACHE_MAX_ITEMS:
                RESOURCE_RESPONSE_CACHE.pop(next(iter(RESOURCE_RESPONSE_CACHE)))
        RESOURCE_RESPONSE_CACHE[key] = (
            now + RESOURCE_CACHE_SECONDS,
            copy.deepcopy(response),
        )
    return response


def image_download_lock(cache_path: Path) -> Lock:
    key = str(cache_path)
    with CACHE_LOCK:
        lock = IMAGE_DOWNLOAD_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            IMAGE_DOWNLOAD_LOCKS[key] = lock
        return lock


def cleanup_image_cache_if_needed() -> None:
    global IMAGE_CACHE_LAST_CLEANUP
    now = time.monotonic()
    if now - IMAGE_CACHE_LAST_CLEANUP < 3600:
        return
    if not IMAGE_CACHE_CLEANUP_LOCK.acquire(blocking=False):
        return
    try:
        IMAGE_CACHE_LAST_CLEANUP = now
        roots = [DATA_DIR / "tmdb-images", DATA_DIR / "douban-images"]
        files = [
            path
            for root in roots
            if root.exists()
            for path in root.rglob("*")
            if path.is_file() and not path.name.endswith(".tmp")
        ]
        if len(files) <= IMAGE_CACHE_MAX_FILES:
            return
        files.sort(key=lambda path: path.stat().st_mtime)
        for path in files[: max(0, len(files) - IMAGE_CACHE_TARGET_FILES)]:
            try:
                path.unlink()
            except OSError:
                pass
    finally:
        IMAGE_CACHE_CLEANUP_LOCK.release()


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
                completed_at TEXT NOT NULL DEFAULT '',
                archived_at TEXT NOT NULL DEFAULT '',
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
                authorized_scopes TEXT NOT NULL DEFAULT '',
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
                current_emby_season INTEGER NOT NULL DEFAULT 1,
                current_emby_episode INTEGER NOT NULL DEFAULT 0,
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
                destination TEXT NOT NULL DEFAULT 'p115',
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
            DROP INDEX IF EXISTS resource_episode_success_idx;
            CREATE UNIQUE INDEX resource_episode_success_idx
                ON resource_transfer_log(tmdb_id, season_number, episode_number)
                WHERE status = 'success' AND episode_number > 0
                  AND transfer_scope != 'auto_wash';
            CREATE INDEX IF NOT EXISTS resource_manual_success_tmdb_idx
                ON resource_transfer_log(tmdb_id)
                WHERE transfer_scope = 'manual'
                  AND status = 'success' AND tmdb_id > 0;
            CREATE TABLE IF NOT EXISTS hdhive_message_log (
                message_key TEXT PRIMARY KEY,
                event_type TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                subscription_id INTEGER,
                tmdb_id INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                next_retry_at TEXT NOT NULL DEFAULT '',
                processed_at TEXT NOT NULL DEFAULT '',
                acknowledged_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hdhive_follow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL DEFAULT '',
                follow_id INTEGER REFERENCES tv_follows(id) ON DELETE SET NULL,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                tmdb_id INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS hdhive_follow_event_time_idx
                ON hdhive_follow_events(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS hdhive_follow_event_follow_idx
                ON hdhive_follow_events(follow_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS wecom_message_log (
                message_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS emby_library_monitor_state (
                destination TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS emby_library_snapshot (
                destination TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_type TEXT NOT NULL,
                series_id TEXT NOT NULL DEFAULT '',
                tmdb_id INTEGER NOT NULL DEFAULT 0,
                season_number INTEGER NOT NULL DEFAULT 0,
                episode_number INTEGER NOT NULL DEFAULT 0,
                observed_at TEXT NOT NULL,
                PRIMARY KEY(destination, item_id)
            );
            CREATE INDEX IF NOT EXISTS emby_library_snapshot_series_idx
                ON emby_library_snapshot(destination, series_id);
            CREATE TABLE IF NOT EXISTS emby_webhook_notifications (
                destination TEXT NOT NULL,
                item_id TEXT NOT NULL,
                item_type TEXT NOT NULL DEFAULT '',
                notified_at TEXT NOT NULL,
                PRIMARY KEY(destination, item_id)
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
            CREATE TABLE IF NOT EXISTS hdhive_wash_episodes (
                follow_id INTEGER NOT NULL REFERENCES tv_follows(id) ON DELETE CASCADE,
                season_number INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                closes_at TEXT NOT NULL,
                locked_at TEXT NOT NULL DEFAULT '',
                process_count INTEGER NOT NULL DEFAULT 0,
                last_resource_slug TEXT NOT NULL DEFAULT '',
                last_file_name TEXT NOT NULL DEFAULT '',
                last_file_size INTEGER NOT NULL DEFAULT 0,
                last_message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(follow_id, season_number, episode_number)
            );
            CREATE TABLE IF NOT EXISTS hdhive_wash_attempts (
                fingerprint TEXT PRIMARY KEY,
                follow_id INTEGER NOT NULL REFERENCES tv_follows(id) ON DELETE CASCADE,
                season_number INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                resource_slug TEXT NOT NULL,
                file_name TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS hdhive_wash_episode_time_idx
                ON hdhive_wash_episodes(follow_id, closes_at);
            CREATE TABLE IF NOT EXISTS hdhive_file_list_cache (
                slug TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'success',
                error TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS hdhive_file_list_cache_expiry_idx
                ON hdhive_file_list_cache(expires_at);
            CREATE TABLE IF NOT EXISTS media_workflow_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                follow_id INTEGER REFERENCES tv_follows(id) ON DELETE SET NULL,
                destination TEXT NOT NULL,
                source TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL DEFAULT 0,
                media_type TEXT NOT NULL DEFAULT '',
                season_number INTEGER NOT NULL DEFAULT 0,
                episode_numbers_json TEXT NOT NULL DEFAULT '[]',
                title TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'discovered',
                detail TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS media_workflow_job_state_idx
                ON media_workflow_jobs(state, next_retry_at, updated_at);
            CREATE INDEX IF NOT EXISTS media_workflow_job_follow_idx
                ON media_workflow_jobs(follow_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS p115_offline_monitors (
                workflow_job_id INTEGER PRIMARY KEY
                    REFERENCES media_workflow_jobs(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                follow_id INTEGER REFERENCES tv_follows(id) ON DELETE SET NULL,
                destination TEXT NOT NULL DEFAULT 'p115',
                source TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                tmdb_id INTEGER NOT NULL DEFAULT 0,
                media_type TEXT NOT NULL DEFAULT '',
                season_number INTEGER NOT NULL DEFAULT 0,
                episode_numbers_json TEXT NOT NULL DEFAULT '[]',
                title TEXT NOT NULL DEFAULT '',
                target_cid TEXT NOT NULL DEFAULT '0',
                links_json TEXT NOT NULL DEFAULT '[]',
                expected_files_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS p115_offline_monitor_status_idx
                ON p115_offline_monitors(status, updated_at);
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL,
                recipient TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS notification_outbox_dedupe_idx
                ON notification_outbox(dedupe_key, channel)
                WHERE dedupe_key != '';
            CREATE INDEX IF NOT EXISTS notification_outbox_pending_idx
                ON notification_outbox(status, next_retry_at, id);
            CREATE TABLE IF NOT EXISTS worker_health (
                worker TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'idle',
                last_started_at TEXT NOT NULL DEFAULT '',
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
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
        added_current_emby_season = "current_emby_season" not in follow_columns
        added_current_emby_episode = "current_emby_episode" not in follow_columns
        if added_current_emby_season:
            connection.execute(
                "ALTER TABLE tv_follows ADD COLUMN current_emby_season "
                "INTEGER NOT NULL DEFAULT 1"
            )
        if added_current_emby_episode:
            connection.execute(
                "ALTER TABLE tv_follows ADD COLUMN current_emby_episode "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if added_current_emby_season or added_current_emby_episode:
            connection.execute(
                "UPDATE tv_follows SET current_emby_season = baseline_season, "
                "current_emby_episode = baseline_episode"
            )
        request_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(movie_requests)").fetchall()
        }
        if "completed_at" not in request_columns:
            connection.execute(
                "ALTER TABLE movie_requests ADD COLUMN completed_at "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "archived_at" not in request_columns:
            connection.execute(
                "ALTER TABLE movie_requests ADD COLUMN archived_at "
                "TEXT NOT NULL DEFAULT ''"
            )
        transfer_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(resource_transfer_log)"
            ).fetchall()
        }
        if "destination" not in transfer_columns:
            connection.execute(
                "ALTER TABLE resource_transfer_log ADD COLUMN destination "
                "TEXT NOT NULL DEFAULT 'p115'"
            )
            connection.execute(
                "UPDATE resource_transfer_log SET destination = COALESCE(("
                "SELECT storage_destination FROM users "
                "WHERE users.id = resource_transfer_log.user_id), 'p115')"
            )
        connection.execute("DROP INDEX IF EXISTS resource_transfer_success_idx")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS resource_transfer_success_idx "
            "ON resource_transfer_log(user_id, destination, source, resource_key, "
            "transfer_scope, season_number, episode_number) "
            "WHERE status = 'success'"
        )
        connection.execute("DROP INDEX IF EXISTS resource_episode_success_idx")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS resource_episode_success_idx "
            "ON resource_transfer_log(user_id, destination, tmdb_id, "
            "season_number, episode_number) WHERE status = 'success' "
            "AND episode_number > 0 AND transfer_scope != 'auto_wash'"
        )
        message_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(hdhive_message_log)"
            ).fetchall()
        }
        added_message_status = "status" not in message_columns
        message_column_sql = {
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "subscription_id": "INTEGER",
            "tmdb_id": "INTEGER NOT NULL DEFAULT 0",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT NOT NULL DEFAULT ''",
            "next_retry_at": "TEXT NOT NULL DEFAULT ''",
            "processed_at": "TEXT NOT NULL DEFAULT ''",
            "acknowledged_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in message_column_sql.items():
            if column not in message_columns:
                connection.execute(
                    f"ALTER TABLE hdhive_message_log ADD COLUMN {column} {definition}"
                )
        if added_message_status:
            connection.execute(
                "UPDATE hdhive_message_log SET status = 'acknowledged', "
                "processed_at = CASE WHEN processed_at = '' THEN created_at ELSE processed_at END, "
                "acknowledged_at = CASE WHEN acknowledged_at = '' THEN created_at ELSE acknowledged_at END "
                "WHERE status = 'pending' AND attempt_count = 0"
            )
        oauth_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(hdhive_oauth)").fetchall()
        }
        if "authorized_scopes" not in oauth_columns:
            connection.execute(
                "ALTER TABLE hdhive_oauth "
                "ADD COLUMN authorized_scopes TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                "UPDATE hdhive_oauth SET authorized_scopes = scopes "
                "WHERE access_token_cipher != ''"
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
        for key in ("emby_webhook_token", "p123_emby_webhook_token"):
            connection.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, secrets.token_urlsafe(24)),
            )


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


HDHIVE_SCOPES = "meta query unlock write vip subscription messages"


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
            "refresh_token_cipher = ?, authorized_scopes = ?, token_expires_at = ?, "
            "authorized_at = ?, status = 'connected', last_error = '', updated_at = ? "
            "WHERE id = 1",
            (
                encrypt_secret(tokens.access_token),
                encrypt_secret(refresh),
                " ".join(tokens.scopes)
                or current["authorized_scopes"]
                or current["scopes"],
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
            scope = f"（限制范围：{error.limit_scope}）" if error.limit_scope else ""
            headers = (
                {"Retry-After": str(error.retry_after)}
                if error.retry_after > 0
                else None
            )
            raise HTTPException(
                429,
                f"影巢调用已达到限制{scope}{wait}",
                headers=headers,
            ) from error
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


def p115_error_detail(payload: dict[str, Any], fallback: str) -> str:
    message = response_message(payload, fallback).strip() or fallback
    summary = response_summary(payload)
    if summary == "无状态字段" or summary in message:
        return message
    return f"{message}；115返回：{summary}"


def p115_receive_was_duplicate(payload: dict[str, Any]) -> bool:
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("error", "error_msg", "message", "msg")
    ).lower()
    return any(
        marker in text
        for marker in ("已接收", "接收过", "已存在", "重复", "already", "duplicate")
    )


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


def wait_for_p115_change(
    snapshot: Any,
    before: set[Any],
    attempts: int = 10,
    interval_seconds: float = 1.5,
) -> bool:
    """Allow for 115's eventual consistency before deciding nothing changed."""
    for _ in range(max(1, int(attempts))):
        after = snapshot()
        if after - before:
            return True
        time.sleep(max(0.0, float(interval_seconds)))
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


def p115_item_parent_id(item: dict[str, Any]) -> str:
    return str(
        item.get("pid")
        or item.get("parent_id")
        or item.get("cid")
        or item.get("file_parent_id")
        or ""
    )


def recover_duplicate_p115_receive(
    client: Any,
    target_cid: str,
    selected: list[dict[str, Any]],
    before_files: set[Any],
) -> tuple[bool, str]:
    """Copy previously received matching files into the configured target."""
    recovered: list[dict[str, Any]] = []
    for source_item in selected:
        source_name = p115_share_item_name(source_item)
        source_sha1 = p115_share_item_sha1(source_item)
        source_size = p115_share_item_size(source_item)
        search_value = source_sha1 or source_name
        if not search_value:
            return False, "115报告资源已接收，但无法识别分享文件名称"
        result = p115_call(
            "搜索115中已接收文件失败",
            client.fs_search,
            {
                "search_value": search_value,
                "limit": 100,
                "show_dir": 0,
                "fc": 2,
            },
        )
        if not response_ok(result):
            return False, p115_error_detail(result, "无法搜索115中已接收文件")
        candidates = extract_share_items(result)
        matched = None
        for candidate in candidates:
            candidate_sha1 = p115_share_item_sha1(candidate)
            candidate_name = p115_share_item_name(candidate)
            candidate_size = p115_share_item_size(candidate)
            if source_sha1 and candidate_sha1 == source_sha1:
                matched = candidate
                break
            if (
                source_name
                and candidate_name == source_name
                and (not source_size or candidate_size == source_size)
            ):
                matched = candidate
                break
        if not matched:
            return False, f"115报告已接收，但全盘未找到对应文件：{source_name}"
        recovered.append(matched)

    to_copy = [
        item for item in recovered
        if p115_item_parent_id(item) != str(target_cid)
    ]
    if not to_copy:
        return True, "对应文件已经位于115目标目录"
    for item in to_copy:
        file_id = p115_share_item_id(item)
        if not file_id:
            return False, "找到已接收文件，但115未返回可复制的文件ID"
        copied = p115_call(
            "复制115中已接收文件失败",
            client.fs_copy,
            {"fid": file_id, "pid": target_cid},
        )
        if not response_ok(copied):
            return False, p115_error_detail(copied, "无法复制已接收文件到目标目录")
    if not wait_for_p115_change(
        lambda: p115_folder_snapshot(client, target_cid),
        before_files,
    ):
        return False, "已找到并复制115中的同一文件，但目标目录仍未显示变化"
    return True, "已从115中找回接收过的文件并复制到目标目录"


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
        status="submitted",
        detail=message,
        season_number=season_number,
        episode_numbers=episode_numbers or [],
    )
    send_notifications_async(
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


def select_largest_missing_episode_files_by_season(
    items: list[dict[str, Any]],
    missing_episode_keys: set[tuple[int, int]],
    fallback_season: int = 1,
) -> tuple[list[dict[str, Any]], set[tuple[int, int]]]:
    """Pick media and subtitle files without merging equal episode numbers across seasons."""

    media_extensions = {
        ".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".webm"
    }
    subtitle_extensions = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
    best_by_episode: dict[tuple[int, int], dict[str, Any]] = {}
    subtitles: list[tuple[dict[str, Any], set[tuple[int, int]]]] = []
    for item in items:
        if item.get("_share_is_dir"):
            continue
        name = str(item.get("_share_name") or "")
        parsed = parse_episode_spec(name)
        seasons = {
            int(value) for value in parsed.get("season_numbers") or [] if int(value) > 0
        }
        # A cross-season filename cannot safely map one flat episode list back
        # to its seasons, so leave it for manual handling.
        if len(seasons) > 1:
            continue
        season = next(iter(seasons), int(fallback_season or 1))
        keys = {
            (season, int(episode))
            for episode in parsed.get("episode_numbers") or []
            if (season, int(episode)) in missing_episode_keys
        }
        if not keys:
            continue
        suffix = Path(name).suffix.lower()
        if suffix in subtitle_extensions:
            subtitles.append((item, keys))
            continue
        if suffix and suffix not in media_extensions:
            continue
        size = p115_share_item_size(item)
        for key in keys:
            current = best_by_episode.get(key)
            if current is None or size > p115_share_item_size(current):
                best_by_episode[key] = item

    selected: dict[str, dict[str, Any]] = {}
    for item in best_by_episode.values():
        selected[str(item.get("_share_id") or id(item))] = item
    selected_keys = set(best_by_episode)
    for item, keys in subtitles:
        if keys & selected_keys:
            selected[str(item.get("_share_id") or id(item))] = item
    return list(selected.values()), selected_keys


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


def has_manual_transfer(
    tmdb_id: int,
    user_id: Optional[int] = None,
    destination: str = "",
) -> bool:
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
        if destination:
            query += " AND destination = ?"
            values.append(storage_destination(destination))
        return bool(connection.execute(query + " LIMIT 1", values).fetchone())


def has_initial_media_submission(
    tmdb_id: int,
    user_id: int,
    destination: str,
) -> bool:
    if tmdb_id <= 0 or user_id <= 0:
        return False
    with db() as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM media_workflow_jobs WHERE user_id = ? "
                "AND destination = ? AND tmdb_id = ? AND state IN ("
                "'submitted', 'transferred', 'organizing', "
                "'waiting_library', 'ingested') LIMIT 1",
                (
                    int(user_id), storage_destination(destination), int(tmdb_id)
                ),
            ).fetchone()
        )


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
    destination: str = "",
) -> None:
    numbers = episode_numbers or [0]
    with db() as connection:
        if not destination:
            row = connection.execute(
                "SELECT storage_destination FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            destination = storage_destination(
                row["storage_destination"] if row else "p115"
            )
        for episode_number in numbers:
            connection.execute(
                "INSERT OR IGNORE INTO resource_transfer_log("
                "user_id, follow_id, source, resource_key, destination, tmdb_id, "
                "season_number, episode_number, transfer_scope, status, "
                "detail, created_at, updated_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    follow_id,
                    source,
                    resource_key,
                    storage_destination(destination),
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


def workflow_job_key(
    *,
    user_id: int,
    destination: str,
    source: str,
    resource_key: str,
    tmdb_id: int,
    season_number: int = 0,
    episode_numbers: Optional[list[int]] = None,
    scope: str = "manual",
) -> str:
    material = "\0".join(
        (
            str(int(user_id)),
            storage_destination(destination),
            str(source or ""),
            str(resource_key or ""),
            str(int(tmdb_id or 0)),
            str(int(season_number or 0)),
            episode_numbers_json(episode_numbers or []),
            str(scope or "manual"),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def begin_workflow_job(
    *,
    user_id: int,
    destination: str,
    source: str,
    resource_key: str,
    tmdb_id: int,
    media_type: str,
    title: str,
    season_number: int = 0,
    episode_numbers: Optional[list[int]] = None,
    follow_id: Optional[int] = None,
    scope: str = "manual",
) -> sqlite3.Row:
    key = workflow_job_key(
        user_id=user_id,
        destination=destination,
        source=source,
        resource_key=resource_key,
        tmdb_id=tmdb_id,
        season_number=season_number,
        episode_numbers=episode_numbers,
        scope=scope,
    )
    timestamp = now_iso()
    with db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO media_workflow_jobs("
            "idempotency_key, user_id, follow_id, destination, source, "
            "resource_key, tmdb_id, media_type, season_number, "
            "episode_numbers_json, title, state, created_at, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)",
            (
                key, int(user_id), follow_id, storage_destination(destination),
                str(source), str(resource_key), int(tmdb_id or 0), str(media_type),
                int(season_number or 0), episode_numbers_json(episode_numbers or []),
                str(title or "")[:200], timestamp, timestamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM media_workflow_jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row and row["state"] in ("unlocking", "submitted", "transferred", "organizing", "waiting_library"):
            raise HTTPException(409, f"这个资源正在处理：{JOB_STATE_LABELS.get(row['state'], row['state'])}")
        if row and row["state"] == "ingested":
            raise HTTPException(409, "这个资源已经完成入库")
        if row and row["state"] == "failed" and row["next_retry_at"]:
            try:
                retry_at = datetime.fromisoformat(str(row["next_retry_at"]))
            except (TypeError, ValueError):
                retry_at = datetime.min.replace(tzinfo=timezone.utc)
            if retry_at > datetime.now(timezone.utc):
                raise HTTPException(409, f"这个资源等待重试：{row['last_error']}")
        connection.execute(
            "UPDATE media_workflow_jobs SET state = 'unlocking', last_error = '', "
            "next_retry_at = '', attempt_count = attempt_count + 1, updated_at = ? "
            "WHERE id = ?",
            (timestamp, int(row["id"])),
        )
        return connection.execute(
            "SELECT * FROM media_workflow_jobs WHERE id = ?", (int(row["id"]),)
        ).fetchone()


def update_workflow_job(
    job_id: int,
    state: str,
    detail: str = "",
    error: str = "",
    retry_seconds: int = 0,
) -> None:
    if state not in JOB_STATE_LABELS:
        raise ValueError("invalid workflow state")
    timestamp = now_iso()
    next_retry_at = (
        (datetime.now(timezone.utc) + timedelta(seconds=max(0, retry_seconds))).isoformat()
        if retry_seconds > 0 else ""
    )
    completed_at = timestamp if state == "ingested" else ""
    with db() as connection:
        connection.execute(
            "UPDATE media_workflow_jobs SET state = ?, detail = ?, last_error = ?, "
            "next_retry_at = ?, completed_at = CASE WHEN ? != '' THEN ? "
            "ELSE completed_at END, updated_at = ? WHERE id = ?",
            (
                state, str(detail or "")[:1000], str(error or "")[:1000],
                next_retry_at, completed_at, completed_at, timestamp, int(job_id),
            ),
        )


def update_workflow_job_episodes(
    job_id: int,
    season_number: int,
    episode_numbers: Iterable[int],
) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE media_workflow_jobs SET season_number = ?, "
            "episode_numbers_json = ?, updated_at = ? WHERE id = ?",
            (
                int(season_number or 0),
                episode_numbers_json(episode_numbers),
                now_iso(),
                int(job_id),
            ),
        )


def fail_workflow_job(job_id: int, error: Any, retry_seconds: int = 300) -> None:
    update_workflow_job(
        job_id,
        "failed",
        detail="处理失败，等待重试",
        error=str(error),
        retry_seconds=retry_seconds,
    )


def attach_workflow_job_to_request(request: Any, job_id: int) -> None:
    """Attach failure tracking for ASGI requests without breaking direct callers."""
    state = getattr(request, "state", None)
    if state is not None:
        state.workflow_job_id = int(job_id)


def recover_stale_workflow_jobs() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    timestamp = now_iso()
    with db() as connection:
        cursor = connection.execute(
            "UPDATE media_workflow_jobs SET state = 'failed', "
            "last_error = '处理进程中断，已恢复为可重试状态', "
            "next_retry_at = ?, updated_at = ? WHERE state = 'unlocking' "
            "AND updated_at < ?",
            (timestamp, timestamp, cutoff),
        )
        return int(cursor.rowcount or 0)


def log_hdhive_follow_event(
    stage: str,
    status: str,
    message: str,
    *,
    follow: Optional[sqlite3.Row] = None,
    follow_id: Optional[int] = None,
    user_id: Optional[int] = None,
    tmdb_id: int = 0,
    title: str = "",
    cycle_id: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Persist a user-facing resource-management event without affecting work."""
    try:
        if follow is not None:
            follow_id = int(follow["id"])
            user_id = int(follow["user_id"])
            tmdb_id = int(follow["tmdb_id"] or 0)
            title = str(follow["title"] or "")
        elif follow_id:
            with db() as connection:
                row = connection.execute(
                    "SELECT id, user_id, tmdb_id, title FROM tv_follows WHERE id = ?",
                    (int(follow_id),),
                ).fetchone()
            if row:
                user_id = int(row["user_id"])
                tmdb_id = int(row["tmdb_id"] or 0)
                title = str(row["title"] or "")
        with db() as connection:
            connection.execute(
                "INSERT INTO hdhive_follow_events("
                "cycle_id, follow_id, user_id, tmdb_id, title, stage, status, "
                "message, detail_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(cycle_id or ""),
                    follow_id,
                    user_id,
                    tmdb_id,
                    title,
                    str(stage or "info")[:40],
                    str(status or "info")[:20],
                    str(message or "")[:500],
                    json.dumps(detail or {}, ensure_ascii=False),
                    now_iso(),
                ),
            )
    except Exception:
        LOGGER.exception("failed to persist HDHive follow event")


def cleanup_hdhive_follow_events(keep: int = 5000) -> None:
    try:
        with db() as connection:
            connection.execute(
                "DELETE FROM hdhive_follow_events WHERE id NOT IN ("
                "SELECT id FROM hdhive_follow_events ORDER BY id DESC LIMIT ?)",
                (max(500, int(keep)),),
            )
    except Exception:
        LOGGER.exception("failed to prune HDHive follow events")


def management_resource_status(payload: dict[str, Any], has_follow: bool = False) -> str:
    """Normalize the completed/ongoing label stored with management events."""
    if has_follow:
        return "ongoing"
    value = str(payload.get("resource_status") or "").strip().lower()
    if value in {"ongoing", "completed"}:
        return value
    return "completed" if str(payload.get("media_type") or "") == "movie" else ""


def log_follow_library_event(
    destination: str,
    tmdb_id: int,
    message: str,
    *,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    if int(tmdb_id or 0) <= 0:
        return
    with db() as connection:
        follows = connection.execute(
            "SELECT f.* FROM tv_follows f JOIN users u ON u.id = f.user_id "
            "WHERE f.active = 1 AND f.tmdb_id = ? AND u.storage_destination = ?",
            (int(tmdb_id), storage_destination(destination)),
        ).fetchall()
    for follow in follows:
        log_hdhive_follow_event(
            "library", "success", message, follow=follow, detail=detail
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


def select_missing_episode_transfer_links(
    links: list[str],
    *,
    season_number: int,
    wanted_episodes: set[int],
    present_episodes: set[int],
) -> tuple[list[str], set[int]]:
    """Select only missing episodes when each offline URL names its episode."""
    if not links or not wanted_episodes:
        return links, set(wanted_episodes)

    parsed_links: list[tuple[str, set[int]]] = []
    available: set[int] = set()
    for link in links:
        parsed = parse_episode_spec(unquote(link))
        seasons = {
            int(value)
            for value in parsed.get("season_numbers") or []
            if int(value) > 0
        }
        if seasons and season_number not in seasons:
            episodes: set[int] = set()
        else:
            episodes = {
                int(value)
                for value in parsed.get("episode_numbers") or []
                if int(value) > 0
            } & wanted_episodes
        parsed_links.append((link, episodes))
        available.update(episodes)

    # Only filter when every requested episode can be mapped to at least one
    # URL. A partial/ambiguous payload must retain the original safe fallback.
    if not wanted_episodes.issubset(available):
        return links, set(wanted_episodes)

    missing = wanted_episodes - present_episodes
    if not missing:
        return [], set()
    selected = [
        link for link, episodes in parsed_links if episodes & missing
    ]
    return selected, missing


def offline_expected_files(
    links: Iterable[str], fallback_season: int = 1
) -> list[dict[str, Any]]:
    """Extract stable filename/size evidence from ED2K URLs."""
    expected: list[dict[str, Any]] = []
    for link in links:
        value = unquote(str(link or "").strip())
        if not value.lower().startswith("ed2k://|file|"):
            continue
        parts = value.split("|")
        if len(parts) < 5:
            continue
        name = parts[2].strip()
        try:
            size = max(0, int(parts[3]))
        except (TypeError, ValueError):
            size = 0
        parsed = parse_episode_spec(name)
        seasons = [
            int(item) for item in parsed.get("season_numbers") or [] if int(item) > 0
        ]
        episodes = [
            int(item) for item in parsed.get("episode_numbers") or [] if int(item) > 0
        ]
        expected.append({
            "name": name,
            "size": size,
            "season_number": seasons[0] if len(set(seasons)) == 1 else fallback_season,
            "episode_numbers": sorted(set(episodes)),
        })
    return expected


def register_p115_offline_monitor(
    *,
    workflow_job_id: int,
    user_id: int,
    follow_id: Optional[int],
    destination: str,
    source: str,
    resource_key: str,
    tmdb_id: int,
    media_type: str,
    season_number: int,
    episode_numbers: Iterable[int],
    title: str,
    target_cid: str,
    links: Iterable[str],
) -> None:
    link_list = [str(link) for link in links if str(link).strip()]
    expected = offline_expected_files(link_list, max(1, int(season_number or 1)))
    timestamp = now_iso()
    with db() as connection:
        connection.execute(
            "INSERT INTO p115_offline_monitors("
            "workflow_job_id, user_id, follow_id, destination, source, resource_key, "
            "tmdb_id, media_type, season_number, episode_numbers_json, title, "
            "target_cid, links_json, expected_files_json, status, created_at, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) "
            "ON CONFLICT(workflow_job_id) DO UPDATE SET follow_id = excluded.follow_id, "
            "target_cid = excluded.target_cid, links_json = excluded.links_json, "
            "expected_files_json = excluded.expected_files_json, status = 'pending', "
            "last_error = '', updated_at = excluded.updated_at",
            (
                int(workflow_job_id), int(user_id), follow_id,
                storage_destination(destination), source, resource_key, int(tmdb_id or 0),
                media_type, int(season_number or 0),
                episode_numbers_json(episode_numbers), title, str(target_cid or "0"),
                json.dumps(link_list, ensure_ascii=False),
                json.dumps(expected, ensure_ascii=False), timestamp, timestamp,
            ),
        )
    if follow_id and expected:
        seed_offline_wash_baseline(int(follow_id), expected)


def seed_offline_wash_baseline(
    follow_id: int, expected_files: Iterable[dict[str, Any]]
) -> None:
    config = hdhive_wash_config()
    timestamp = datetime.now(timezone.utc)
    closes_at = (timestamp + timedelta(hours=config["window_hours"])).isoformat()
    with db() as connection:
        for item in expected_files:
            file_name = str(item.get("name") or "")
            file_size = max(0, int(item.get("size") or 0))
            season = max(1, int(item.get("season_number") or 1))
            for episode in item.get("episode_numbers") or []:
                episode = int(episode or 0)
                if episode <= 0:
                    continue
                connection.execute(
                    "INSERT INTO hdhive_wash_episodes("
                    "follow_id, season_number, episode_number, opened_at, closes_at, "
                    "last_file_name, last_file_size, last_message, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(follow_id, season_number, episode_number) DO UPDATE SET "
                    "last_file_name = CASE WHEN excluded.last_file_size >= last_file_size "
                    "THEN excluded.last_file_name ELSE last_file_name END, "
                    "last_file_size = MAX(last_file_size, excluded.last_file_size), "
                    "last_message = excluded.last_message, updated_at = excluded.updated_at",
                    (
                        int(follow_id), season, episode, timestamp.isoformat(), closes_at,
                        file_name, file_size, "已用ED2K版本建立洗版大小基线",
                        timestamp.isoformat(),
                    ),
                )


def attach_pending_offline_monitors_to_follow(
    follow_id: int, user_id: int, tmdb_id: int
) -> None:
    with db() as connection:
        rows = connection.execute(
            "SELECT workflow_job_id, expected_files_json FROM p115_offline_monitors "
            "WHERE user_id = ? AND tmdb_id = ? AND status IN ('pending', 'completed')",
            (int(user_id), int(tmdb_id)),
        ).fetchall()
        connection.execute(
            "UPDATE p115_offline_monitors SET follow_id = ?, updated_at = ? "
            "WHERE user_id = ? AND tmdb_id = ? AND status IN ('pending', 'completed')",
            (int(follow_id), now_iso(), int(user_id), int(tmdb_id)),
        )
    for row in rows:
        try:
            expected = json.loads(row["expected_files_json"] or "[]")
        except (TypeError, ValueError):
            expected = []
        if isinstance(expected, list):
            seed_offline_wash_baseline(follow_id, expected)


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
    complete_words = bool(
        re.search(
            r"(全集|合集|全\s*\d+\s*集|(?<!第)\d+\s*集\s*(?:全|完结)|完结)",
            value,
            re.I,
        )
    )

    for match in re.finditer(r"全\s*(\d{1,4})\s*集", value, re.I):
        total = int(match.group(1))
        if 0 < total <= 10000:
            episodes.update(range(1, total + 1))

    for match in re.finditer(r"(\d{1,4})\s*集\s*(?:全|完结)", value, re.I):
        if re.search(r"第\s*$", value[:match.start(1)]):
            continue
        total = int(match.group(1))
        if 0 < total <= 10000:
            episodes.update(range(1, total + 1))

    for pattern in (
        r"共\s*(\d{1,4})\s*集",
        r"更新至\s*(?:第\s*)?(\d{1,4})\s*集",
    ):
        for match in re.finditer(pattern, value, re.I):
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


def hdhive_resource_is_direct_115(resource: dict[str, Any]) -> bool:
    """Return whether HDHive can preview and directly receive this 115 share."""

    pan_type = str(
        resource.get("pan_type") or resource.get("share_type_label") or ""
    ).strip().lower()
    return "115" in pan_type and not bool(resource.get("is_offline"))


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
    parsed_numbers = parsed["episode_numbers"]
    provided_parsed = parse_episode_spec(provided_episode_label)
    if parsed_numbers and len(parsed_numbers) > len(existing_numbers):
        item["episode_numbers"] = parsed["episode_numbers"]
        item["episode_start"] = parsed["episode_start"]
        item["episode_end"] = parsed["episode_end"]
        item["season_number"] = parsed["season_number"]
        item["episode_label"] = (
            provided_episode_label
            if len(provided_parsed["episode_numbers"]) >= len(parsed_numbers)
            else parsed["episode_label"]
        )
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
        1 if resource.get("is_pack") or episode_count > 1 else 0,
        episode_count,
        resource_size_bytes(resource.get("size_gb")),
        1 if resource.get("is_official_group") else 0,
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


def p123_delivery_settings() -> dict[str, str]:
    with db() as connection:
        mode = setting(connection, "p123_delivery_mode") or "telegram"
        if mode not in ("telegram", "p115"):
            mode = "telegram"
        return {
            "mode": mode,
            "target_cid": setting(connection, "p123_staging_cid") or "0",
            "target_name": setting(connection, "p123_staging_name") or "根目录",
        }


def uses_p115_delivery(user: dict[str, Any]) -> bool:
    return (
        storage_destination(user.get("storage_destination")) == "p115"
        or p123_delivery_settings()["mode"] == "p115"
    )


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


def update_follow_progress_pair(
    connection: sqlite3.Connection,
    follow_id: int,
    prefix: str,
    season_number: int,
    episode_number: int,
) -> None:
    """Advance a season/episode pair atomically instead of maximizing each column."""

    if prefix not in ("last_seen", "last_transferred"):
        raise ValueError("invalid follow progress prefix")
    season_column = f"{prefix}_season"
    episode_column = f"{prefix}_episode"
    connection.execute(
        f"UPDATE tv_follows SET {episode_column} = CASE "
        f"WHEN ? > {season_column} THEN ? "
        f"WHEN ? = {season_column} THEN MAX({episode_column}, ?) "
        f"ELSE {episode_column} END, "
        f"{season_column} = MAX({season_column}, ?) WHERE id = ?",
        (
            int(season_number),
            int(episode_number),
            int(season_number),
            int(episode_number),
            int(season_number),
            int(follow_id),
        ),
    )


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


def integer_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def emby_item_tmdb_id(item: dict[str, Any]) -> int:
    provider_ids = item.get("ProviderIds") or {}
    return integer_value(provider_ids.get("Tmdb") or provider_ids.get("TMDB"))


def emby_library_notification_enabled(destination: str) -> bool:
    key = (
        "p123_emby_library_notification_enabled"
        if storage_destination(destination) == "p123"
        else "emby_library_notification_enabled"
    )
    with db() as connection:
        return setting(connection, key) != "0"


def emby_webhook_enabled(destination: str) -> bool:
    key = (
        "p123_emby_webhook_enabled"
        if storage_destination(destination) == "p123"
        else "emby_webhook_enabled"
    )
    with db() as connection:
        return setting(connection, key) == "1"


def emby_webhook_token(destination: str) -> str:
    key = (
        "p123_emby_webhook_token"
        if storage_destination(destination) == "p123"
        else "emby_webhook_token"
    )
    with db() as connection:
        return setting(connection, key)


def emby_webhook_url(destination: str, site_url: str = "") -> str:
    current = storage_destination(destination)
    token = emby_webhook_token(current)
    if not site_url:
        with db() as connection:
            site_url = setting(connection, "site_public_url") or "https://qp.weige1999.xin"
    return (
        f"{site_url.rstrip('/')}/api/emby-webhook/{current}/"
        f"{quote(token, safe='')}"
    )


def emby_recent_library_items(
    destination: str = "p115",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read only the newest Emby records used by the library notification monitor."""

    base_url, api_key = emby_credentials(destination)
    if not base_url or not api_key:
        return []
    try:
        response = requests.get(
            emby_api_url(base_url, "/Items"),
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": (
                    "ProviderIds,SeriesId,SeriesName,ParentIndexNumber,IndexNumber,"
                    "DateCreated,Overview,Genres,CommunityRating,ProductionYear"
                ),
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": max(1, min(int(limit), 500)),
            },
            timeout=20,
        )
        response.raise_for_status()
        return [
            item for item in response.json().get("Items", [])
            if isinstance(item, dict) and str(item.get("Id") or "")
        ]
    except (requests.RequestException, ValueError, TypeError):
        return []


def emby_library_item(
    destination: str,
    item_id: str,
) -> dict[str, Any]:
    base_url, api_key = emby_credentials(destination)
    if not base_url or not api_key or not item_id:
        return {}
    try:
        response = requests.get(
            emby_api_url(base_url, f"/Items/{quote(item_id, safe='')}"),
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            params={
                "Fields": (
                    "ProviderIds,Overview,Genres,CommunityRating,ProductionYear,"
                    "SeriesId,SeriesName,ParentIndexNumber,IndexNumber,DateCreated"
                )
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}
    except (requests.RequestException, ValueError, TypeError):
        return {}


def emby_episode_range(items: list[dict[str, Any]]) -> str:
    seasons: dict[int, set[int]] = {}
    for item in items:
        season = integer_value(item.get("ParentIndexNumber"))
        episode = integer_value(item.get("IndexNumber"))
        if season > 0 and episode > 0:
            seasons.setdefault(season, set()).add(episode)
    labels = []
    for season, values in sorted(seasons.items()):
        numbers = sorted(values)
        ranges: list[tuple[int, int]] = []
        for number in numbers:
            if not ranges or number > ranges[-1][1] + 1:
                ranges.append((number, number))
            else:
                ranges[-1] = (ranges[-1][0], number)
        episode_text = "、".join(
            f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}"
            for start, end in ranges
        )
        labels.append(f"S{season:02d} {episode_text}")
    return " / ".join(labels)


def emby_episode_season_groups(
    items: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    seasons: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        seasons.setdefault(integer_value(item.get("ParentIndexNumber")), []).append(item)
    return [seasons[number] for number in sorted(seasons)]


def tmdb_library_metadata(media_type: str, tmdb_id: int) -> dict[str, Any]:
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        return {}
    try:
        return tmdb_get(f"/{media_type}/{tmdb_id}", {"language": "zh-CN"})
    except (HTTPException, requests.RequestException, ValueError, TypeError):
        return {}


def library_notification_asset_url(path: str) -> str:
    with db() as connection:
        site_url = setting(connection, "site_public_url") or "https://qp.weige1999.xin"
    return f"{site_url.rstrip('/')}/{path.lstrip('/')}"


def cache_emby_library_image(
    destination: str,
    item: dict[str, Any],
) -> str:
    item_id = str(item.get("Id") or "")
    base_url, api_key = emby_credentials(destination)
    if not item_id or not base_url or not api_key:
        return ""
    cache_dir = DATA_DIR / "library-notification-images"
    candidates = ("Backdrop/0", "Primary")
    for image_type in candidates:
        cache_key = hashlib.sha256(
            f"{storage_destination(destination)}|{base_url}|{item_id}|{image_type}".encode()
        ).hexdigest()
        existing = next(cache_dir.glob(f"{cache_key}.*"), None) if cache_dir.exists() else None
        if existing and existing.is_file():
            return library_notification_asset_url(
                f"/api/library-notification-image/{existing.name}"
            )
        try:
            response = requests.get(
                emby_api_url(
                    base_url,
                    f"/Items/{quote(item_id, safe='')}/Images/{image_type}",
                ),
                headers={"X-Emby-Token": api_key, "Accept": "image/*"},
                params={"maxWidth": 1280, "quality": 90},
                timeout=20,
            )
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/") or len(response.content) < 100:
                continue
            suffix = (
                ".png" if "png" in content_type
                else ".webp" if "webp" in content_type
                else ".jpg"
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / f"{cache_key}{suffix}"
            cache_path.write_bytes(response.content)
            files = sorted(
                (path for path in cache_dir.iterdir() if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for stale_path in files[500:]:
                try:
                    stale_path.unlink()
                except OSError:
                    pass
            return library_notification_asset_url(
                f"/api/library-notification-image/{cache_path.name}"
            )
        except (requests.RequestException, OSError):
            continue
    return ""


def mp_library_notification(
    item: dict[str, Any],
    media_type: str,
    season_episode: str = "",
    destination: str = "p115",
) -> tuple[str, str]:
    tmdb_id = emby_item_tmdb_id(item)
    metadata = tmdb_library_metadata(media_type, tmdb_id)
    title = (
        metadata.get("title") or metadata.get("name")
        or item.get("SeriesName") or item.get("Name") or "未命名"
    )
    release_date = metadata.get("release_date") or metadata.get("first_air_date") or ""
    year = str(release_date)[:4] or str(
        metadata.get("production_year") or item.get("ProductionYear") or ""
    )[:4]
    rating_value = metadata.get("vote_average")
    if rating_value in (None, ""):
        rating_value = item.get("CommunityRating")
    try:
        rating = f"{float(rating_value):.1f}"
    except (TypeError, ValueError):
        rating = "暂无"

    type_name = "电影" if media_type == "movie" else "电视剧"
    countries = {str(value) for value in (metadata.get("origin_country") or [])}
    country_labels = {
        "movie": (
            ({"CN", "HK", "TW"}, "华语电影"),
            ({"JP", "KR"}, "日韩电影"),
            ({"US", "GB", "CA", "AU", "FR", "DE"}, "欧美电影"),
        ),
        "tv": (
            ({"CN"}, "国产剧"),
            ({"HK", "TW"}, "港台剧"),
            ({"JP", "KR"}, "日韩剧"),
            ({"US", "GB", "CA", "AU", "FR", "DE"}, "欧美剧"),
            ({"TH"}, "泰剧"),
        ),
    }
    category = next(
        (label for codes, label in country_labels[media_type] if countries & codes),
        "",
    )
    content_type = " · ".join(value for value in (type_name, category) if value)
    overview = str(metadata.get("overview") or item.get("Overview") or "暂无简介").strip()
    overview = overview[:520] + ("…" if len(overview) > 520 else "")
    source_name = "123 Emby" if storage_destination(destination) == "p123" else "115 Emby"
    heading = f"📥 入库完成 | {source_name} · {title}"
    if year:
        heading += f" ({year})"
    if season_episode:
        heading += f" {season_episode}"
    caption = (
        f"{heading}\n"
        f"⭐ 综合评分 | {rating}\n"
        f"🎭 内容类型 | {content_type}\n"
        f"📜 内容描述 | {overview}"
    )
    image_path = metadata.get("backdrop_path") or metadata.get("poster_path") or ""
    image_url = (
        f"https://image.tmdb.org/t/p/w780/{str(image_path).lstrip('/')}"
        if image_path else ""
    )
    if not image_url:
        image_url = cache_emby_library_image(destination, item)
    if not image_url:
        image_url = library_notification_asset_url(
            "/api/library-notification-fallback.png"
        )
    return caption[:1024], image_url


def send_mp_library_notification(
    item: dict[str, Any],
    media_type: str,
    season_episode: str = "",
    destination: str = "p115",
) -> None:
    caption, image_url = mp_library_notification(
        item,
        media_type,
        season_episode,
        destination,
    )
    send_telegram_photo(caption, image_url)
    send_wecom_article(caption, image_url)


def sync_emby_library_notifications(
    destination: Optional[str] = None,
) -> dict[str, set[int]]:
    """Notify for newly observed Emby movies and episodes, independent of requests."""

    destinations = [storage_destination(destination)] if destination else ["p115", "p123"]
    notified: dict[str, set[int]] = {current: set() for current in destinations}
    for current in destinations:
        if not emby_library_notification_enabled(current):
            continue
        items = emby_recent_library_items(current)
        if not items:
            continue
        item_by_id = {str(item.get("Id")): item for item in items}
        item_ids = list(item_by_id)
        placeholders = ",".join("?" for _ in item_ids)
        timestamp = now_iso()
        with db() as connection:
            state = connection.execute(
                "SELECT initialized_at FROM emby_library_monitor_state WHERE destination = ?",
                (current,),
            ).fetchone()
            known = {
                str(row["item_id"])
                for row in connection.execute(
                    f"SELECT item_id FROM emby_library_snapshot "
                    f"WHERE destination = ? AND item_id IN ({placeholders})",
                    (current, *item_ids),
                ).fetchall()
            }
            new_items = [item for item_id, item in item_by_id.items() if item_id not in known]
            connection.executemany(
                "INSERT OR IGNORE INTO emby_library_snapshot("
                "destination, item_id, item_type, series_id, tmdb_id, season_number, "
                "episode_number, observed_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        current,
                        str(item.get("Id")),
                        str(item.get("Type") or ""),
                        str(item.get("SeriesId") or ""),
                        emby_item_tmdb_id(item),
                        integer_value(item.get("ParentIndexNumber")),
                        integer_value(item.get("IndexNumber")),
                        timestamp,
                    )
                    for item in items
                ],
            )
            connection.execute(
                "INSERT INTO emby_library_monitor_state(destination, initialized_at, last_checked_at) "
                "VALUES(?, ?, ?) ON CONFLICT(destination) DO UPDATE SET "
                "last_checked_at = excluded.last_checked_at",
                (current, timestamp, timestamp),
            )
        if state is None:
            continue

        for item in new_items:
            if str(item.get("Type") or "") != "Movie":
                continue
            tmdb_id = emby_item_tmdb_id(item)
            send_mp_library_notification(item, "movie", destination=current)
            log_follow_library_event(
                current, tmdb_id, "Emby 已确认电影入库",
                detail={"destination": current, "item_id": str(item.get("Id") or "")},
            )
            if tmdb_id > 0:
                notified[current].add(tmdb_id)

        episodes_by_series: dict[str, list[dict[str, Any]]] = {}
        for item in new_items:
            if str(item.get("Type") or "") == "Episode" and item.get("SeriesId"):
                episodes_by_series.setdefault(str(item["SeriesId"]), []).append(item)
        for series_id, episodes in episodes_by_series.items():
            series = item_by_id.get(series_id) or emby_library_item(current, series_id)
            if not series:
                series = dict(episodes[0])
            series.setdefault("SeriesName", episodes[0].get("SeriesName") or "")
            tmdb_id = emby_item_tmdb_id(series)
            for season_episodes in emby_episode_season_groups(episodes):
                episode_label = emby_episode_range(season_episodes)
                send_mp_library_notification(
                    series,
                    "tv",
                    episode_label,
                    destination=current,
                )
                log_follow_library_event(
                    current, tmdb_id, f"Emby 已确认入库：{episode_label}",
                    detail={
                        "destination": current,
                        "episode_numbers": sorted(
                            integer_value(item.get("IndexNumber"))
                            for item in season_episodes
                            if integer_value(item.get("IndexNumber")) > 0
                        ),
                    },
                )
            if tmdb_id > 0:
                notified[current].add(tmdb_id)
    return notified


def emby_webhook_item(payload: dict[str, Any]) -> tuple[str, str]:
    item = payload.get("Item") or payload.get("item") or {}
    if not isinstance(item, dict):
        item = {}
    item_id = str(
        item.get("Id") or item.get("id")
        or payload.get("ItemId") or payload.get("itemId") or ""
    ).strip()
    item_type = str(
        item.get("Type") or item.get("type")
        or payload.get("ItemType") or payload.get("itemType") or ""
    ).strip()
    return item_id, item_type


def parse_emby_webhook_payload(raw_body: bytes, content_type: str) -> dict[str, Any]:
    """Accept Emby's JSON and legacy form-data webhook formats."""

    if not raw_body:
        return {}
    content_type = str(content_type or "")
    content_type_lower = content_type.lower()
    candidates: list[str] = []
    if "multipart/form-data" in content_type_lower:
        try:
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
                + raw_body
            )
            for part in message.iter_parts():
                if part.get_param("name", header="content-disposition") == "data":
                    value = part.get_content()
                    candidates.append(
                        value.decode(errors="replace")
                        if isinstance(value, bytes) else str(value)
                    )
        except (AttributeError, TypeError, ValueError):
            pass
    elif "application/x-www-form-urlencoded" in content_type_lower:
        candidates.extend(parse_qs(raw_body.decode(errors="replace")).get("data", []))
    else:
        candidates.append(raw_body.decode(errors="replace"))

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def record_emby_webhook_notifications(
    destination: str,
    items: list[dict[str, Any]],
) -> None:
    if not items:
        return
    current = storage_destination(destination)
    timestamp = now_iso()
    with db() as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO emby_webhook_notifications("
            "destination, item_id, item_type, notified_at) VALUES(?, ?, ?, ?)",
            [
                (
                    current,
                    str(item.get("Id") or ""),
                    str(item.get("Type") or ""),
                    timestamp,
                )
                for item in items
                if str(item.get("Id") or "")
            ],
        )
        connection.executemany(
            "INSERT OR IGNORE INTO emby_library_snapshot("
            "destination, item_id, item_type, series_id, tmdb_id, season_number, "
            "episode_number, observed_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    current,
                    str(item.get("Id") or ""),
                    str(item.get("Type") or ""),
                    str(item.get("SeriesId") or ""),
                    emby_item_tmdb_id(item),
                    integer_value(item.get("ParentIndexNumber")),
                    integer_value(item.get("IndexNumber")),
                    timestamp,
                )
                for item in items
                if str(item.get("Id") or "")
            ],
        )


def sync_emby_webhook_notifications(
    destination: str,
    webhook_items: dict[str, str],
    lookup_attempts: int = 8,
    retry_delay_seconds: float = 2,
) -> tuple[set[int], int]:
    """Notify exact Emby items from webhook data, even if polling saw them first."""

    current = storage_destination(destination)
    if not webhook_items or not emby_library_notification_enabled(current):
        return set(), 0
    item_ids = list(webhook_items)
    placeholders = ",".join("?" for _ in item_ids)
    with db() as connection:
        already_notified = {
            str(row["item_id"])
            for row in connection.execute(
                f"SELECT item_id FROM emby_webhook_notifications "
                f"WHERE destination = ? AND item_id IN ({placeholders})",
                (current, *item_ids),
            ).fetchall()
        }

    pending = [item_id for item_id in item_ids if item_id not in already_notified]
    resolved: dict[str, dict[str, Any]] = {}
    attempts = max(1, min(10, int(lookup_attempts or 1)))
    for attempt in range(attempts):
        for item_id in list(pending):
            item = emby_library_item(current, item_id)
            if item:
                resolved[item_id] = item
                pending.remove(item_id)
        if not pending or attempt >= attempts - 1:
            break
        if retry_delay_seconds > 0:
            time.sleep(float(retry_delay_seconds))
    items = list(resolved.values())
    missing = list(pending)
    if missing:
        LOGGER.warning(
            "Emby webhook items unavailable destination=%s count=%d",
            current,
            len(missing),
        )

    notified_tmdb_ids: set[int] = set()
    notification_count = 0
    movies = [item for item in items if str(item.get("Type") or "") == "Movie"]
    episodes_by_series: dict[str, list[dict[str, Any]]] = {}
    series_items: dict[str, dict[str, Any]] = {}
    for item in items:
        item_type = str(item.get("Type") or "")
        if item_type == "Episode" and item.get("SeriesId"):
            episodes_by_series.setdefault(str(item["SeriesId"]), []).append(item)
        elif item_type == "Series":
            series_items[str(item.get("Id") or "")] = item
        elif item_type == "Season" and item.get("SeriesId"):
            series_id = str(item["SeriesId"])
            series = emby_library_item(current, series_id)
            if series:
                series_items[series_id] = series

    for item in movies:
        send_mp_library_notification(item, "movie", destination=current)
        notification_count += 1
        tmdb_id = emby_item_tmdb_id(item)
        log_follow_library_event(
            current, tmdb_id, "Emby Webhook 已确认电影入库",
            detail={"destination": current, "item_id": str(item.get("Id") or "")},
        )
        if tmdb_id > 0:
            notified_tmdb_ids.add(tmdb_id)
        record_emby_webhook_notifications(current, [item])

    processed_series: set[str] = set()
    for series_id, episodes in episodes_by_series.items():
        series = series_items.get(series_id) or emby_library_item(current, series_id)
        if not series:
            series = dict(episodes[0])
        series.setdefault("SeriesName", episodes[0].get("SeriesName") or "")
        tmdb_id = emby_item_tmdb_id(series)
        for season_episodes in emby_episode_season_groups(episodes):
            episode_label = emby_episode_range(season_episodes)
            send_mp_library_notification(
                series,
                "tv",
                episode_label,
                destination=current,
            )
            notification_count += 1
            log_follow_library_event(
                current, tmdb_id, f"Emby Webhook 已确认入库：{episode_label}",
                detail={
                    "destination": current,
                    "episode_numbers": sorted(
                        integer_value(item.get("IndexNumber"))
                        for item in season_episodes
                        if integer_value(item.get("IndexNumber")) > 0
                    ),
                },
            )
        if tmdb_id > 0:
            notified_tmdb_ids.add(tmdb_id)
        recorded_items = list(episodes)
        if series_id in series_items:
            recorded_items.append(series_items[series_id])
        record_emby_webhook_notifications(current, recorded_items)
        processed_series.add(series_id)

    record_emby_webhook_notifications(
        current,
        [
            series for series_id, series in series_items.items()
            if series_id not in processed_series
        ],
    )

    unsupported = [
        item for item in items
        if str(item.get("Type") or "") not in ("Movie", "Episode", "Series")
    ]
    record_emby_webhook_notifications(current, unsupported)
    return notified_tmdb_ids, notification_count


def process_emby_webhook(destination: str, delay_seconds: float = 1) -> None:
    current = storage_destination(destination)
    try:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        while True:
            with EMBY_WEBHOOK_LOCK:
                generation = EMBY_WEBHOOK_GENERATIONS.get(current, 0)
                webhook_items = dict(EMBY_WEBHOOK_ITEMS.get(current, {}))
                EMBY_WEBHOOK_ITEMS[current] = {}
            exact_ids, exact_count = sync_emby_webhook_notifications(
                current,
                webhook_items,
            )
            polled = sync_emby_library_notifications(current)
            notified = set(polled.get(current, set())) | exact_ids
            sync_emby_requests(
                destination=current,
                suppress_notifications={current: notified},
            )
            LOGGER.info(
                "Emby webhook processed destination=%s items=%d notifications=%d",
                current,
                len(webhook_items),
                exact_count,
            )
            with EMBY_WEBHOOK_LOCK:
                if EMBY_WEBHOOK_GENERATIONS.get(current, 0) == generation:
                    EMBY_WEBHOOK_PENDING.discard(current)
                    EMBY_WEBHOOK_ITEMS.pop(current, None)
                    break
    except Exception:
        LOGGER.exception("Emby webhook processing failed destination=%s", current)
        with EMBY_WEBHOOK_LOCK:
            EMBY_WEBHOOK_PENDING.discard(current)


def queue_emby_webhook(
    destination: str,
    item_id: str = "",
    item_type: str = "",
) -> bool:
    current = storage_destination(destination)
    with EMBY_WEBHOOK_LOCK:
        EMBY_WEBHOOK_GENERATIONS[current] = EMBY_WEBHOOK_GENERATIONS.get(current, 0) + 1
        if item_id:
            EMBY_WEBHOOK_ITEMS.setdefault(current, {})[str(item_id)] = str(item_type or "")
        if current in EMBY_WEBHOOK_PENDING:
            return False
        EMBY_WEBHOOK_PENDING.add(current)
    Thread(
        target=process_emby_webhook,
        args=(current,),
        name=f"emby-webhook-{current}",
        daemon=True,
    ).start()
    return True


def complete_workflow_jobs_from_library(
    destination: str,
    library_tmdb_ids: set[int],
) -> int:
    current = storage_destination(destination)
    if not library_tmdb_ids:
        return 0
    marks = ",".join("?" for _ in library_tmdb_ids)
    with db() as connection:
        rows = connection.execute(
            f"SELECT * FROM media_workflow_jobs WHERE destination = ? "
            f"AND tmdb_id IN ({marks}) AND state IN ("
            f"'submitted', 'transferred', 'organizing', 'waiting_library', 'failed')",
            (current, *sorted(library_tmdb_ids)),
        ).fetchall()
    completed = 0
    progress_cache: dict[int, dict[str, Any]] = {}
    for row in rows:
        episodes = episode_numbers_from_json(row["episode_numbers_json"])
        if str(row["media_type"] or "") == "tv":
            season_number = int(row["season_number"] or 0)
            if not episodes:
                with db() as connection:
                    transfer_rows = connection.execute(
                        "SELECT season_number, episode_number FROM resource_transfer_log "
                        "WHERE destination = ? AND user_id = ? AND tmdb_id = ? "
                        "AND source = ? AND resource_key = ? "
                        "AND status IN ('submitted', 'success') AND episode_number > 0 "
                        "ORDER BY id",
                        (
                            current,
                            int(row["user_id"]),
                            int(row["tmdb_id"]),
                            str(row["source"] or ""),
                            str(row["resource_key"] or ""),
                        ),
                    ).fetchall()
                recovered_seasons = {
                    int(item["season_number"] or 0) for item in transfer_rows
                    if int(item["season_number"] or 0) > 0
                }
                recovered_episodes = {
                    int(item["episode_number"] or 0) for item in transfer_rows
                    if int(item["episode_number"] or 0) > 0
                }
                if recovered_episodes and len(recovered_seasons) == 1:
                    season_number = next(iter(recovered_seasons))
                    episodes = sorted(recovered_episodes)
                    update_workflow_job_episodes(
                        int(row["id"]), season_number, episodes
                    )
            # A series-level TMDB match only proves that the show exists in
            # Emby. It does not prove an unknown episode/package was ingested.
            if not episodes:
                continue
            if season_number <= 0:
                continue
            tmdb_id = int(row["tmdb_id"])
            if tmdb_id not in progress_cache:
                progress_cache[tmdb_id] = destination_episode_progress(
                    current,
                    tmdb_id,
                    known_in_library=True,
                    force=True,
                )
            by_season = progress_cache[tmdb_id].get("emby_episode_numbers") or {}
            present = {
                int(value)
                for value in by_season.get(str(season_number), [])
                if int(value) > 0
            }
            if not set(episodes).issubset(present):
                continue
        update_workflow_job(int(row["id"]), "ingested", "Emby 已确认入库")
        completed += 1
    return completed


def sync_emby_requests(
    force: bool = False,
    destination: Optional[str] = None,
    suppress_notifications: Optional[dict[str, set[int]]] = None,
) -> int:
    destinations = [storage_destination(destination)] if destination else ["p115", "p123"]
    completed = 0
    for current in destinations:
        tmdb_ids = emby_library_tmdb_ids(force=force, destination=current)
        if not tmdb_ids:
            continue
        complete_workflow_jobs_from_library(current, tmdb_ids)
        placeholders = ",".join("?" for _ in tmdb_ids)
        with db() as connection:
            rows = connection.execute(
                f"SELECT r.id, r.tmdb_id, r.title, r.year, u.display_name "
                f"FROM movie_requests r JOIN users u ON u.id = r.user_id "
                f"WHERE r.tmdb_id IN ({placeholders}) "
                f"AND u.storage_destination = ? AND r.status != 'available'",
                (*tmdb_ids, current),
            ).fetchall()
            if rows:
                ids = [int(row["id"]) for row in rows]
                marks = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE movie_requests SET status = 'available', "
                    f"completed_at = ?, updated_at = ? WHERE id IN ({marks})",
                    (now_iso(), now_iso(), *ids),
                )
        suppressed = (suppress_notifications or {}).get(current, set())
        for row in rows:
            if int(row["tmdb_id"]) in suppressed:
                continue
            send_notifications(
                f"✅ 求片已入库 · {'123' if current == 'p123' else '115'} Emby\n\n"
                f"{row['title']} ({row['year']})\n申请人：{row['display_name']}"
            )
        completed += len(rows)
    return completed


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
        user_row = connection.execute(
            "SELECT storage_destination FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        destination = storage_destination(
            user_row["storage_destination"] if user_row else "p115"
        )
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
                "AND status = 'success' AND tmdb_id > 0 AND destination = ?",
                (destination,),
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
        "poster_url": tmdb_image_proxy_url(poster_path, "w342"),
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


def send_telegram_photo(caption: str, image_url: str) -> None:
    with db() as connection:
        chat_id = setting(connection, "telegram_chat_id")
    if not chat_id:
        return
    if image_url:
        result = telegram_request(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": image_url,
                "caption": str(caption)[:1024],
            },
        )
        if result:
            return
    send_telegram(caption)


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


def send_wecom_article(text: str, image_url: str, to_user: str = "") -> bool:
    """Send the same image-first MP-style card to WeCom, with text fallback."""

    with db() as connection:
        agent_id = setting(connection, "wecom_agent_id")
        recipient = to_user or setting(connection, "wecom_to_user") or "@all"
        site_url = setting(connection, "site_public_url") or "https://qp.weige1999.xin"
    if not agent_id:
        return False
    if not image_url:
        return send_wecom(text, to_user)
    title, _, description = str(text).partition("\n")
    result = wecom_request(
        "/cgi-bin/message/send",
        {
            "touser": recipient,
            "msgtype": "news",
            "agentid": int(agent_id),
            "news": {
                "articles": [
                    {
                        "title": title[:128],
                        "description": description[:512],
                        "url": site_url,
                        "picurl": image_url,
                    }
                ]
            },
            "safe": 0,
        },
    )
    if int(result.get("errcode", -1)) == 0:
        return True
    return send_wecom(text, to_user)


def send_notifications(text: str) -> None:
    send_telegram(text)
    send_wecom(text)


def enqueue_notifications(text: str, dedupe_key: str = "") -> int:
    """Persist optional notifications so container restarts cannot lose them."""

    timestamp = now_iso()
    queued = 0
    with db() as connection:
        channels: list[tuple[str, str]] = []
        if setting(connection, "telegram_token") and setting(
            connection, "telegram_chat_id"
        ):
            channels.append(("telegram", setting(connection, "telegram_chat_id")))
        if setting(connection, "wecom_agent_id"):
            channels.append(
                ("wecom", setting(connection, "wecom_to_user") or "@all")
            )
        for channel, recipient in channels:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO notification_outbox("
                "dedupe_key, channel, recipient, payload, status, "
                "created_at, updated_at) VALUES(?, ?, ?, ?, 'pending', ?, ?)",
                (
                    str(dedupe_key or ""), channel, recipient,
                    str(text)[:4000], timestamp, timestamp,
                ),
            )
            queued += int(cursor.rowcount or 0)
    return queued


def process_notification_outbox(limit: int = 20) -> dict[str, int]:
    timestamp = now_iso()
    with db() as connection:
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat()
        connection.execute(
            "UPDATE notification_outbox SET status = 'failed', "
            "last_error = CASE WHEN last_error = '' THEN "
            "'发送进程中断，已恢复等待重试' ELSE last_error END, "
            "next_retry_at = '', updated_at = ? "
            "WHERE status = 'sending' AND updated_at < ?",
            (timestamp, stale_cutoff),
        )
        rows = connection.execute(
            "SELECT * FROM notification_outbox WHERE status IN ('pending', 'failed') "
            "AND (next_retry_at = '' OR next_retry_at <= ?) "
            "ORDER BY id LIMIT ?",
            (timestamp, max(1, min(100, int(limit)))),
        ).fetchall()
        claimed: list[sqlite3.Row] = []
        for row in rows:
            cursor = connection.execute(
                "UPDATE notification_outbox SET status = 'sending', updated_at = ? "
                "WHERE id = ? AND status IN ('pending', 'failed')",
                (timestamp, int(row["id"])),
            )
            if int(cursor.rowcount or 0):
                claimed.append(row)
    sent = failed = 0
    for row in claimed:
        try:
            if row["channel"] == "telegram":
                result = telegram_request(
                    "sendMessage",
                    {
                        "chat_id": row["recipient"],
                        "text": row["payload"],
                        "reply_markup": {"remove_keyboard": True},
                    },
                )
                if not result:
                    raise RuntimeError("Telegram 未确认发送成功")
            elif row["channel"] == "wecom":
                if not send_wecom(str(row["payload"]), str(row["recipient"])):
                    raise RuntimeError("企业微信未确认发送成功")
            else:
                raise RuntimeError("未知通知渠道")
            with db() as connection:
                connection.execute(
                    "UPDATE notification_outbox SET status = 'sent', sent_at = ?, "
                    "last_error = '', next_retry_at = '', updated_at = ? WHERE id = ?",
                    (now_iso(), now_iso(), int(row["id"])),
                )
            sent += 1
        except Exception as error:
            attempts = int(row["attempt_count"] or 0) + 1
            delay = min(6 * 3600, 60 * (2 ** min(attempts - 1, 8)))
            retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            with db() as connection:
                connection.execute(
                    "UPDATE notification_outbox SET status = 'failed', "
                    "attempt_count = ?, next_retry_at = ?, last_error = ?, "
                    "updated_at = ? WHERE id = ?",
                    (attempts, retry_at, str(error)[:500], now_iso(), int(row["id"])),
                )
            failed += 1
    return {"sent": sent, "failed": failed}


def send_notifications_async(text: str) -> None:
    enqueue_notifications(text)


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


def update_worker_health(
    worker: str,
    status: str,
    *,
    error: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    timestamp = now_iso()
    started = timestamp if status == "running" else ""
    succeeded = timestamp if status == "ok" else ""
    failed = timestamp if status == "error" else ""
    with db() as connection:
        connection.execute(
            "INSERT INTO worker_health(worker, status, last_started_at, "
            "last_success_at, last_error_at, last_error, detail_json, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(worker) DO UPDATE SET "
            "status = excluded.status, "
            "last_started_at = CASE WHEN excluded.last_started_at != '' "
            "THEN excluded.last_started_at ELSE worker_health.last_started_at END, "
            "last_success_at = CASE WHEN excluded.last_success_at != '' "
            "THEN excluded.last_success_at ELSE worker_health.last_success_at END, "
            "last_error_at = CASE WHEN excluded.last_error_at != '' "
            "THEN excluded.last_error_at ELSE worker_health.last_error_at END, "
            "last_error = excluded.last_error, detail_json = excluded.detail_json, "
            "updated_at = excluded.updated_at",
            (
                worker, status, started, succeeded, failed, str(error)[:500],
                json.dumps(detail or {}, ensure_ascii=False), timestamp,
            ),
        )


def telegram_poll_loop() -> None:
    global TELEGRAM_OFFSET
    while True:
        with db() as connection:
            token = setting(connection, "telegram_token")
            allowed_chat = setting(connection, "telegram_chat_id")
        if not token or not allowed_chat:
            update_worker_health("telegram", "idle", detail={"configured": False})
            time.sleep(5)
            continue
        try:
            update_worker_health("telegram", "running")
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
            update_worker_health("telegram", "ok")
            if not data:
                time.sleep(3)
        except Exception as error:
            update_worker_health("telegram", "error", error=str(error))
            LOGGER.exception("telegram polling failed")
            time.sleep(5)


def emby_sync_loop() -> None:
    while True:
        try:
            update_worker_health("emby_sync", "running")
            notified = sync_emby_library_notifications()
            sync_emby_requests(suppress_notifications=notified)
            update_worker_health("emby_sync", "ok")
        except Exception as error:
            update_worker_health("emby_sync", "error", error=str(error))
            LOGGER.exception("Emby background sync failed")
        time.sleep(300)


def notification_outbox_loop() -> None:
    while True:
        try:
            update_worker_health("notifications", "running")
            result = process_notification_outbox()
            update_worker_health("notifications", "ok", detail=result)
        except Exception as error:
            update_worker_health("notifications", "error", error=str(error))
            LOGGER.exception("notification outbox failed")
        time.sleep(10)


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
            update_worker_health("dian_signin", "ok")
        except Exception as error:
            update_worker_health("dian_signin", "error", error=str(error))
            LOGGER.exception("Dian sign-in worker failed")
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


def hdhive_wash_config() -> dict[str, Any]:
    with db() as connection:
        return {
            "enabled": setting(connection, "hdhive_auto_transfer") != "0",
            "window_hours": max(
                12,
                min(
                    72,
                    int(setting(connection, "hdhive_wash_window_hours") or 48),
                ),
            ),
            "wash_after_emby": setting(connection, "hdhive_wash_after_emby") != "0",
            "reprocess_changed": setting(connection, "hdhive_reprocess_changed") != "0",
            "max_transfers": max(
                1,
                min(
                    10,
                    int(setting(connection, "hdhive_max_episode_transfers") or 4),
                ),
            ),
            "lock_after_window": setting(connection, "hdhive_lock_after_window") != "0",
        }


def hdhive_file_episode_candidates(
    slug: str,
    result: dict[str, Any],
    fallback_season: int,
) -> dict[tuple[int, int], dict[str, Any]]:
    data = hdhive_response_data(result)
    if not isinstance(data, dict):
        return {}
    provider = str(data.get("provider") or data.get("list_type") or "").lower()
    if provider and "115" not in provider:
        return {}
    if str(data.get("result_type") or "").lower() == "validation":
        return {}
    if str(data.get("resource_validate_status") or "").lower() in {
        "invalid", "expired", "deleted", "failed"
    }:
        return {}
    files = data.get("files")
    if not isinstance(files, list):
        return {}
    video_extensions = {
        ".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".webm"
    }
    candidates: dict[tuple[int, int], dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or Path(str(item.get("path") or "")).name)
        suffix = str(item.get("extension") or Path(name).suffix).lower()
        if suffix and not suffix.startswith("."):
            suffix = f".{suffix}"
        if suffix and suffix not in video_extensions:
            continue
        parsed = parse_episode_spec(f"{item.get('path') or ''} {name}")
        season = int(parsed.get("season_number") or fallback_season or 1)
        size = resource_size_bytes(item.get("size"))
        for episode in parsed.get("episode_numbers") or []:
            episode = int(episode)
            if episode <= 0:
                continue
            fingerprint = hashlib.sha256(
                f"{slug}\0{season}\0{episode}\0{name}\0{size}".encode()
            ).hexdigest()
            candidate = {
                "slug": slug,
                "season_number": season,
                "episode_number": episode,
                "file_name": name,
                "file_size": size,
                "fingerprint": fingerprint,
            }
            current = candidates.get((season, episode))
            if current is None or size > int(current["file_size"]):
                candidates[(season, episode)] = candidate
    return candidates


def hdhive_cached_file_list(
    slug: str,
    *,
    force: bool = False,
) -> tuple[Optional[dict[str, Any]], bool, str]:
    """Read an HDHive file list with durable success and failure cooldowns."""

    slug = str(slug or "").strip()
    if not slug:
        return None, False, "资源缺少 slug"
    now = datetime.now(timezone.utc)
    with db() as connection:
        cached = connection.execute(
            "SELECT payload_json, status, error, expires_at "
            "FROM hdhive_file_list_cache WHERE slug = ?",
            (slug,),
        ).fetchone()
    if cached:
        try:
            fresh = datetime.fromisoformat(str(cached["expires_at"])) > now
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            if cached["status"] != "success":
                return None, True, str(cached["error"] or "资源文件列表暂不可用")
            if not force:
                try:
                    payload = json.loads(str(cached["payload_json"] or "{}"))
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    return payload, True, ""

    try:
        result = hdhive_call("resource_file_list", slug)
    except HTTPException as error:
        if error.status_code == 429 or error.status_code in (401, 403):
            raise
        permanent = error.status_code in (400, 404, 410, 422)
        ttl = (
            HDHIVE_INVALID_FILE_LIST_CACHE_SECONDS
            if permanent
            else HDHIVE_TRANSIENT_FILE_LIST_CACHE_SECONDS
        )
        message = str(error.detail or f"HTTP {error.status_code}")[:500]
        checked_at = now_iso()
        with db() as connection:
            connection.execute(
                "INSERT INTO hdhive_file_list_cache("
                "slug, payload_json, status, error, expires_at, updated_at"
                ") VALUES(?, '', ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET payload_json = '', "
                "status = excluded.status, error = excluded.error, "
                "expires_at = excluded.expires_at, updated_at = excluded.updated_at",
                (
                    slug,
                    "invalid" if permanent else "failed",
                    message,
                    (now + timedelta(seconds=ttl)).isoformat(),
                    checked_at,
                ),
            )
        return None, False, message

    checked_at = now_iso()
    with db() as connection:
        connection.execute(
            "INSERT INTO hdhive_file_list_cache("
            "slug, payload_json, status, error, expires_at, updated_at"
            ") VALUES(?, ?, 'success', '', ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET payload_json = excluded.payload_json, "
            "status = 'success', error = '', expires_at = excluded.expires_at, "
            "updated_at = excluded.updated_at",
            (
                slug,
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                (now + timedelta(seconds=HDHIVE_FILE_LIST_CACHE_SECONDS)).isoformat(),
                checked_at,
            ),
        )
        connection.execute(
            "DELETE FROM hdhive_file_list_cache WHERE expires_at < ?",
            ((now - timedelta(days=7)).isoformat(),),
        )
    return result, False, ""


def hdhive_wash_candidate_allowed(
    follow: sqlite3.Row,
    candidate: dict[str, Any],
    config: dict[str, Any],
    emby_present: set[tuple[int, int]],
) -> bool:
    follow_id = int(follow["id"])
    season = int(candidate["season_number"])
    episode = int(candidate["episode_number"])
    baseline = (
        int(follow["baseline_season"] or 1),
        int(follow["baseline_episode"] or 0),
    )
    now = datetime.now(timezone.utc)
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM hdhive_wash_episodes WHERE follow_id = ? "
            "AND season_number = ? AND episode_number = ?",
            (follow_id, season, episode),
        ).fetchone()
        if not config["wash_after_emby"] and (season, episode) in emby_present:
            return False
        if row is None:
            if (season, episode) <= baseline:
                return False
            opened_at = now.isoformat()
            closes_at = (now + timedelta(hours=config["window_hours"])).isoformat()
            connection.execute(
                "INSERT INTO hdhive_wash_episodes("
                "follow_id, season_number, episode_number, opened_at, closes_at, "
                "updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (follow_id, season, episode, opened_at, closes_at, opened_at),
            )
            process_count = 0
        else:
            baseline_size = int(row["last_file_size"] or 0)
            candidate_size = int(candidate.get("file_size") or 0)
            if baseline_size > 0 and candidate_size > 0 and candidate_size <= baseline_size:
                return False
            if row["locked_at"]:
                return False
            closes_at_value = datetime.fromisoformat(str(row["closes_at"]))
            if closes_at_value <= now:
                if config["lock_after_window"]:
                    connection.execute(
                        "UPDATE hdhive_wash_episodes SET locked_at = ?, "
                        "last_message = ?, updated_at = ? WHERE follow_id = ? "
                        "AND season_number = ? AND episode_number = ?",
                        (
                            now.isoformat(),
                            "洗版窗口已结束，后续候选已锁定",
                            now.isoformat(),
                            follow_id,
                            season,
                            episode,
                        ),
                    )
                    return False
                connection.execute(
                    "UPDATE hdhive_wash_episodes SET opened_at = ?, closes_at = ?, "
                    "process_count = 0, updated_at = ? WHERE follow_id = ? "
                    "AND season_number = ? AND episode_number = ?",
                    (
                        now.isoformat(),
                        (now + timedelta(hours=config["window_hours"])).isoformat(),
                        now.isoformat(),
                        follow_id,
                        season,
                        episode,
                    ),
                )
                process_count = 0
            else:
                process_count = int(row["process_count"] or 0)
        if process_count >= int(config["max_transfers"]):
            return False
        attempt = connection.execute(
            "SELECT status FROM hdhive_wash_attempts WHERE fingerprint = ?",
            (candidate["fingerprint"],),
        ).fetchone()
        if attempt and attempt["status"] == "success":
            return False
        if not config["reprocess_changed"]:
            same_resource = connection.execute(
                "SELECT 1 FROM hdhive_wash_attempts WHERE follow_id = ? "
                "AND season_number = ? AND episode_number = ? "
                "AND resource_slug = ? AND status = 'success' LIMIT 1",
                (follow_id, season, episode, candidate["slug"]),
            ).fetchone()
            if same_resource:
                return False
    return True


def auto_wash_hdhive_follow(
    follow_id: int,
    resources: list[dict[str, Any]],
    cycle_id: str = "",
    force_file_lists: bool = False,
) -> dict[str, Any]:
    config = hdhive_wash_config()
    if not config["enabled"]:
        return {"transferred": [], "message": "追更自动洗版已关闭"}
    with db() as connection:
        follow = connection.execute(
            "SELECT f.*, u.storage_destination FROM tv_follows f "
            "JOIN users u ON u.id = f.user_id WHERE f.id = ? AND f.active = 1",
            (follow_id,),
        ).fetchone()
        target_cid = setting(connection, "p115_target_cid") or "0"
    if not follow or follow["storage_destination"] != "p115":
        if follow:
            log_hdhive_follow_event(
                "transfer", "skipped", "当前追更不是115目标，已跳过自动转存",
                follow=follow, cycle_id=cycle_id,
            )
        return {"transferred": [], "message": "自动洗版仅处理115对应的追更"}

    progress = destination_episode_progress(
        "p115", int(follow["tmdb_id"]), known_in_library=True
    )
    emby_present = {
        (int(season), int(episode))
        for season, episodes in (progress.get("emby_episode_numbers") or {}).items()
        for episode in episodes
        if int(episode) > 0
    }
    fallback_season = int(
        progress.get("emby_latest_season_number")
        or follow["last_seen_season"]
        or follow["baseline_season"]
        or 1
    )
    prepared: list[tuple[dict[str, Any], dict[tuple[int, int], dict[str, Any]]]] = []
    direct_resources = [
        resource for resource in resources
        if hdhive_resource_is_direct_115(resource)
    ]
    for resource in sorted(
        direct_resources, key=hdhive_resource_priority, reverse=True
    )[:12]:
        slug = str(resource.get("slug") or "").strip()
        if not slug or not hdhive_resource_is_supported(resource):
            continue
        resource_title = str(resource.get("title") or slug)
        log_hdhive_follow_event(
            "file_list", "running", f"正在读取资源文件列表：{resource_title}",
            follow=follow, cycle_id=cycle_id, detail={"resource_slug": slug},
        )
        try:
            file_result, from_cache, file_error = hdhive_cached_file_list(
                slug, force=force_file_lists
            )
        except HTTPException:
            raise
        if file_result is None:
            log_hdhive_follow_event(
                "file_list", "skipped" if from_cache else "failed",
                (
                    f"资源文件列表已进入冷却：{resource_title} · {file_error}"
                    if from_cache
                    else f"资源文件列表读取失败：{resource_title} · {file_error}"
                ),
                follow=follow, cycle_id=cycle_id,
                detail={
                    "resource_slug": slug,
                    "cached": from_cache,
                    "error": file_error,
                },
            )
            continue
        candidates = hdhive_file_episode_candidates(slug, file_result, fallback_season)
        allowed = {
            key: candidate
            for key, candidate in candidates.items()
            if hdhive_wash_candidate_allowed(
                follow, candidate, config, emby_present
            )
        }
        log_hdhive_follow_event(
            "file_list", "success",
            (
                f"资源文件列表读取完成：识别 {len(candidates)} 集，"
                f"其中 {len(allowed)} 集需要处理"
            ),
            follow=follow, cycle_id=cycle_id,
            detail={
                "resource_slug": slug,
                "identified_count": len(candidates),
                "candidate_count": len(allowed),
                "cached": from_cache,
            },
        )
        if allowed:
            prepared.append((resource, allowed))

    if not prepared:
        return {"transferred": [], "message": "没有新的115洗版候选"}

    ranked_by_episode: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for _resource, candidates in prepared:
        for key, candidate in candidates.items():
            ranked_by_episode.setdefault(key, []).append(candidate)
    allowed_fingerprints: set[str] = set()
    with db() as connection:
        for (season, episode), candidates in ranked_by_episode.items():
            row = connection.execute(
                "SELECT process_count FROM hdhive_wash_episodes "
                "WHERE follow_id = ? AND season_number = ? AND episode_number = ?",
                (follow_id, season, episode),
            ).fetchone()
            remaining = max(
                0,
                int(config["max_transfers"])
                - int(row["process_count"] if row else 0),
            )
            candidates.sort(key=lambda value: int(value["file_size"]), reverse=True)
            allowed_fingerprints.update(
                candidate["fingerprint"] for candidate in candidates[:remaining]
            )
    prepared = [
        (
            resource,
            {
                key: candidate
                for key, candidate in candidates.items()
                if candidate["fingerprint"] in allowed_fingerprints
            },
        )
        for resource, candidates in prepared
    ]
    prepared = [(resource, candidates) for resource, candidates in prepared if candidates]

    client = p115_client()
    transferred: set[tuple[int, int]] = set()
    summaries: list[str] = []
    for resource, candidates in prepared:
        slug = str(resource.get("slug") or "")
        resource_title = str(resource.get("title") or slug)
        target_keys = set(candidates)
        target_episodes = {episode for _season, episode in target_keys}
        episode_label = compact_episode_numbers(target_episodes)
        try:
            job = begin_workflow_job(
                user_id=int(follow["user_id"]),
                destination="p115",
                source="hdhive",
                resource_key=slug,
                tmdb_id=int(follow["tmdb_id"]),
                media_type="tv",
                title=str(follow["title"] or resource_title),
                season_number=min(season for season, _episode in target_keys),
                episode_numbers=sorted(target_episodes),
                follow_id=follow_id,
                scope="auto_wash",
            )
        except HTTPException as error:
            log_hdhive_follow_event(
                "transfer", "skipped", str(error.detail),
                follow=follow, cycle_id=cycle_id,
                detail={"resource_slug": slug},
            )
            continue
        job_id = int(job["id"])
        log_hdhive_follow_event(
            "unlock", "running",
            f"正在解锁资源：{resource_title} · 目标第{episode_label}集",
            follow=follow, cycle_id=cycle_id,
            detail={"resource_slug": slug, "episodes": sorted(target_episodes)},
        )
        try:
            unlocked = hdhive_call("unlock", slug)
            data = hdhive_response_data(unlocked)
            share_url = (
                str(data.get("full_url") or data.get("url") or "").strip()
                if isinstance(data, dict)
                else ""
            )
            if not is_115_share_url(share_url):
                fail_workflow_job(job_id, "资源未返回有效115链接", retry_seconds=21600)
                log_hdhive_follow_event(
                    "unlock", "failed", "资源已解锁，但没有返回有效的115链接",
                    follow=follow, cycle_id=cycle_id,
                    detail={"resource_slug": slug},
                )
                continue
            log_hdhive_follow_event(
                "unlock", "success",
                f"资源解锁成功，正在检查115分享中的第{episode_label}集",
                follow=follow, cycle_id=cycle_id,
                detail={"resource_slug": slug},
            )
            tree = p115_share_tree(client, share_url)
            selected, selected_keys = select_largest_missing_episode_files_by_season(
                tree,
                target_keys,
                fallback_season=int(follow["baseline_season"] or 1),
            )
            selected_episodes = {episode for _season, episode in selected_keys}
            selected_ids = [
                str(item.get("_share_id") or "")
                for item in selected
                if item.get("_share_id")
            ]
            if not selected_ids or not selected_episodes:
                fail_workflow_job(job_id, "115分享中没有可安全转存的目标集", retry_seconds=21600)
                log_hdhive_follow_event(
                    "transfer", "skipped", "115分享中没有找到可安全转存的缺失集文件",
                    follow=follow, cycle_id=cycle_id,
                    detail={"resource_slug": slug},
                )
                continue
            selected_seasons = {season for season, _episode in selected_keys}
            update_workflow_job_episodes(
                job_id,
                next(iter(selected_seasons)) if len(selected_seasons) == 1 else 0,
                selected_episodes,
            )
            selected_label = compact_episode_numbers(selected_episodes)
            log_hdhive_follow_event(
                "transfer", "running",
                f"正在转存第{selected_label}集到115目标目录",
                follow=follow, cycle_id=cycle_id,
                detail={"resource_slug": slug, "episodes": sorted(selected_episodes)},
            )
            before_files = p115_folder_snapshot(client, target_cid)
            received = p115_call(
                "接收115分享失败",
                client.share_receive,
                {"file_id": ",".join(selected_ids), "cid": target_cid},
                share_url=share_url,
            )
            receive_confirmed = False
            if not response_ok(received):
                rejection = p115_error_detail(received, "115拒绝接收")
                if p115_receive_was_duplicate(received):
                    receive_confirmed, recovery = recover_duplicate_p115_receive(
                        client, target_cid, selected, before_files
                    )
                    rejection = f"{rejection}；{recovery}"
                if not receive_confirmed:
                    fail_workflow_job(job_id, rejection, retry_seconds=900)
                    log_hdhive_follow_event(
                        "transfer", "failed",
                        f"115拒绝接收第{selected_label}集：{rejection}",
                        follow=follow, cycle_id=cycle_id,
                        detail={
                            "resource_slug": slug,
                            "episodes": sorted(selected_episodes),
                            "p115_response": response_summary(received),
                        },
                    )
                    continue
                log_hdhive_follow_event(
                    "transfer", "success",
                    f"第{selected_label}集曾被115接收，已找回并放入目标目录",
                    follow=follow, cycle_id=cycle_id,
                    detail={
                        "resource_slug": slug,
                        "episodes": sorted(selected_episodes),
                        "duplicate_recovered": True,
                    },
                )
            if not receive_confirmed and not wait_for_p115_change(
                lambda: p115_folder_snapshot(client, target_cid), before_files
            ):
                log_hdhive_follow_event(
                    "transfer", "running",
                    f"115首次受理后目录未变化，正在重新提交第{selected_label}集",
                    follow=follow, cycle_id=cycle_id,
                    detail={
                        "resource_slug": slug,
                        "episodes": sorted(selected_episodes),
                        "confirmation_retry": True,
                    },
                )
                received = p115_call(
                    "重新接收115分享失败",
                    client.share_receive,
                    {"file_id": ",".join(selected_ids), "cid": target_cid},
                    share_url=share_url,
                )
                changed = response_ok(received) and wait_for_p115_change(
                    lambda: p115_folder_snapshot(client, target_cid), before_files
                )
                recovery_detail = ""
                if not changed and p115_receive_was_duplicate(received):
                    changed, recovery_detail = recover_duplicate_p115_receive(
                        client, target_cid, selected, before_files
                    )
                if not changed:
                    rejection = p115_error_detail(
                        received, "115两次受理后目标目录仍没有新增文件"
                    )
                    failure = (
                        f"{rejection}"
                        f"{'；' + recovery_detail if recovery_detail else ''}；"
                        "已安排15分钟后重新处理"
                    )
                    fail_workflow_job(job_id, failure, retry_seconds=900)
                    log_hdhive_follow_event(
                        "transfer", "failed", failure,
                        follow=follow, cycle_id=cycle_id,
                        detail={
                            "resource_slug": slug,
                            "episodes": sorted(selected_episodes),
                            "confirmation_attempts": 2,
                        },
                    )
                    continue
        except HTTPException as error:
            fail_workflow_job(job_id, error.detail, retry_seconds=900)
            log_hdhive_follow_event(
                "transfer", "failed", f"资源处理失败：{error.detail}",
                follow=follow, cycle_id=cycle_id,
                detail={"resource_slug": slug},
            )
            continue

        log_hdhive_follow_event(
            "transfer", "success",
            f"第{selected_label}集已确认转存到115，等待 PanSave 整理与 Emby 入库",
            follow=follow, cycle_id=cycle_id,
            detail={"resource_slug": slug, "episodes": sorted(selected_episodes)},
        )
        update_workflow_job(
            job_id,
            "waiting_library",
            f"第{selected_label}集已确认转存，等待PanSave整理与Emby入库",
        )

        checked_at = now_iso()
        for (season, episode), candidate in candidates.items():
            if (season, episode) not in selected_keys:
                continue
            detail = (
                f"自动洗版第{episode}集：{candidate['file_name']} "
                f"({resource_size_label(candidate['file_size'])})"
            )
            with db() as connection:
                connection.execute(
                    "INSERT INTO hdhive_wash_attempts("
                    "fingerprint, follow_id, season_number, episode_number, "
                    "resource_slug, file_name, file_size, status, detail, "
                    "created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, "
                    "'success', ?, ?, ?) ON CONFLICT(fingerprint) DO UPDATE SET "
                    "status = 'success', detail = excluded.detail, "
                    "updated_at = excluded.updated_at",
                    (
                        candidate["fingerprint"], follow_id, season, episode,
                        slug, candidate["file_name"], candidate["file_size"],
                        detail, checked_at, checked_at,
                    ),
                )
                connection.execute(
                    "UPDATE hdhive_wash_episodes SET process_count = process_count + 1, "
                    "last_resource_slug = ?, last_file_name = ?, last_file_size = ?, "
                    "last_message = ?, updated_at = ? WHERE follow_id = ? "
                    "AND season_number = ? AND episode_number = ?",
                    (
                        slug, candidate["file_name"], candidate["file_size"], detail,
                        checked_at, follow_id, season, episode,
                    ),
                )
            record_transfer(
                user_id=int(follow["user_id"]),
                source="hdhive",
                resource_key=candidate["fingerprint"],
                tmdb_id=int(follow["tmdb_id"]),
                transfer_scope="auto_wash",
                status="success",
                detail=detail,
                follow_id=follow_id,
                season_number=season,
                episode_numbers=[episode],
            )
            transferred.add((season, episode))
            summaries.append(
                f"S{season:02d}E{episode:02d} · "
                f"{resource_size_label(candidate['file_size'])}"
            )

    if not transferred:
        return {"transferred": [], "message": "候选资源未能完成115转存"}
    latest = max(transferred)
    message = f"已提交洗版：{', '.join(summaries)}"
    with db() as connection:
        update_follow_progress_pair(
            connection, follow_id, "last_transferred", latest[0], latest[1]
        )
        update_follow_progress_pair(
            connection, follow_id, "last_seen", latest[0], latest[1]
        )
        connection.execute(
            "UPDATE tv_follows SET last_checked_at = ?, last_message = ?, "
            "updated_at = ? WHERE id = ?",
            (
                now_iso(), message, now_iso(), follow_id,
            ),
        )
    send_notifications(
        f"✅ 影巢追更已提交 PanSave 洗版\n\n"
        f"剧集：{follow['title']}\n"
        f"候选：{', '.join(summaries)}\n"
        f"规则：仅115 · 单集窗口 {config['window_hours']} 小时 · "
        f"每集最多 {config['max_transfers']} 次"
    )
    return {
        "transferred": [list(value) for value in sorted(transferred)],
        "message": message,
    }


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
            update_follow_progress_pair(
                connection, follow_id, "last_transferred", season_number, latest
            )
            update_follow_progress_pair(
                connection, follow_id, "last_seen", season_number, latest
            )
            connection.execute(
                "UPDATE tv_follows SET last_checked_at = ?, last_message = ?, "
                "updated_at = ? WHERE id = ?",
                (
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


def refresh_hdhive_subscribed_follows(
    include_unsubscribed: bool = False,
    cycle_id: str = "",
    only_unsubscribed: bool = False,
    force_file_lists: bool = False,
    follow_ids: Optional[set[int]] = None,
    strict: bool = False,
) -> int:
    with db() as connection:
        query = (
            "SELECT f.*, u.storage_destination FROM tv_follows f "
            "JOIN users u ON u.id = f.user_id WHERE f.active = 1"
        )
        if only_unsubscribed:
            query += " AND f.hdhive_subscription_id IS NULL"
        elif not include_unsubscribed:
            query += " AND f.hdhive_subscription_id IS NOT NULL"
        values: list[Any] = []
        if follow_ids is not None:
            if not follow_ids:
                return 0
            marks = ",".join("?" for _ in follow_ids)
            query += f" AND f.id IN ({marks})"
            values.extend(sorted(follow_ids))
        query += (
            " ORDER BY CASE u.storage_destination WHEN 'p115' THEN 0 ELSE 1 END, "
            "f.id ASC"
        )
        follows = connection.execute(query, values).fetchall()
    changed = 0
    processed_targets: set[tuple[str, int]] = set()
    for follow in follows:
        media_type = str(follow["media_type"] or "tv")
        target_key = (media_type, int(follow["tmdb_id"]))
        if target_key in processed_targets:
            continue
        processed_targets.add(target_key)
        log_hdhive_follow_event(
            "scan", "running", "开始读取影巢追更资源",
            follow=follow, cycle_id=cycle_id,
        )
        try:
            result = hdhive_call("resources", media_type, int(follow["tmdb_id"]))
        except HTTPException as error:
            log_hdhive_follow_event(
                "scan", "failed", f"影巢追更资源读取失败：{error.detail}",
                follow=follow, cycle_id=cycle_id,
            )
            if error.status_code == 429:
                raise
            if strict:
                raise
            continue
        resources = normalize_supported_hdhive_resources(
            extract_share_items(result)
        )
        direct_resources = [
            resource for resource in resources
            if hdhive_resource_is_direct_115(resource)
        ]
        log_hdhive_follow_event(
            "scan", "success",
            (
                f"影巢资源读取完成，共找到 {len(direct_resources)} 个115直转候选"
                + (
                    f"；另有 {len(resources) - len(direct_resources)} 个离线候选不读取文件列表"
                    if len(resources) > len(direct_resources)
                    else ""
                )
            ),
            follow=follow, cycle_id=cycle_id,
            detail={
                "resource_count": len(resources),
                "direct_115_count": len(direct_resources),
            },
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
        wash_config = hdhive_wash_config()
        if wash_config["enabled"]:
            wash_result = auto_wash_hdhive_follow(
                int(follow["id"]),
                direct_resources,
                cycle_id=cycle_id,
                force_file_lists=force_file_lists,
            )
            transferred = wash_result.get("transferred") or []
            if transferred:
                changed += len(transferred)
                log_hdhive_follow_event(
                    "complete", "success", str(wash_result.get("message") or "追更处理完成"),
                    follow=follow, cycle_id=cycle_id,
                    detail={"transferred": transferred},
                )
            else:
                log_hdhive_follow_event(
                    "complete", "skipped", str(wash_result.get("message") or "本次没有需要转存的资源"),
                    follow=follow, cycle_id=cycle_id,
                )
                checked_at = now_iso()
                with db() as connection:
                    connection.execute(
                        "UPDATE tv_follows SET last_checked_at = ?, updated_at = ? "
                        "WHERE id = ?",
                        (checked_at, checked_at, follow["id"]),
                    )
            continue
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


def hdhive_message_follow_ids(items: list[dict[str, Any]]) -> tuple[set[int], bool]:
    subscription_ids: set[int] = set()
    tmdb_ids: set[int] = set()
    target_keys: set[str] = set()
    for item in items:
        hints = message_target_hints(item)
        subscription_ids.update(int(value) for value in hints["subscription_ids"])
        tmdb_ids.update(int(value) for value in hints["tmdb_ids"])
        target_keys.update(str(value) for value in hints["target_keys"])
    with db() as connection:
        rows = connection.execute(
            "SELECT id, media_type, tmdb_id, hdhive_subscription_id "
            "FROM tv_follows WHERE active = 1 "
            "AND hdhive_subscription_id IS NOT NULL"
        ).fetchall()
        target_rows = (
            connection.execute(
                f"SELECT media_type, tmdb_id FROM hdhive_media_targets "
                f"WHERE target_key IN ({','.join('?' for _ in target_keys)})",
                sorted(target_keys),
            ).fetchall()
            if target_keys else []
        )
    target_pairs = {
        (str(row["media_type"]), int(row["tmdb_id"])) for row in target_rows
    }
    matched = {
        int(row["id"])
        for row in rows
        if (
            int(row["hdhive_subscription_id"] or 0) in subscription_ids
            or int(row["tmdb_id"] or 0) in tmdb_ids
            or (str(row["media_type"] or "tv"), int(row["tmdb_id"] or 0))
            in target_pairs
        )
    }
    has_hints = bool(subscription_ids or tmdb_ids or target_keys)
    if matched:
        return matched, False
    return {int(row["id"]) for row in rows}, not has_hints or not matched


def poll_hdhive_follow_messages(
    refresh_follows: bool = True,
    cycle_id: str = "",
) -> int:
    with db() as connection:
        active_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM tv_follows WHERE active = 1 "
                "AND hdhive_subscription_id IS NOT NULL"
            ).fetchone()[0]
        )
    if not active_count:
        log_hdhive_follow_event(
            "messages", "skipped", "当前没有已开启的影巢原生追更",
            cycle_id=cycle_id,
        )
        return 0
    log_hdhive_follow_event(
        "messages", "running", "正在读取影巢未读订阅消息",
        cycle_id=cycle_id,
    )
    unread_result = hdhive_call(
        "unread_message_count",
        subscription_only=True,
    )
    unread_data = hdhive_response_data(unread_result)
    unread_count = int(
        unread_data.get("unread_count") or 0
        if isinstance(unread_data, dict)
        else 0
    )
    if unread_count <= 0:
        acknowledged_at = now_iso()
        with db() as connection:
            connection.execute(
                "UPDATE hdhive_message_log SET status = 'acknowledged', "
                "acknowledged_at = ?, last_error = '' "
                "WHERE status = 'processed'",
                (acknowledged_at,),
            )
        log_hdhive_follow_event(
            "messages", "success", "影巢订阅消息读取完成，没有新消息",
            cycle_id=cycle_id, detail={"unread_count": 0},
        )
        return 0
    result = hdhive_call(
        "messages",
        subscription_only=True,
        status="unread",
        page_size=100,
    )
    items = extract_share_items(result)
    pending: list[tuple[str, str, dict[str, Any]]] = []
    retry_ack_ids: list[int] = []
    message_keys_by_id: dict[int, str] = {}
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
            existing = connection.execute(
                "SELECT status FROM hdhive_message_log WHERE message_key = ?",
                (message_key,),
            ).fetchone()
        if not existing or existing["status"] not in ("processed", "acknowledged"):
            pending.append((message_key, event_type, item))
        try:
            message_id = int(item.get("id") or item.get("message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        if message_id > 0:
            message_ids.append(message_id)
            message_keys_by_id[message_id] = message_key
            if existing and existing["status"] == "processed":
                retry_ack_ids.append(message_id)
    if pending:
        log_hdhive_follow_event(
            "messages", "success", f"读取到 {len(pending)} 条新订阅消息",
            cycle_id=cycle_id,
            detail={"unread_count": unread_count, "new_count": len(pending)},
        )
        stored_at = now_iso()
        with db() as connection:
            for message_key, event_type, item in pending:
                hints = message_target_hints(item)
                subscription_id = next(iter(hints["subscription_ids"]), None)
                tmdb_id = next(iter(hints["tmdb_ids"]), 0)
                connection.execute(
                    "INSERT INTO hdhive_message_log("
                    "message_key, event_type, payload_json, status, "
                    "subscription_id, tmdb_id, created_at"
                    ") VALUES(?, ?, ?, 'pending', ?, ?, ?) "
                    "ON CONFLICT(message_key) DO UPDATE SET "
                    "payload_json = excluded.payload_json, "
                    "event_type = excluded.event_type",
                    (
                        message_key,
                        event_type,
                        json.dumps(item, ensure_ascii=False),
                        subscription_id,
                        tmdb_id,
                        stored_at,
                    ),
                )
            latest = pending[0][2]
            set_setting(connection, "hdhive_last_message_at", stored_at)
            set_setting(
                connection,
                "hdhive_last_message_title",
                str(latest.get("title") or latest.get("body") or "订阅资源有更新")[:160],
            )
        if refresh_follows:
            follow_ids, fallback = hdhive_message_follow_ids(
                [item for _key, _event, item in pending]
            )
            log_hdhive_follow_event(
                "scan",
                "info",
                (
                    f"站内信已匹配 {len(follow_ids)} 条追更，开始精准扫描"
                    if not fallback
                    else "站内信缺少可用关联字段，回退扫描全部原生订阅"
                ),
                cycle_id=cycle_id,
                detail={"follow_ids": sorted(follow_ids), "fallback": fallback},
            )
            try:
                refresh_hdhive_subscribed_follows(
                    cycle_id=cycle_id,
                    force_file_lists=True,
                    follow_ids=follow_ids,
                    strict=True,
                )
            except Exception as error:
                retry_at = (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat()
                with db() as connection:
                    connection.executemany(
                        "UPDATE hdhive_message_log SET status = 'failed', "
                        "attempt_count = attempt_count + 1, last_error = ?, "
                        "next_retry_at = ? WHERE message_key = ?",
                        [
                            (str(error)[:500], retry_at, key)
                            for key, _event, _item in pending
                        ],
                    )
                raise
        processed_at = now_iso()
        with db() as connection:
            connection.executemany(
                "UPDATE hdhive_message_log SET status = 'processed', "
                "attempt_count = attempt_count + 1, processed_at = ?, "
                "last_error = '', next_retry_at = '' WHERE message_key = ?",
                [(processed_at, key) for key, _event, _item in pending],
            )
    ids_to_ack = sorted(set(message_ids or retry_ack_ids))
    if ids_to_ack:
        hdhive_call("mark_messages_read", ids_to_ack)
        acknowledged_at = now_iso()
        with db() as connection:
            connection.executemany(
                "UPDATE hdhive_message_log SET status = 'acknowledged', "
                "acknowledged_at = ?, last_error = '' WHERE message_key = ?",
                [
                    (acknowledged_at, message_keys_by_id[message_id])
                    for message_id in ids_to_ack
                    if message_id in message_keys_by_id
                ],
            )
    return len(pending)


def run_hdhive_follow_cycle(
    *,
    authorized_scopes: set[str],
    include_unsubscribed: bool,
    interval: int,
    cycle_id: str = "",
) -> dict[str, Any]:
    cycle_id = cycle_id or secrets.token_hex(8)
    log_hdhive_follow_event(
        "poll", "running", "后台追更检查开始",
        cycle_id=cycle_id,
        detail={"interval_seconds": interval},
    )
    message_count = 0
    changed = 0
    if {"subscription", "messages"}.issubset(authorized_scopes):
        message_count = poll_hdhive_follow_messages(
            refresh_follows=True, cycle_id=cycle_id
        )
        with db() as connection:
            last_full_scan = setting(connection, "hdhive_last_full_scan_at")
        try:
            last_full_timestamp = (
                datetime.fromisoformat(last_full_scan).timestamp()
                if last_full_scan
                else 0
            )
        except (TypeError, ValueError):
            last_full_timestamp = 0
        reconcile_due = (
            message_count == 0
            and time.time() - last_full_timestamp >= HDHIVE_QUIET_SCAN_SECONDS
        )
        if reconcile_due:
            changed += refresh_hdhive_subscribed_follows(
                cycle_id=cycle_id,
                force_file_lists=True,
            )
            with db() as connection:
                set_setting(connection, "hdhive_last_full_scan_at", now_iso())
        else:
            log_hdhive_follow_event(
                "scan",
                "skipped",
                (
                    "新订阅消息已完成精准扫描；本轮无需重复全量扫描"
                    if message_count > 0
                    else "没有新的订阅消息；六小时兜底检查尚未到期"
                ),
                cycle_id=cycle_id,
            )
    else:
        log_hdhive_follow_event(
            "scan", "skipped",
            "未同时获得订阅与站内信权限，不执行后台追更资源刷新",
            cycle_id=cycle_id,
        )
    with db() as connection:
        set_setting(connection, "hdhive_last_poll_at", now_iso())
        set_setting(connection, "hdhive_last_poll_error", "")
        set_setting(connection, "hdhive_next_poll_at", "")
    log_hdhive_follow_event(
        "poll", "success", "后台追更检查完成",
        cycle_id=cycle_id,
        detail={"message_count": message_count, "changed_count": changed},
    )
    cleanup_hdhive_follow_events()
    return {
        "cycle_id": cycle_id,
        "message_count": message_count,
        "changed_count": changed,
    }


def hdhive_follow_loop() -> None:
    while True:
        try:
            maybe_perform_hdhive_signin()
        except Exception:
            pass
        try:
            update_worker_health("hdhive_follow", "running")
            with db() as connection:
                row = hdhive_oauth_row(connection)
                enabled = setting(connection, "hdhive_poll_enabled") != "0"
                interval = max(
                    300,
                    int(setting(connection, "hdhive_poll_interval") or 900),
                )
                last_poll = setting(connection, "hdhive_last_poll_at")
                next_poll = setting(connection, "hdhive_next_poll_at")
                authorized_scopes = {
                    value
                    for value in str(row["authorized_scopes"] or "").split()
                    if value
                }
                unsubscribed_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM tv_follows WHERE active = 1 "
                        "AND hdhive_subscription_id IS NULL"
                    ).fetchone()[0]
                )
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
            try:
                next_time = (
                    datetime.fromisoformat(next_poll).timestamp()
                    if next_poll
                    else 0
                )
            except (TypeError, ValueError):
                next_time = 0
            if (
                HDHIVE_MESSAGE_POLLING_ENABLED
                and enabled
                and configured
                and time.time() - last_time >= interval
                and time.time() >= next_time
            ):
                cycle_id = secrets.token_hex(8)
                run_hdhive_follow_cycle(
                    authorized_scopes=authorized_scopes,
                    include_unsubscribed=bool(unsubscribed_count),
                    interval=interval,
                    cycle_id=cycle_id,
                )
            update_worker_health("hdhive_follow", "ok")
        except Exception as error:
            retry_after = 0
            if isinstance(error, HTTPException) and error.status_code == 429:
                try:
                    retry_after = int((error.headers or {}).get("Retry-After") or 0)
                except (TypeError, ValueError):
                    retry_after = 0
            retry_after = max(retry_after, 300)
            failed_at = datetime.now(timezone.utc)
            with db() as connection:
                set_setting(connection, "hdhive_last_poll_error", str(error)[:240])
                set_setting(connection, "hdhive_last_poll_at", failed_at.isoformat())
                set_setting(
                    connection,
                    "hdhive_next_poll_at",
                    (failed_at + timedelta(seconds=retry_after)).isoformat(),
                )
            log_hdhive_follow_event(
                "poll",
                "failed",
                f"后台追更检查异常：{error}；将在约 {retry_after} 秒后重试",
                cycle_id=locals().get("cycle_id", ""),
                detail={"retry_after_seconds": retry_after},
            )
            update_worker_health("hdhive_follow", "error", error=str(error))
        time.sleep(60)


def p115_offline_monitor_once() -> dict[str, int]:
    with db() as connection:
        monitors = connection.execute(
            "SELECT * FROM p115_offline_monitors WHERE status = 'pending' "
            "ORDER BY updated_at LIMIT 50"
        ).fetchall()
    if not monitors:
        return {"checked": 0, "completed": 0, "failed": 0}
    client = p115_client()
    checked = completed = failed = 0
    snapshots: dict[str, set[tuple[str, str, str]]] = {}
    for monitor in monitors:
        checked += 1
        monitor_id = int(monitor["workflow_job_id"])
        try:
            expected = json.loads(monitor["expected_files_json"] or "[]")
        except (TypeError, ValueError):
            expected = []
        if not isinstance(expected, list) or not expected:
            with db() as connection:
                connection.execute(
                    "UPDATE p115_offline_monitors SET last_error = ?, updated_at = ? "
                    "WHERE workflow_job_id = ?",
                    ("离线链接没有可核验的ED2K文件名，继续等待入库确认", now_iso(), monitor_id),
                )
            continue
        target_cid = str(monitor["target_cid"] or "0")
        try:
            if target_cid not in snapshots:
                snapshots[target_cid] = p115_folder_snapshot(client, target_cid)
            snapshot = snapshots[target_cid]
            files = {name: int(size or 0) for _fid, name, size in snapshot if name}
            landed = all(
                str(item.get("name") or "") in files
                and (
                    int(item.get("size") or 0) <= 0
                    or files[str(item.get("name") or "")] >= int(item.get("size") or 0)
                )
                for item in expected
            )
            if not landed:
                with db() as connection:
                    connection.execute(
                        "UPDATE p115_offline_monitors SET last_error = '', updated_at = ? "
                        "WHERE workflow_job_id = ?",
                        (now_iso(), monitor_id),
                    )
                continue
            timestamp = now_iso()
            detail = "已确认ED2K文件写入115目标目录"
            with db() as connection:
                connection.execute(
                    "UPDATE p115_offline_monitors SET status = 'completed', "
                    "last_error = '', completed_at = ?, updated_at = ? "
                    "WHERE workflow_job_id = ?",
                    (timestamp, timestamp, monitor_id),
                )
                connection.execute(
                    "UPDATE resource_transfer_log SET status = 'success', detail = ?, "
                    "updated_at = ? WHERE user_id = ? AND destination = ? AND source = ? "
                    "AND resource_key = ? AND transfer_scope = 'manual' "
                    "AND status = 'submitted'",
                    (
                        detail, timestamp, int(monitor["user_id"]),
                        str(monitor["destination"]), str(monitor["source"]),
                        str(monitor["resource_key"]),
                    ),
                )
            update_workflow_job(monitor_id, "waiting_library", detail)
            if monitor["follow_id"]:
                seed_offline_wash_baseline(int(monitor["follow_id"]), expected)
            log_hdhive_follow_event(
                "transfer", "success", detail,
                follow_id=monitor["follow_id"], user_id=int(monitor["user_id"]),
                tmdb_id=int(monitor["tmdb_id"] or 0), title=str(monitor["title"] or ""),
                detail={"target_cid": target_cid, "workflow_job_id": monitor_id},
            )
            completed += 1
        except Exception as error:
            with db() as connection:
                connection.execute(
                    "UPDATE p115_offline_monitors SET last_error = ?, updated_at = ? "
                    "WHERE workflow_job_id = ?",
                    (str(error)[:500], now_iso(), monitor_id),
                )
            failed += 1
    return {"checked": checked, "completed": completed, "failed": failed}


def p115_offline_monitor_loop() -> None:
    while True:
        try:
            result = p115_offline_monitor_once()
            update_worker_health("p115_offline_monitor", "ok", detail=result)
        except Exception as error:
            update_worker_health("p115_offline_monitor", "error", error=str(error))
        time.sleep(60)


@APP.on_event("startup")
def startup() -> None:
    init_db()
    recovered = recover_stale_workflow_jobs()
    update_worker_health(
        "workflow_recovery", "ok", detail={"recovered_jobs": recovered}
    )
    Thread(target=configure_telegram_menu, name="telegram-menu", daemon=True).start()
    Thread(target=configure_wecom_menu, name="wecom-menu", daemon=True).start()
    Thread(target=telegram_poll_loop, name="telegram-bot", daemon=True).start()
    Thread(target=emby_sync_loop, name="emby-sync", daemon=True).start()
    Thread(
        target=notification_outbox_loop,
        name="notification-outbox",
        daemon=True,
    ).start()
    Thread(target=dian_signin_loop, name="dian-signin", daemon=True).start()
    Thread(target=hdhive_follow_loop, name="hdhive-follow", daemon=True).start()
    Thread(
        target=p115_offline_monitor_loop,
        name="p115-offline-monitor",
        daemon=True,
    ).start()


@APP.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_PATH, headers={"Cache-Control": "no-cache"})


@APP.get("/api/library-notification-fallback.png")
def library_notification_fallback_image() -> FileResponse:
    return FileResponse(
        LIBRARY_NOTIFICATION_FALLBACK_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@APP.get("/api/library-notification-image/{filename}")
def library_notification_image(filename: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{64}\.(?:jpg|png|webp)", filename):
        raise HTTPException(404, "图片不存在")
    path = DATA_DIR / "library-notification-images" / filename
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    media_type = (
        "image/png" if path.suffix == ".png"
        else "image/webp" if path.suffix == ".webp"
        else "image/jpeg"
    )
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=2592000, immutable"},
    )


@APP.post("/api/emby-webhook/{destination}/{token}")
async def receive_emby_webhook(
    destination: str,
    token: str,
    request: Request,
) -> dict[str, Any]:
    if destination not in ("p115", "p123"):
        raise HTTPException(404, "Webhook 不存在")
    expected = emby_webhook_token(destination)
    if not expected or not hmac.compare_digest(str(token), expected):
        raise HTTPException(404, "Webhook 不存在")
    if not emby_webhook_enabled(destination):
        raise HTTPException(409, "该 Emby 的实时联动尚未开启")
    payload = parse_emby_webhook_payload(
        await request.body(),
        str(request.headers.get("content-type") or ""),
    )
    item_id, item_type = emby_webhook_item(payload)
    queued = queue_emby_webhook(destination, item_id, item_type)
    LOGGER.info(
        "Emby webhook accepted destination=%s item_type=%s has_item_id=%s queued=%s",
        destination,
        item_type or "unknown",
        bool(item_id),
        queued,
    )
    return {
        "ok": True,
        "queued": queued,
        "item_received": bool(item_id),
        "message": "已接收入库事件" if queued else "已合并重复入库事件",
    }


@APP.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def serialize_workflow_job(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["episode_numbers"] = episode_numbers_from_json(
        item.pop("episode_numbers_json", "[]")
    )
    item["state_label"] = JOB_STATE_LABELS.get(item["state"], item["state"])
    item["active"] = item["state"] in ACTIVE_JOB_STATES
    return item


def group_wash_activity(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    """Merge the same media episode across family members into one wash item."""
    grouped: dict[tuple[str, int, int, int], dict[str, Any]] = {}
    for row in rows:
        value = dict(row)
        key = (
            str(value.get("media_type") or "tv"),
            int(value.get("tmdb_id") or 0),
            int(value.get("season_number") or 1),
            int(value.get("episode_number") or 0),
        )
        current = grouped.get(key)
        if current is None:
            current = {
                "tmdb_id": key[1],
                "media_type": key[0],
                "title": str(value.get("title") or "未命名影片"),
                "year": str(value.get("year") or ""),
                "poster_url": tmdb_image_proxy_url(value.get("poster_path"), "w342"),
                "season_number": key[2],
                "episode_number": key[3],
                "opened_at": str(value.get("opened_at") or ""),
                "closes_at": str(value.get("closes_at") or ""),
                "process_count": 0,
                "last_file_size": 0,
                "last_file_name": "",
                "last_message": "",
                "follower_names": [],
            }
            grouped[key] = current
        opened_at = str(value.get("opened_at") or "")
        closes_at = str(value.get("closes_at") or "")
        if opened_at and (not current["opened_at"] or opened_at < current["opened_at"]):
            current["opened_at"] = opened_at
        if closes_at and closes_at > current["closes_at"]:
            current["closes_at"] = closes_at
        current["process_count"] += int(value.get("process_count") or 0)
        file_size = int(value.get("last_file_size") or 0)
        if file_size >= int(current["last_file_size"]):
            current["last_file_size"] = file_size
            current["last_file_name"] = str(value.get("last_file_name") or "")
            current["last_message"] = str(value.get("last_message") or "")
        name = str(value.get("display_name") or "家人")
        if name not in current["follower_names"]:
            current["follower_names"].append(name)
    for item in grouped.values():
        item["last_file_size_label"] = resource_size_label(item["last_file_size"])
        item["display_name"] = "、".join(item["follower_names"])
        item["follower_count"] = len(item["follower_names"])
    return sorted(grouped.values(), key=lambda item: item["closes_at"])


def reset_workflow_job_record(job_id: int) -> bool:
    """Cancel a non-terminal job so the same resource may be processed again."""
    timestamp = now_iso()
    with db() as connection:
        row = connection.execute(
            "SELECT id, state FROM media_workflow_jobs WHERE id = ?", (int(job_id),)
        ).fetchone()
        if not row:
            return False
        if str(row["state"]) in ("ingested", "cancelled"):
            raise HTTPException(409, "已完成或已重置的任务无需再次重置")
        connection.execute(
            "UPDATE media_workflow_jobs SET state = 'cancelled', "
            "detail = '管理员已重置；后续可重新处理这个资源', "
            "last_error = '', next_retry_at = '', completed_at = ?, updated_at = ? "
            "WHERE id = ?",
            (timestamp, timestamp, int(job_id)),
        )
        connection.execute(
            "UPDATE p115_offline_monitors SET status = 'cancelled', updated_at = ? "
            "WHERE workflow_job_id = ? AND status = 'pending'",
            (timestamp, int(job_id)),
        )
    return True


def serialize_worker_health(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    thresholds = {
        "notifications": 60,
        "telegram": 120,
        "dian_signin": 180,
        "hdhive_follow": 180,
        "emby_sync": 720,
    }
    threshold = thresholds.get(str(item["worker"]), 0)
    stale = False
    if threshold and str(item["status"]) != "idle":
        try:
            updated_at = datetime.fromisoformat(str(item["updated_at"]))
            stale = (
                datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)
            ).total_seconds() > threshold
        except (TypeError, ValueError):
            stale = True
    item["stale"] = stale
    if stale:
        item["status"] = "stale"
    try:
        item["detail"] = json.loads(str(item.pop("detail_json") or "{}"))
    except (TypeError, ValueError):
        item["detail"] = {}
    return item


@APP.get("/api/activity")
def activity_summary(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    request_filter = "" if user["role"] == "admin" else "WHERE user_id = ?"
    job_filter = "" if user["role"] == "admin" else "WHERE user_id = ?"
    values = () if user["role"] == "admin" else (int(user["id"]),)
    processing_states = sorted(ACTIVE_JOB_STATES - {"failed"})
    processing_marks = ",".join("?" for _ in processing_states)
    job_owner_condition = "" if user["role"] == "admin" else "AND j.user_id = ? "
    job_owner_values = () if user["role"] == "admin" else (int(user["id"]),)
    with db() as connection:
        request_rows = connection.execute(
            f"SELECT status, COUNT(*) AS count FROM movie_requests {request_filter} "
            "GROUP BY status",
            values,
        ).fetchall()
        job_rows = connection.execute(
            f"SELECT state, COUNT(*) AS count FROM media_workflow_jobs {job_filter} "
            "GROUP BY state",
            values,
        ).fetchall()
        recent_jobs = connection.execute(
            "SELECT * FROM media_workflow_jobs "
            + ("" if user["role"] == "admin" else "WHERE user_id = ? ")
            + "ORDER BY updated_at DESC LIMIT 8",
            values,
        ).fetchall()
        active_jobs = connection.execute(
            "SELECT j.*, u.display_name FROM media_workflow_jobs j "
            "LEFT JOIN users u ON u.id = j.user_id "
            f"WHERE j.state IN ({processing_marks}) {job_owner_condition}"
            "ORDER BY j.updated_at DESC LIMIT 100",
            (*processing_states, *job_owner_values),
        ).fetchall()
        failed_jobs = connection.execute(
            "SELECT j.*, u.display_name FROM media_workflow_jobs j "
            "LEFT JOIN users u ON u.id = j.user_id "
            f"WHERE j.state = 'failed' {job_owner_condition}"
            "ORDER BY j.updated_at DESC LIMIT 100",
            job_owner_values,
        ).fetchall()
        active_requests = connection.execute(
            "SELECT r.*, u.display_name FROM movie_requests r "
            "LEFT JOIN users u ON u.id = r.user_id "
            "WHERE r.status IN ('pending', 'approved', 'searching') "
            + ("" if user["role"] == "admin" else "AND r.user_id = ? ")
            + "ORDER BY r.updated_at DESC LIMIT 100",
            values,
        ).fetchall()
        completed_requests = connection.execute(
            "SELECT r.*, u.display_name FROM movie_requests r "
            "LEFT JOIN users u ON u.id = r.user_id WHERE r.status = 'available' "
            + ("" if user["role"] == "admin" else "AND r.user_id = ? ")
            + "ORDER BY r.updated_at DESC LIMIT 100",
            values,
        ).fetchall()
        active_follows = connection.execute(
            "SELECT f.*, u.display_name FROM tv_follows f "
            "LEFT JOIN users u ON u.id = f.user_id WHERE f.active = 1 "
            + ("" if user["role"] == "admin" else "AND f.user_id = ? ")
            + "ORDER BY f.updated_at DESC LIMIT 100",
            values,
        ).fetchall()
        active_wash_rows = connection.execute(
            "SELECT w.*, f.tmdb_id, f.media_type, f.title, f.year, f.poster_path, "
            "u.display_name FROM hdhive_wash_episodes w "
            "JOIN tv_follows f ON f.id = w.follow_id "
            "JOIN users u ON u.id = f.user_id "
            "WHERE f.active = 1 AND w.locked_at = '' AND w.closes_at > ? "
            + ("" if user["role"] == "admin" else "AND f.user_id = ? ")
            + "ORDER BY w.closes_at ASC",
            (now_iso(), *values),
        ).fetchall()
    request_counts = {str(row["status"]): int(row["count"]) for row in request_rows}
    job_counts = {str(row["state"]): int(row["count"]) for row in job_rows}
    grouped_follows = (
        group_follow_items([serialize_follow(row) for row in active_follows])
        if user["role"] == "admin"
        else [serialize_follow(row) for row in active_follows]
    )
    active_washes = group_wash_activity(active_wash_rows)
    return {
        "requests_active": sum(
            request_counts.get(status, 0)
            for status in ("pending", "approved", "searching")
        ),
        "requests_completed": request_counts.get("available", 0),
        "follows_active": len(grouped_follows),
        "washes_active": len(active_washes),
        # Failed work is actionable, but it is not currently processing. Keep the
        # two overview cards disjoint so their counts have an obvious meaning.
        "jobs_active": sum(job_counts.get(state, 0) for state in processing_states),
        "jobs_failed": job_counts.get("failed", 0),
        "recent_jobs": [serialize_workflow_job(row) for row in recent_jobs],
        "active_jobs": [serialize_workflow_job(row) for row in active_jobs],
        "failed_jobs": [serialize_workflow_job(row) for row in failed_jobs],
        "active_requests": [serialize_request(row) for row in active_requests],
        "completed_requests": [serialize_request(row) for row in completed_requests],
        "active_follows": grouped_follows,
        "active_washes": active_washes,
    }


@APP.post("/api/admin/workflow-jobs/{job_id}/reset")
def reset_workflow_job(
    job_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    if not reset_workflow_job_record(job_id):
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "job_id": int(job_id), "message": "任务已重置，可重新处理"}


@APP.post("/api/admin/workflow-jobs/reset-stale")
def reset_stale_workflow_jobs(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    states = sorted(ACTIVE_JOB_STATES - {"failed"})
    marks = ",".join("?" for _ in states)
    timestamp = now_iso()
    with db() as connection:
        stale_rows = connection.execute(
            "SELECT id FROM media_workflow_jobs "
            f"WHERE state IN ({marks}) AND updated_at < ?",
            (*states, cutoff),
        ).fetchall()
        cursor = connection.execute(
            "UPDATE media_workflow_jobs SET state = 'cancelled', "
            "detail = '管理员批量重置停滞任务；后续可重新处理这个资源', "
            "last_error = '', next_retry_at = '', completed_at = ?, updated_at = ? "
            f"WHERE state IN ({marks}) AND updated_at < ?",
            (timestamp, timestamp, *states, cutoff),
        )
        if stale_rows:
            stale_ids = [int(row["id"]) for row in stale_rows]
            id_marks = ",".join("?" for _ in stale_ids)
            connection.execute(
                "UPDATE p115_offline_monitors SET status = 'cancelled', updated_at = ? "
                f"WHERE status = 'pending' AND workflow_job_id IN ({id_marks})",
                (timestamp, *stale_ids),
            )
    return {
        "ok": True,
        "reset_count": int(cursor.rowcount or 0),
        "message": f"已重置 {int(cursor.rowcount or 0)} 个停滞任务",
    }


@APP.post("/api/admin/workflow-jobs/reset-failed")
def reset_failed_workflow_jobs(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    timestamp = now_iso()
    with db() as connection:
        failed_rows = connection.execute(
            "SELECT id FROM media_workflow_jobs WHERE state = 'failed'"
        ).fetchall()
        cursor = connection.execute(
            "UPDATE media_workflow_jobs SET state = 'cancelled', "
            "detail = '管理员批量重置失败任务；后续可重新处理这些资源', "
            "last_error = '', next_retry_at = '', completed_at = ?, updated_at = ? "
            "WHERE state = 'failed'",
            (timestamp, timestamp),
        )
        if failed_rows:
            failed_ids = [int(row["id"]) for row in failed_rows]
            marks = ",".join("?" for _ in failed_ids)
            connection.execute(
                "UPDATE p115_offline_monitors SET status = 'cancelled', updated_at = ? "
                f"WHERE status = 'pending' AND workflow_job_id IN ({marks})",
                (timestamp, *failed_ids),
            )
    count = int(cursor.rowcount or 0)
    return {
        "ok": True,
        "reset_count": count,
        "message": f"已重置 {count} 个失败任务，可重新选择资源处理",
    }


@APP.get("/api/admin/health")
def admin_health(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    with db() as connection:
        workers = connection.execute(
            "SELECT * FROM worker_health ORDER BY worker"
        ).fetchall()
        pending_messages = int(
            connection.execute(
                "SELECT COUNT(*) FROM hdhive_message_log "
                "WHERE status IN ('pending', 'failed', 'processed')"
            ).fetchone()[0]
        )
        pending_notifications = int(
            connection.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE status IN ('pending', 'failed')"
            ).fetchone()[0]
        )
        active_jobs = int(
            connection.execute(
                "SELECT COUNT(*) FROM media_workflow_jobs WHERE state IN ("
                "'discovered', 'unlocking', 'submitted', 'transferred', "
                "'organizing', 'waiting_library', 'failed')"
            ).fetchone()[0]
        )
    return {
        "workers": [serialize_worker_health(row) for row in workers],
        "pending_messages": pending_messages,
        "pending_notifications": pending_notifications,
        "active_jobs": active_jobs,
        "hdhive": hdhive_public_status(),
    }


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

    with image_download_lock(cache_path):
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
    cleanup_image_cache_if_needed()
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


@APP.patch("/api/admin/notice")
async def update_site_notice(
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    text = str(payload.get("text") or "").strip()
    if len(text) > 240:
        raise HTTPException(400, "公告最多 240 个字")
    with db() as connection:
        set_setting(connection, "site_notice", text)
    return {"ok": True, "text": text}


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
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        suffix = ".jpg"
    cache_dir = DATA_DIR / "douban-images"
    cache_path = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + suffix)
    headers = {"Cache-Control": "private, max-age=604800"}
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return FileResponse(cache_path, headers=headers)
    with image_download_lock(cache_path):
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return FileResponse(cache_path, headers=headers)
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
        if not content_type.startswith("image/") or not image.content:
            raise HTTPException(502, "豆瓣海报返回了无效内容")
        if len(image.content) > 20 * 1024 * 1024:
            raise HTTPException(502, "豆瓣海报文件过大")
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_name(
            f"{cache_path.name}.{secrets.token_hex(6)}.tmp"
        )
        temporary.write_bytes(image.content)
        os.replace(temporary, cache_path)
    cleanup_image_cache_if_needed()
    return FileResponse(cache_path, media_type=content_type, headers=headers)


@APP.get("/api/details/{media_type}/{tmdb_id}")
def media_details(
    media_type: str,
    tmdb_id: int,
    movie_session: Optional[str] = Cookie(default=None),
    refresh: bool = False,
) -> dict[str, Any]:
    user = require_user(movie_session)
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "影片信息无效")
    tmdb_options = {"force_refresh": True} if refresh else {}
    data = tmdb_get(
        f"/{media_type}/{tmdb_id}",
        {
            "language": "zh-CN",
            "append_to_response": "credits,videos,recommendations",
        },
        **tmdb_options,
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
            "AND destination = ? AND status = 'success' LIMIT 1",
            (
                user["id"], tmdb_id,
                storage_destination(user["storage_destination"]),
            ),
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
    library_ids = destination_emby_ids(destination, prefer_cached=True)
    progress = destination_episode_progress(
        destination,
        tmdb_id,
        known_in_library=tmdb_id in library_ids,
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
        authorized_scopes = {
            value for value in str(row["authorized_scopes"] or "").split() if value
        }
        poll_enabled = (
            HDHIVE_MESSAGE_POLLING_ENABLED
            and setting(connection, "hdhive_poll_enabled") != "0"
        )
        poll_interval = max(
            300, int(setting(connection, "hdhive_poll_interval") or 900)
        )
        last_poll = setting(connection, "hdhive_last_poll_at")
        next_poll = setting(connection, "hdhive_next_poll_at")
        last_full_scan = setting(connection, "hdhive_last_full_scan_at")
        last_message_at = setting(connection, "hdhive_last_message_at")
        last_message_title = setting(connection, "hdhive_last_message_title")
        last_poll_error = setting(connection, "hdhive_last_poll_error")
        auto_transfer = setting(connection, "hdhive_auto_transfer") != "0"
        offline_retry_cleanup = (
            setting(connection, "p115_offline_retry_cleanup") != "0"
        )
        wash_window_hours = max(
            12, min(72, int(setting(connection, "hdhive_wash_window_hours") or 48))
        )
        wash_after_emby = setting(connection, "hdhive_wash_after_emby") != "0"
        reprocess_changed = setting(connection, "hdhive_reprocess_changed") != "0"
        max_episode_transfers = max(
            1, min(10, int(setting(connection, "hdhive_max_episode_transfers") or 4))
        )
        lock_after_window = setting(connection, "hdhive_lock_after_window") != "0"
        pending_candidates = int(
            connection.execute(
                "SELECT COUNT(*) FROM hdhive_wash_episodes "
                "WHERE locked_at = '' AND closes_at > ?",
                (now_iso(),),
            ).fetchone()[0]
        )
        signin_enabled = setting(connection, "hdhive_signin_enabled") == "1"
        signin_time = setting(connection, "hdhive_signin_time") or "08:35"
        signin_mode = setting(connection, "hdhive_signin_mode") or "normal"
        last_signin_at = setting(connection, "hdhive_last_signin_at")
        last_signin_mode = setting(connection, "hdhive_last_signin_mode")
        last_signin_status = setting(connection, "hdhive_last_signin_status")
        last_signin_message = setting(connection, "hdhive_last_signin_message")
    app_secret = (
        decrypt_secret(row["app_secret_cipher"])
        if row["app_secret_cipher"]
        else ""
    )
    saved_proxy_url = (
        decrypt_secret(row["proxy_url_cipher"])
        if row["proxy_url_cipher"]
        else os.getenv("HDHIVE_PROXY_URL", "").strip()
    )
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
        "app_secret": app_secret,
        "scopes": row["scopes"] or HDHIVE_SCOPES,
        "authorized_scopes": sorted(authorized_scopes),
        "subscription_authorized": "subscription" in authorized_scopes,
        "messages_authorized": "messages" in authorized_scopes,
        "reauthorization_required": bool(
            connected
            and not {"subscription", "messages"}.issubset(authorized_scopes)
        ),
        "redirect_uri": row["redirect_uri"],
        "proxy_url": saved_proxy_url,
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
        "quiet_scan_interval": HDHIVE_QUIET_SCAN_SECONDS,
        "last_poll_at": last_poll,
        "next_poll_at": next_poll,
        "last_full_scan_at": last_full_scan,
        "last_message_at": last_message_at,
        "last_message_title": last_message_title,
        "last_poll_error": last_poll_error,
        "auto_transfer": auto_transfer,
        "offline_retry_cleanup": offline_retry_cleanup,
        "only_115": True,
        "wash_window_hours": wash_window_hours,
        "wash_after_emby": wash_after_emby,
        "reprocess_changed": reprocess_changed,
        "max_episode_transfers": max_episode_transfers,
        "lock_after_window": lock_after_window,
        "pending_candidates": pending_candidates,
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


@APP.get("/api/admin/hdhive/follow-events")
def hdhive_follow_events(
    status: str = "",
    stage: str = "",
    resource_status: str = "",
    follow_id: int = 0,
    limit: int = 200,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    status = str(status or "").strip().lower()
    stage = str(stage or "").strip().lower()
    resource_status = str(resource_status or "").strip().lower()
    allowed_statuses = {"running", "success", "skipped", "failed", "info"}
    allowed_stages = {
        "subscription", "poll", "messages", "scan", "file_list", "unlock",
        "transfer", "complete", "library"
    }
    if status and status not in allowed_statuses:
        raise HTTPException(400, "管理日志状态筛选无效")
    if stage and stage not in allowed_stages:
        raise HTTPException(400, "管理日志步骤筛选无效")
    if resource_status and resource_status not in {"ongoing", "completed"}:
        raise HTTPException(400, "管理日志资源状态筛选无效")
    conditions: list[str] = []
    values: list[Any] = []
    if status:
        conditions.append("e.status = ?")
        values.append(status)
    if stage:
        conditions.append("e.stage = ?")
        values.append(stage)
    if follow_id > 0:
        conditions.append("e.follow_id = ?")
        values.append(int(follow_id))
    if resource_status:
        conditions.append(
            "CASE WHEN e.follow_id IS NOT NULL THEN 'ongoing' "
            "ELSE COALESCE(json_extract(e.detail_json, '$.resource_status'), '') "
            "END = ?"
        )
        values.append(resource_status)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    safe_limit = max(20, min(500, int(limit or 200)))
    with db() as connection:
        rows = connection.execute(
            "SELECT e.*, u.display_name, "
            "COALESCE(NULLIF(f.poster_path, ''), ("
            "SELECT NULLIF(r.poster_path, '') FROM movie_requests r "
            "WHERE r.tmdb_id = e.tmdb_id AND r.poster_path != '' "
            "ORDER BY r.id DESC LIMIT 1"
            "), '') AS poster_path FROM hdhive_follow_events e "
            "LEFT JOIN users u ON u.id = e.user_id "
            "LEFT JOIN tv_follows f ON f.id = e.follow_id "
            f"{where} ORDER BY e.id DESC LIMIT ?",
            (*values, safe_limit),
        ).fetchall()
        summary_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM hdhive_follow_events "
            "WHERE created_at >= ? GROUP BY status",
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
        ).fetchall()
    events = []
    for row in rows:
        try:
            detail = json.loads(str(row["detail_json"] or "{}"))
        except json.JSONDecodeError:
            detail = {}
        events.append(
            {
                "id": int(row["id"]),
                "cycle_id": row["cycle_id"],
                "follow_id": row["follow_id"],
                "user_id": int(row["user_id"] or 0),
                "display_name": row["display_name"] or "",
                "tmdb_id": int(row["tmdb_id"] or 0),
                "title": row["title"],
                "poster_path": str(row["poster_path"] or ""),
                "poster_url": tmdb_image_proxy_url(row["poster_path"], "w342"),
                "stage": row["stage"],
                "status": row["status"],
                "message": row["message"],
                "resource_status": str(
                    detail.get("resource_status")
                    or ("ongoing" if row["follow_id"] else "")
                ),
                "detail": detail,
                "created_at": row["created_at"],
            }
        )
    summary = {key: 0 for key in allowed_statuses}
    summary.update({str(row["status"]): int(row["count"]) for row in summary_rows})
    return {"events": events, "summary": summary, "window_hours": 24}


def hdhive_message_text(payload: Any, keys: set[str], depth: int = 0) -> str:
    """Read a display field from known message keys without exposing raw JSON."""
    if depth > 5:
        return ""
    if isinstance(payload, dict):
        for raw_key, value in payload.items():
            if str(raw_key or "").strip().lower() in keys and isinstance(
                value, (str, int, float)
            ):
                text = str(value or "").strip()
                if text:
                    return text[:500]
        for value in payload.values():
            if isinstance(value, (dict, list, tuple)):
                text = hdhive_message_text(value, keys, depth + 1)
                if text:
                    return text
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            text = hdhive_message_text(value, keys, depth + 1)
            if text:
                return text
    return ""


def hdhive_message_detail_fields(payload: Any) -> list[dict[str, str]]:
    """Flatten useful inbox fields while omitting credentials and large blobs."""
    blocked = {
        "token", "access_token", "refresh_token", "secret", "app_secret",
        "cookie", "password", "authorization", "signature", "sign",
    }
    fields: list[dict[str, str]] = []

    def walk(value: Any, path: str = "", depth: int = 0) -> None:
        if depth > 4 or len(fields) >= 30:
            return
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key or "").strip()
                if key and key.lower() not in blocked:
                    walk(child, f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(value, (list, tuple)):
            if all(not isinstance(item, (dict, list, tuple)) for item in value):
                text = "、".join(str(item) for item in value if item is not None)
                if text:
                    fields.append({"label": path or "内容", "value": text[:500]})
            else:
                for index, child in enumerate(value[:10]):
                    walk(child, f"{path}[{index + 1}]", depth + 1)
        elif value is not None and path:
            text = str(value).strip()
            if text:
                fields.append({"label": path, "value": text[:500]})

    walk(payload)
    return fields


@APP.get("/api/admin/hdhive/messages")
def hdhive_messages(
    limit: int = 60,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    """Return useful HDHive update notices, not background polling heartbeats."""
    require_admin(movie_session)
    safe_limit = max(10, min(200, int(limit or 60)))
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM hdhive_message_log ORDER BY created_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        follows = connection.execute(
            "SELECT f.id, f.user_id, f.tmdb_id, f.title, "
            "f.hdhive_subscription_id, u.display_name FROM tv_follows f "
            "LEFT JOIN users u ON u.id = f.user_id WHERE f.active = 1 "
            "ORDER BY f.id DESC"
        ).fetchall()
    by_subscription: dict[int, list[sqlite3.Row]] = {}
    by_tmdb: dict[int, list[sqlite3.Row]] = {}
    for follow in follows:
        subscription_id = int(follow["hdhive_subscription_id"] or 0)
        tmdb_id = int(follow["tmdb_id"] or 0)
        if subscription_id:
            by_subscription.setdefault(subscription_id, []).append(follow)
        if tmdb_id:
            by_tmdb.setdefault(tmdb_id, []).append(follow)
    messages: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError):
            payload = {}
        hints = message_target_hints(payload if isinstance(payload, dict) else {})
        subscription_id = int(
            row["subscription_id"]
            or next(iter(hints["subscription_ids"]), 0)
            or 0
        )
        tmdb_id = int(row["tmdb_id"] or next(iter(hints["tmdb_ids"]), 0) or 0)
        headline = hdhive_message_text(
            payload, {"title", "subject", "headline", "notification_title"}
        )
        content = hdhive_message_text(
            payload, {"content", "body", "message", "description", "summary"}
        )
        remote_created_at = hdhive_message_text(
            payload,
            {"created_at", "sent_at", "published_at", "event_time", "timestamp"},
        )
        matching_follows = list(
            by_subscription.get(subscription_id) or by_tmdb.get(tmdb_id) or []
        )
        if not matching_follows:
            message_text = f"{headline}\n{content}"
            title_matches = [
                candidate for candidate in follows
                if len(str(candidate["title"] or "").strip()) >= 2
                and str(candidate["title"]).strip() in message_text
            ]
            if title_matches:
                longest = max(
                    len(str(candidate["title"]).strip())
                    for candidate in title_matches
                )
                matched_titles = {
                    str(candidate["title"]).strip()
                    for candidate in title_matches
                    if len(str(candidate["title"]).strip()) == longest
                }
                matching_follows = [
                    candidate for candidate in title_matches
                    if str(candidate["title"]).strip() in matched_titles
                ]
        follow = matching_follows[0] if matching_follows else None
        follow_ids = list(
            dict.fromkeys(int(candidate["id"]) for candidate in matching_follows)
        )
        member_names = list(
            dict.fromkeys(
                str(candidate["display_name"] or "").strip()
                for candidate in matching_follows
                if str(candidate["display_name"] or "").strip()
            )
        )
        messages.append(
            {
                "message_key": row["message_key"],
                "event_type": row["event_type"],
                "headline": headline or "影巢订阅有新资源更新",
                "content": content,
                "status": row["status"],
                "last_error": row["last_error"],
                "attempt_count": int(row["attempt_count"] or 0),
                "next_retry_at": row["next_retry_at"],
                "received_at": row["created_at"],
                "remote_created_at": remote_created_at,
                "subscription_id": subscription_id,
                "tmdb_id": tmdb_id or int(follow["tmdb_id"] if follow else 0),
                "follow_id": int(follow["id"] if follow else 0),
                "follow_ids": follow_ids,
                "follow_title": str(follow["title"] if follow else ""),
                "display_name": "、".join(member_names),
                "detail_fields": hdhive_message_detail_fields(payload),
            }
        )
    return {"messages": messages, "count": len(messages)}


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
                "1" if payload["poll_enabled"] else "0",
            )
        if payload.get("poll_interval"):
            interval = max(300, min(3600, int(payload["poll_interval"])))
            set_setting(connection, "hdhive_poll_interval", interval)
        boolean_settings = {
            "auto_transfer": "hdhive_auto_transfer",
            "offline_retry_cleanup": "p115_offline_retry_cleanup",
            "wash_after_emby": "hdhive_wash_after_emby",
            "reprocess_changed": "hdhive_reprocess_changed",
            "lock_after_window": "hdhive_lock_after_window",
        }
        for payload_key, setting_key in boolean_settings.items():
            if payload_key in payload:
                set_setting(
                    connection,
                    setting_key,
                    "1" if payload[payload_key] else "0",
                )
        if payload.get("wash_window_hours"):
            hours = max(12, min(72, int(payload["wash_window_hours"])))
            set_setting(connection, "hdhive_wash_window_hours", hours)
        if payload.get("max_episode_transfers"):
            maximum = max(1, min(10, int(payload["max_episode_transfers"])))
            set_setting(connection, "hdhive_max_episode_transfers", maximum)
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
    result = await asyncio.to_thread(
        perform_hdhive_signin,
        mode,
        source="manual",
    )
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
            "refresh_token_cipher = '', authorized_scopes = '', authorized_at = '', token_expires_at = '', "
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
    refresh: bool = False,
) -> dict[str, Any]:
    require_user(movie_session)
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "影片编号无效")
    if not refresh:
        cached = cached_resource_response("hdhive", media_type, tmdb_id)
        if cached is not None:
            return cached
    with resource_request_lock("hdhive", media_type, tmdb_id):
        if not refresh:
            cached = cached_resource_response("hdhive", media_type, tmdb_id)
            if cached is not None:
                return cached
        result = hdhive_call("resources", media_type, tmdb_id)
        data = result.get("data", [])
        if isinstance(data, dict):
            items = extract_share_items({"data": data})
        elif isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        else:
            items = []
        return cache_resource_response("hdhive", media_type, tmdb_id, {
            "resources": normalize_supported_hdhive_resources(items),
            "meta": result.get("meta", {}),
        })


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
    item["current_emby_label"] = episode_progress_label(
        int(item.get("current_emby_season") or 1),
        int(item.get("current_emby_episode") or 0),
        "已入库至",
    )
    item["latest_label"] = episode_progress_label(
        int(item["last_seen_season"]),
        int(item["last_seen_episode"]),
        "已看到",
    )
    with db() as connection:
        wash = connection.execute(
            "SELECT season_number, episode_number, closes_at, locked_at, "
            "process_count, last_file_size, last_message "
            "FROM hdhive_wash_episodes WHERE follow_id = ? "
            "ORDER BY season_number DESC, episode_number DESC LIMIT 1",
            (int(item["id"]),),
        ).fetchone()
    item["wash"] = None
    if wash:
        remaining_seconds = max(
            0,
            int(
                datetime.fromisoformat(str(wash["closes_at"])).timestamp()
                - time.time()
            ),
        )
        item["wash"] = {
            "season_number": int(wash["season_number"]),
            "episode_number": int(wash["episode_number"]),
            "process_count": int(wash["process_count"] or 0),
            "last_file_size": int(wash["last_file_size"] or 0),
            "last_file_size_label": resource_size_label(wash["last_file_size"]),
            "remaining_seconds": remaining_seconds,
            "locked": bool(wash["locked_at"]),
            "last_message": str(wash["last_message"] or ""),
        }
    return item


def group_follow_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge family members following the same TMDB title into one media card."""
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        key = (str(item.get("media_type") or "tv"), int(item["tmdb_id"]))
        current = grouped.get(key)
        if current is None:
            current = dict(item)
            current["follow_ids"] = []
            current["follower_names"] = []
            grouped[key] = current
        current["follow_ids"].append(int(item["id"]))
        name = str(item.get("display_name") or item.get("username") or "家人")
        if name not in current["follower_names"]:
            current["follower_names"].append(name)
        current["hdhive_subscribed"] = bool(
            current.get("hdhive_subscribed") or item.get("hdhive_subscribed")
        )
        if not current.get("hdhive_subscription_id") and item.get(
            "hdhive_subscription_id"
        ):
            current["id"] = int(item["id"])
            current["hdhive_subscription_id"] = item["hdhive_subscription_id"]
        current_progress = (
            int(current.get("current_emby_season") or 1),
            int(current.get("current_emby_episode") or 0),
        )
        item_progress = (
            int(item.get("current_emby_season") or 1),
            int(item.get("current_emby_episode") or 0),
        )
        if item_progress > current_progress:
            current["current_emby_season"] = item_progress[0]
            current["current_emby_episode"] = item_progress[1]
            current["current_emby_label"] = item.get("current_emby_label") or ""
    for item in grouped.values():
        item["follower_count"] = len(item["follower_names"])
        item["display_name"] = "、".join(item["follower_names"])
    return list(grouped.values())


def refresh_follow_emby_baseline(follow_id: int) -> sqlite3.Row:
    """Refresh current Emby progress without mutating the follow start baseline."""
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
            "UPDATE tv_follows SET current_emby_season = ?, current_emby_episode = ?, "
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
    items = [serialize_follow(row) for row in rows]
    if user["role"] != "admin":
        return {"follows": items}

    return {"follows": group_follow_items(items)}


@APP.get("/api/follows/{follow_id}/timeline")
def follow_timeline(
    follow_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    with db() as connection:
        follow = connection.execute(
            "SELECT * FROM tv_follows WHERE id = ? AND active = 1", (follow_id,)
        ).fetchone()
        if not follow or (
            user["role"] != "admin" and int(follow["user_id"]) != int(user["id"])
        ):
            raise HTTPException(404, "没有找到这条追更")
        events = connection.execute(
            "SELECT id, stage, status, message, detail_json, created_at "
            "FROM hdhive_follow_events WHERE follow_id = ? "
            "ORDER BY id DESC LIMIT 80",
            (follow_id,),
        ).fetchall()
        jobs = connection.execute(
            "SELECT * FROM media_workflow_jobs WHERE follow_id = ? "
            "ORDER BY updated_at DESC LIMIT 30",
            (follow_id,),
        ).fetchall()
    return {
        "follow": serialize_follow(follow),
        "events": [dict(row) for row in events],
        "jobs": [serialize_workflow_job(row) for row in jobs],
    }


@APP.get("/api/follows/emby-progress")
def follows_emby_progress(
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    """Refresh all visible follows while sharing one library lookup per destination."""

    user = require_user(movie_session)
    with db() as connection:
        query = (
            "SELECT f.*, u.storage_destination FROM tv_follows f "
            "JOIN users u ON u.id = f.user_id WHERE f.active = 1 "
        )
        values: tuple[Any, ...] = ()
        if user["role"] != "admin":
            query += "AND f.user_id = ? "
            values = (user["id"],)
        rows = connection.execute(query, values).fetchall()

    progress_by_id: dict[int, dict[str, Any]] = {}
    rows_by_destination: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if str(row["media_type"] or "tv") != "tv":
            continue
        destination = storage_destination(row["storage_destination"])
        rows_by_destination.setdefault(destination, []).append(row)

    jobs: list[tuple[int, str, int]] = []
    for destination, destination_rows in rows_by_destination.items():
        library_ids = destination_emby_ids(destination)
        for row in destination_rows:
            tmdb_id = int(row["tmdb_id"])
            if tmdb_id in library_ids:
                jobs.append((int(row["id"]), destination, tmdb_id))
            else:
                progress_by_id[int(row["id"])] = {"not_in_library": True}

    if jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as executor:
            future_map = {
                executor.submit(
                    destination_episode_progress,
                    destination,
                    tmdb_id,
                    known_in_library=True,
                ): follow_id
                for follow_id, destination, tmdb_id in jobs
            }
            for future in as_completed(future_map):
                follow_id = future_map[future]
                try:
                    progress_by_id[follow_id] = future.result()
                except Exception:
                    progress_by_id[follow_id] = {}

    refreshed_at = now_iso()
    items: list[dict[str, Any]] = []
    with db() as connection:
        for row in rows:
            if str(row["media_type"] or "tv") != "tv":
                continue
            follow_id = int(row["id"])
            progress = progress_by_id.get(follow_id, {})
            if progress.get("not_in_library"):
                season_number, episode_number = 1, 0
            elif progress.get("emby_latest_episode_number") is not None:
                season_number = int(progress.get("emby_latest_season_number") or 1)
                episode_number = int(progress.get("emby_latest_episode_number") or 0)
            else:
                season_number = int(row["current_emby_season"] or 1)
                episode_number = int(row["current_emby_episode"] or 0)
            connection.execute(
                "UPDATE tv_follows SET current_emby_season = ?, current_emby_episode = ?, "
                "updated_at = ? WHERE id = ?",
                (season_number, episode_number, refreshed_at, follow_id),
            )
            items.append({
                "follow_id": follow_id,
                "current_emby_season": season_number,
                "current_emby_episode": episode_number,
                "current_emby_label": episode_progress_label(
                    season_number, episode_number, "已入库至"
                ),
            })
    return {"progress": items}


@APP.get("/api/follows/{follow_id}/emby-progress")
def follow_emby_progress(
    follow_id: int,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    user = require_user(movie_session)
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM tv_follows WHERE id = ? AND active = 1",
            (follow_id,),
        ).fetchone()
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(404, "没有找到这条追更")
    row = refresh_follow_emby_baseline(follow_id)
    return {
        "follow_id": follow_id,
        "current_emby_season": int(row["current_emby_season"] or 1),
        "current_emby_episode": int(row["current_emby_episode"] or 0),
        "current_emby_label": episode_progress_label(
            int(row["current_emby_season"] or 1),
            int(row["current_emby_episode"] or 0),
            "已入库至",
        ),
    }


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
    detail = await asyncio.to_thread(
        tmdb_get,
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
            (
                has_manual_transfer(
                    tmdb_id, int(user["id"]), user["storage_destination"]
                )
                or has_initial_media_submission(
                    tmdb_id, int(user["id"]), user["storage_destination"]
                )
            )
            if user["storage_destination"] == "p123"
            else (
                has_manual_transfer(tmdb_id, int(user["id"]), "p115")
                or has_initial_media_submission(tmdb_id, int(user["id"]), "p115")
            )
        )
    ):
        raise HTTPException(409, "请先手动转存初始版本，再开启影巢追更")
    progress = (
        await asyncio.to_thread(
            destination_episode_progress,
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
            "last_seen_episode, current_emby_season, current_emby_episode, "
            "created_at, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, tmdb_id) DO UPDATE SET active = 1, "
            "media_type = excluded.media_type, title = excluded.title, "
            "original_title = excluded.original_title, "
            "year = excluded.year, poster_path = excluded.poster_path, "
            "baseline_episode = CASE "
            "WHEN excluded.baseline_season > tv_follows.baseline_season "
            "THEN excluded.baseline_episode "
            "WHEN excluded.baseline_season = tv_follows.baseline_season "
            "THEN MAX(tv_follows.baseline_episode, excluded.baseline_episode) "
            "ELSE tv_follows.baseline_episode END, "
            "baseline_season = MAX(tv_follows.baseline_season, excluded.baseline_season), "
            "last_seen_episode = CASE "
            "WHEN excluded.last_seen_season > tv_follows.last_seen_season "
            "THEN excluded.last_seen_episode "
            "WHEN excluded.last_seen_season = tv_follows.last_seen_season "
            "THEN MAX(tv_follows.last_seen_episode, excluded.last_seen_episode) "
            "ELSE tv_follows.last_seen_episode END, "
            "last_seen_season = MAX(tv_follows.last_seen_season, excluded.last_seen_season), "
            "current_emby_season = excluded.current_emby_season, "
            "current_emby_episode = excluded.current_emby_episode, "
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
    attach_pending_offline_monitors_to_follow(
        int(row["id"]), int(user["id"]), tmdb_id
    )
    bind_error = ""
    if subscription_slug:
        try:
            row = await asyncio.to_thread(
                bind_hdhive_follow_subscription,
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
        media_filters={"websites": ["115"]},
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
    log_hdhive_follow_event(
        "subscription", "success", message, follow=row,
        detail={"subscription_id": subscription_id, "target_key": target["target_key"]},
    )
    return row


@APP.post("/api/follows/{follow_id}/hdhive-subscription")
async def create_hdhive_follow_subscription(
    follow_id: int,
    request: Request,
    movie_session: Optional[str] = Cookie(default=None),
) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    row = await asyncio.to_thread(
        bind_hdhive_follow_subscription,
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
    refresh: bool = False,
) -> dict[str, Any]:
    require_user(movie_session)
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "影片编号无效")
    if not refresh:
        cached = cached_resource_response("dian", media_type, tmdb_id, season)
        if cached is not None:
            return cached
    with resource_request_lock("dian", media_type, tmdb_id, season):
        if not refresh:
            cached = cached_resource_response("dian", media_type, tmdb_id, season)
            if cached is not None:
                return cached
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
        return cache_resource_response("dian", media_type, tmdb_id, {
            "resources": normalize_supported_dian_resources(
                extract_share_items(result)
            )
        }, season)


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

    title_spec = parse_episode_spec(payload.get("resource_title"))
    wanted_episodes = set(title_spec["episode_numbers"])
    selected_episode_numbers = sorted(wanted_episodes)
    with db() as connection:
        follow_row = connection.execute(
            "SELECT id FROM tv_follows WHERE user_id = ? AND tmdb_id = ? "
            "AND active = 1",
            (int(user["id"]), tmdb_id),
        ).fetchone()
    event_follow_id = int(follow_row["id"]) if follow_row else None
    resource_status = management_resource_status(payload, bool(follow_row))
    event_detail = {
        "source": "hdhive",
        "resource_slug": slug,
        "resource_title": str(payload.get("resource_title") or ""),
        "media_type": media_type,
        "resource_status": resource_status,
        "season_number": int(title_spec["season_number"]),
        "episode_numbers": selected_episode_numbers,
    }
    log_hdhive_follow_event(
        "unlock", "running", "正在解锁影巢资源",
        follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
        title=str(payload.get("title") or payload.get("resource_title") or ""),
        detail=event_detail,
    )
    try:
        unlocked = await asyncio.to_thread(hdhive_call, "unlock", slug)
    except Exception as error:
        log_hdhive_follow_event(
            "unlock", "failed", f"影巢资源解锁失败：{error}",
            follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            detail=event_detail,
        )
        raise
    data = unlocked.get("data", unlocked)
    if not isinstance(data, dict):
        log_hdhive_follow_event(
            "unlock", "failed", "影巢解锁结果格式无效",
            follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            detail=event_detail,
        )
        raise HTTPException(502, "影巢解锁结果格式无效")
    links = extract_dian_transfer_links({"payload": data})
    if not links:
        log_hdhive_follow_event(
            "unlock", "failed", "影巢解锁后没有返回资源链接",
            follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            detail=event_detail,
        )
        raise HTTPException(502, "影巢解锁后没有返回资源链接")
    share_url = links[0]
    selected_links = links
    episode_links_filtered = False
    if (
        uses_p115_delivery(dict(user))
        and not is_115_share_url(share_url)
        and media_type == "tv"
        and wanted_episodes
    ):
        season_number = int(title_spec["season_number"])
        progress = await asyncio.to_thread(
            destination_episode_progress,
            str(user["storage_destination"]),
            tmdb_id,
            known_in_library=True,
        )
        by_season = progress.get("emby_episode_numbers") or {}
        present_episodes = {
            int(value)
            for value in by_season.get(str(season_number), [])
            if int(value) > 0
        }
        present_episodes.update(
            transferred_episode_set(tmdb_id, season_number)
        )
        selected_links, selected_episodes = select_missing_episode_transfer_links(
            links,
            season_number=season_number,
            wanted_episodes=wanted_episodes,
            present_episodes=present_episodes,
        )
        if not selected_links:
            raise HTTPException(409, "这个资源中可识别的剧集都已存在")
        episode_links_filtered = selected_links != links
        selected_episode_numbers = sorted(selected_episodes)
        event_detail["episode_numbers"] = selected_episode_numbers

    # Build idempotency from the episodes that will actually be submitted.
    # This lets a corrected E07-only attempt bypass an older bad E01-E07 job.
    job = begin_workflow_job(
        user_id=int(user["id"]),
        destination=str(user["storage_destination"]),
        source="hdhive",
        resource_key=slug,
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=str(payload.get("title") or payload.get("resource_title") or ""),
        season_number=int(title_spec["season_number"]),
        episode_numbers=selected_episode_numbers,
        follow_id=event_follow_id,
        scope=transfer_scope,
    )
    attach_workflow_job_to_request(request, int(job["id"]))
    log_hdhive_follow_event(
        "unlock", "success", "影巢资源解锁成功，正在提交到网盘",
        follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
        title=str(payload.get("title") or payload.get("resource_title") or ""),
        detail=event_detail,
    )

    delivery = p123_delivery_settings()
    if user.get("storage_destination") == "p123" and delivery["mode"] == "telegram":
        result = await deliver_to_pansave(
            user=user,
            share_url=share_url,
            source="hdhive",
            resource_key=slug,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            tmdb_id=tmdb_id,
            season_number=int(title_spec["season_number"]),
            episode_numbers=selected_episode_numbers,
        )
        update_workflow_job(
            int(job["id"]), "waiting_library", "链接已发送123，等待整理与入库"
        )
        log_hdhive_follow_event(
            "transfer", "success", "影巢资源链接已发送123，等待整理与入库",
            follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            detail=event_detail,
        )
        return {**result, "job_id": int(job["id"]), "workflow_state": "waiting_library"}

    client = await asyncio.to_thread(p115_client)
    with db() as connection:
        target_cid = (
            delivery["target_cid"]
            if user.get("storage_destination") == "p123"
            else setting(connection, "p115_target_cid") or "0"
        )
        offline_retry_cleanup = (
            setting(connection, "p115_offline_retry_cleanup") != "0"
        )

    if not is_115_share_url(share_url):
        def submit_offline_links(error_label: str) -> dict[str, Any]:
            if len(selected_links) == 1:
                return p115_call(
                    error_label,
                    client.clouddownload_task_add_url,
                    {"url": selected_links[0], "wp_path_id": target_cid},
                )
            offline_payload = {
                f"url[{index}]": link
                for index, link in enumerate(selected_links)
            }
            offline_payload["wp_path_id"] = target_cid
            return p115_call(
                error_label,
                client.clouddownload_task_add_urls,
                offline_payload,
            )

        before_tasks = await asyncio.to_thread(p115_offline_snapshot, client)
        retried_completed_task = False
        queued = await asyncio.to_thread(
            submit_offline_links,
            "提交115离线任务失败",
        )
        failure_message = response_message(queued, "115离线任务提交失败")
        duplicate_task = "任务已存在" in failure_message or "重复" in failure_message
        if (
            not response_ok(queued)
            and duplicate_task
            and offline_retry_cleanup
        ):
            cleared = await asyncio.to_thread(
                p115_call,
                "清理115已完成离线任务记录失败",
                client.clouddownload_task_clear,
                {"flag": 0},
            )
            if not response_ok(cleared):
                raise HTTPException(
                    502,
                    response_message(cleared, "115已完成任务记录清理失败"),
                )
            log_hdhive_follow_event(
                "transfer", "running",
                "115报告离线任务重复；已清空完成任务记录并保留文件，正在重新提交",
                follow_id=event_follow_id, user_id=int(user["id"]),
                tmdb_id=tmdb_id,
                title=str(payload.get("title") or payload.get("resource_title") or ""),
                detail={**event_detail, "offline_clear_flag": 0},
            )
            before_tasks = await asyncio.to_thread(p115_offline_snapshot, client)
            queued = await asyncio.to_thread(
                submit_offline_links,
                "重新提交115离线任务失败",
            )
            retried_completed_task = True
        if not response_ok(queued):
            retry_message = response_message(queued, "115离线任务提交失败")
            if retried_completed_task and (
                "任务已存在" in retry_message or "重复" in retry_message
            ):
                retry_message = (
                    "已清理115已完成任务记录，但该链接仍有任务存在；"
                    "可能仍在进行中，请在115云下载中确认后重试"
                )
            log_hdhive_follow_event(
                "transfer", "failed", retry_message,
                follow_id=event_follow_id, user_id=int(user["id"]),
                tmdb_id=tmdb_id,
                title=str(payload.get("title") or payload.get("resource_title") or ""),
                detail={**event_detail, "offline_retry": retried_completed_task},
            )
            raise HTTPException(502, retry_message)
        changed = await asyncio.to_thread(
            wait_for_p115_change,
            lambda: p115_offline_snapshot(client),
            before_tasks,
        )
        if not changed:
            raise HTTPException(
                502,
                "115接口没有创建云下载任务；返回：" + response_summary(queued),
            )
        mode = "offline"
        message = (
            "已保留原文件并重新加入115离线下载"
            if retried_completed_task
            else (
                "已加入115离线下载，完成后由CloudDrive2同步到123"
                if user.get("storage_destination") == "p123"
                else "已加入115离线下载，完成后会出现在所选目录"
            )
        )
        if episode_links_filtered and selected_episode_numbers:
            message += (
                "：第"
                f"{compact_episode_numbers(set(selected_episode_numbers))}集"
            )
        workflow_state = "submitted"
        register_p115_offline_monitor(
            workflow_job_id=int(job["id"]), user_id=int(user["id"]),
            follow_id=event_follow_id,
            destination=str(user["storage_destination"]), source="hdhive",
            resource_key=slug, tmdb_id=tmdb_id, media_type=media_type,
            season_number=int(title_spec["season_number"]),
            episode_numbers=selected_episode_numbers,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            target_cid=str(target_cid), links=selected_links,
        )
    else:
        before_files = await asyncio.to_thread(
            p115_folder_snapshot,
            client,
            target_cid,
        )
        selected_ids: list[str] = []
        completed_episodes: set[int] = set()
        selected_incremental: set[tuple[int, int]] = set()
        if media_type == "tv" and wanted_episodes:
            season_number = int(title_spec["season_number"])
            tree = await asyncio.to_thread(p115_share_tree, client, share_url)
            available_keys: set[tuple[int, int]] = set()
            for item in tree:
                if item.get("_share_is_dir"):
                    continue
                parsed = parse_episode_spec(item.get("_share_name"))
                seasons = {
                    int(value)
                    for value in parsed.get("season_numbers") or []
                    if int(value) > 0
                }
                if len(seasons) > 1:
                    continue
                item_season = next(iter(seasons), season_number)
                available_keys.update(
                    (item_season, int(episode))
                    for episode in parsed.get("episode_numbers") or []
                    if int(episode) > 0
                )
            requested_keys = {
                (season_number, episode) for episode in wanted_episodes
            }
            candidate_keys = (available_keys & requested_keys) or requested_keys
            completed_keys: set[tuple[int, int]] = set()
            for candidate_season in {season for season, _episode in candidate_keys}:
                candidate_episodes = {
                    episode
                    for season, episode in candidate_keys
                    if season == candidate_season
                }
                completed_keys.update(
                    (candidate_season, episode)
                    for episode in completed_episode_numbers(
                        tmdb_id, candidate_season, candidate_episodes
                    )
                )
            completed_episodes = {
                episode
                for candidate_season, episode in completed_keys
                if candidate_season == season_number
            }
            missing_keys = candidate_keys - completed_keys
            if missing_keys:
                selected, selected_incremental = (
                    select_largest_missing_episode_files_by_season(
                        tree,
                        missing_keys,
                        fallback_season=season_number,
                    )
                )
                selected_ids = [
                    str(item.get("_share_id") or "")
                    for item in selected
                    if item.get("_share_id")
                ]
                selected_episode_numbers = sorted(
                    episode for _season, episode in selected_incremental
                )
            elif completed_keys:
                raise HTTPException(409, "这个分享中可识别的剧集都已转存")
            if missing_keys and completed_keys and not selected_ids:
                raise HTTPException(502, "115分享中没有找到可安全增量转存的新集文件")
        if not selected_ids:
            snap = await asyncio.to_thread(
                p115_call,
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
        received = await asyncio.to_thread(
            p115_call,
            "接收115分享失败",
            client.share_receive,
            {"file_id": ",".join(selected_ids), "cid": target_cid},
            share_url=share_url,
        )
        if not response_ok(received):
            raise HTTPException(502, response_message(received, "115转存失败"))
        changed = await asyncio.to_thread(
            wait_for_p115_change,
            lambda: p115_folder_snapshot(client, target_cid),
            before_files,
        )
        if not changed:
            raise HTTPException(
                502,
                "115接口没有把文件写入目标目录；返回：" + response_summary(received),
            )
        mode = "share"
        if completed_episodes and selected_episode_numbers:
            message = (
                "已增量转存第"
                f"{compact_episode_numbers(set(selected_episode_numbers))}集"
            )
            update_workflow_job_episodes(
                int(job["id"]),
                int(title_spec["season_number"]),
                set(selected_episode_numbers),
            )
        else:
            message = (
                "已转存到115中转目录，等待CloudDrive2同步到123"
                if user.get("storage_destination") == "p123"
                else "已转存"
            )
        workflow_state = "waiting_library"

    record_transfer(
        user_id=int(user["id"]),
        source="hdhive",
        resource_key=slug,
        tmdb_id=tmdb_id,
        transfer_scope=transfer_scope,
        status="submitted" if mode == "offline" else "success",
        detail=message,
        season_number=int(title_spec["season_number"]),
        episode_numbers=sorted(set(selected_episode_numbers)),
    )
    send_notifications_async(
        f"☁️ 影巢资源{'已加入离线下载' if mode == 'offline' else '转存成功'}\n\n"
        f"{payload.get('title') or '影片'} · {user['display_name']}\n{message}"
    )
    update_workflow_job(int(job["id"]), workflow_state, message)
    log_hdhive_follow_event(
        "transfer", "success", f"影巢资源{message}",
        follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
        title=str(payload.get("title") or payload.get("resource_title") or ""),
        detail={**event_detail, "episode_numbers": selected_episode_numbers},
    )
    return {
        "ok": True,
        "mode": mode,
        "message": message,
        "job_id": int(job["id"]),
        "workflow_state": workflow_state,
    }


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
    with db() as connection:
        follow_row = connection.execute(
            "SELECT id FROM tv_follows WHERE user_id = ? AND tmdb_id = ? "
            "AND active = 1",
            (int(user["id"]), tmdb_id),
        ).fetchone()
    event_follow_id = int(follow_row["id"]) if follow_row else None
    resource_status = management_resource_status(payload, bool(follow_row))
    event_detail = {
        "source": "dian",
        "resource_key": resource_key,
        "resource_title": str(payload.get("resource_title") or ""),
        "media_type": media_type,
        "resource_status": resource_status,
        "season_number": int(title_spec["season_number"]),
        "episode_numbers": selected_episode_numbers,
    }
    job = begin_workflow_job(
        user_id=int(user["id"]),
        destination=str(user["storage_destination"]),
        source="dian",
        resource_key=resource_key,
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=str(payload.get("title") or payload.get("resource_title") or ""),
        season_number=int(title_spec["season_number"]),
        episode_numbers=selected_episode_numbers,
        follow_id=event_follow_id,
        scope=transfer_scope,
    )
    attach_workflow_job_to_request(request, int(job["id"]))
    log_hdhive_follow_event(
        "unlock", "running", "正在解锁癫影资源",
        follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
        title=str(payload.get("title") or payload.get("resource_title") or ""),
        detail=event_detail,
    )
    try:
        unlocked = await asyncio.to_thread(
            dian_call,
            "unlock",
            {"share_id": share_id, "resource_id": resource_id},
        )
    except Exception as error:
        log_hdhive_follow_event(
            "unlock", "failed", f"癫影资源解锁失败：{error}",
            follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            detail=event_detail,
        )
        raise
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
        log_hdhive_follow_event(
            "unlock", "failed", "癫影解锁后没有返回可用链接",
            follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            detail=event_detail,
        )
        raise HTTPException(
            502,
            "癫影 unlock 返回的 payload 中没有可用链接；"
            f"payload 类型：{payload_type}；payload 字段：{payload_fields}",
        )
    share_url = links[0]
    log_hdhive_follow_event(
        "unlock", "success", "癫影资源解锁成功，正在提交到网盘",
        follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
        title=str(payload.get("title") or payload.get("resource_title") or ""),
        detail=event_detail,
    )
    delivery = p123_delivery_settings()
    if user.get("storage_destination") == "p123" and delivery["mode"] == "telegram":
        result = await deliver_to_pansave(
            user=user,
            share_url=share_url,
            source="dian",
            resource_key=resource_key,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            tmdb_id=tmdb_id,
            season_number=int(title_spec["season_number"]),
            episode_numbers=selected_episode_numbers,
        )
        update_workflow_job(
            int(job["id"]), "waiting_library", "链接已发送123，等待整理与入库"
        )
        log_hdhive_follow_event(
            "transfer", "success", "癫影资源链接已发送123，等待整理与入库",
            follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            detail=event_detail,
        )
        return {**result, "job_id": int(job["id"]), "workflow_state": "waiting_library"}

    client = await asyncio.to_thread(p115_client)
    with db() as connection:
        target_cid = (
            delivery["target_cid"]
            if user.get("storage_destination") == "p123"
            else setting(connection, "p115_target_cid") or "0"
        )
    if not is_115_share_url(share_url):
        before_tasks = await asyncio.to_thread(p115_offline_snapshot, client)
        if len(links) == 1:
            queued = await asyncio.to_thread(
                p115_call,
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
            queued = await asyncio.to_thread(
                p115_call,
                "批量提交115离线任务失败",
                client.clouddownload_task_add_urls,
                offline_payload,
            )
        if not response_ok(queued):
            raise HTTPException(502, response_message(queued, "115离线任务提交失败"))
        changed = await asyncio.to_thread(
            wait_for_p115_change,
            lambda: p115_offline_snapshot(client),
            before_tasks,
        )
        if not changed:
            raise HTTPException(
                502,
                "115接口没有创建云下载任务；返回：" + response_summary(queued),
            )
        send_notifications_async(
            f"☁️ 115离线任务已提交\n\n{payload.get('title') or '影片'} · {user['display_name']}"
        )
        record_transfer(
            user_id=int(user["id"]),
            source="dian",
            resource_key=resource_key,
            tmdb_id=tmdb_id,
            transfer_scope=transfer_scope,
            status="submitted",
            detail=f"已加入115离线下载（{len(links)}个任务）",
            season_number=int(title_spec["season_number"]),
            episode_numbers=selected_episode_numbers,
        )
        update_workflow_job(
            int(job["id"]), "submitted", f"已创建{len(links)}个115离线任务"
        )
        register_p115_offline_monitor(
            workflow_job_id=int(job["id"]), user_id=int(user["id"]),
            follow_id=event_follow_id,
            destination=str(user["storage_destination"]), source="dian",
            resource_key=resource_key, tmdb_id=tmdb_id, media_type=media_type,
            season_number=int(title_spec["season_number"]),
            episode_numbers=selected_episode_numbers,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            target_cid=str(target_cid), links=links,
        )
        log_hdhive_follow_event(
            "transfer", "success", f"癫影资源已创建{len(links)}个115离线任务",
            follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
            title=str(payload.get("title") or payload.get("resource_title") or ""),
            detail=event_detail,
        )
        return {
            "ok": True,
            "mode": "offline",
            "message": (
                f"已加入115离线下载（{len(links)}个任务），完成后由CloudDrive2同步到123"
                if user.get("storage_destination") == "p123"
                else f"已加入115离线下载（{len(links)}个任务），完成后会出现在所选目录"
            ),
            "job_id": int(job["id"]),
            "workflow_state": "submitted",
        }

    before_files = await asyncio.to_thread(p115_folder_snapshot, client, target_cid)
    snap = await asyncio.to_thread(
        p115_call,
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
    received = await asyncio.to_thread(
        p115_call,
        "接收115分享失败",
        client.share_receive,
        {"file_id": file_ids, "cid": target_cid},
        share_url=share_url,
    )
    if not response_ok(received):
        raise HTTPException(502, response_message(received, "115转存失败"))
    changed = await asyncio.to_thread(
        wait_for_p115_change,
        lambda: p115_folder_snapshot(client, target_cid),
        before_files,
    )
    if not changed:
        raise HTTPException(
            502,
            "115接口没有把文件写入目标目录；返回：" + response_summary(received),
        )
    send_notifications_async(
        f"☁️ 癫影资源转存成功\n\n"
        f"{payload.get('title') or '影片'} · {user['display_name']}\n已转存"
    )
    record_transfer(
        user_id=int(user["id"]),
        source="dian",
        resource_key=resource_key,
        tmdb_id=tmdb_id,
        transfer_scope=transfer_scope,
        status="success",
        detail=(
            "已转存到115中转目录，等待CloudDrive2同步到123"
            if user.get("storage_destination") == "p123"
            else "已转存"
        ),
        season_number=int(title_spec["season_number"]),
        episode_numbers=sorted(set(selected_episode_numbers)),
    )
    update_workflow_job(int(job["id"]), "waiting_library", "已确认115转存，等待入库")
    log_hdhive_follow_event(
        "transfer", "success", "癫影资源已确认115转存，等待入库",
        follow_id=event_follow_id, user_id=int(user["id"]), tmdb_id=tmdb_id,
        title=str(payload.get("title") or payload.get("resource_title") or ""),
        detail=event_detail,
    )
    return {
        "ok": True,
        "mode": "share",
        "message": (
            "已转存到115中转目录，等待CloudDrive2同步到123"
            if user.get("storage_destination") == "p123"
            else "已转存"
        ),
        "job_id": int(job["id"]),
        "workflow_state": "waiting_library",
    }


@APP.get("/api/requests")
def list_requests(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    user = require_user(movie_session)
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
    canonical = await asyncio.to_thread(
        tmdb_get,
        f"/{media_type}/{tmdb_id}",
        {"language": "zh-CN"},
    )
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
    send_notifications_async(
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
            "UPDATE movie_requests SET status = ?, admin_note = ?, "
            "completed_at = CASE WHEN ? = 'available' THEN ? ELSE '' END, "
            "updated_at = ? WHERE id = ?",
            (status, note, status, now_iso(), now_iso(), request_id),
        )
    message = f"📌 求片状态更新\n\n{row['title']} → {STATUS_NAMES[status]}\n申请人：{row['display_name']}"
    if note:
        message += f"\n回复：{note}"
    send_notifications_async(message)
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
        emby_library_notification_enabled_value = (
            setting(connection, "emby_library_notification_enabled") != "0"
        )
        p123_emby_library_notification_enabled_value = (
            setting(connection, "p123_emby_library_notification_enabled") != "0"
        )
        emby_webhook_enabled_value = setting(connection, "emby_webhook_enabled") == "1"
        p123_emby_webhook_enabled_value = (
            setting(connection, "p123_emby_webhook_enabled") == "1"
        )
        emby_webhook_token_value = setting(connection, "emby_webhook_token")
        p123_emby_webhook_token_value = setting(
            connection, "p123_emby_webhook_token"
        )
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
        p123_delivery_mode = setting(connection, "p123_delivery_mode") or "telegram"
        p123_staging_cid = setting(connection, "p123_staging_cid") or "0"
        p123_staging_name = setting(connection, "p123_staging_name") or "根目录"
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
        "tmdb_token": tmdb,
        "telegram_configured": bool(telegram and chat_id),
        "telegram_token": telegram,
        "telegram_chat_id": chat_id,
        "emby_configured": bool(emby_url and emby_key),
        "emby_url": emby_url,
        "emby_api_key": emby_key,
        "emby_library_notification_enabled": emby_library_notification_enabled_value,
        "emby_webhook_enabled": emby_webhook_enabled_value,
        "emby_webhook_url": (
            f"{site_public_url.rstrip('/')}/api/emby-webhook/p115/"
            f"{quote(emby_webhook_token_value, safe='')}"
        ),
        "p123_emby_configured": bool(p123_emby_url and p123_emby_key),
        "p123_emby_url": p123_emby_url,
        "p123_emby_api_key": p123_emby_key,
        "p123_emby_library_notification_enabled": (
            p123_emby_library_notification_enabled_value
        ),
        "p123_emby_webhook_enabled": p123_emby_webhook_enabled_value,
        "p123_emby_webhook_url": (
            f"{site_public_url.rstrip('/')}/api/emby-webhook/p123/"
            f"{quote(p123_emby_webhook_token_value, safe='')}"
        ),
        "telegram_proxy": telegram_proxy,
        "dian_configured": bool(dian_base_url and dian_key),
        "dian_base_url": dian_base_url,
        "dian_api_key": dian_key,
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
        "p123_delivery_mode": p123_delivery_mode,
        "p123_staging_cid": p123_staging_cid,
        "p123_staging_name": p123_staging_name,
        "pansave_configured": bool(
            pansave_api_id and pansave_api_hash_cipher and pansave_phone
        ),
        "pansave_connected": bool(pansave_session_cipher and pansave_authorized),
        "pansave_telegram_api_id": pansave_api_id,
        "pansave_telegram_api_hash_configured": bool(pansave_api_hash_cipher),
        "pansave_telegram_api_hash": (
            decrypt_secret(pansave_api_hash_cipher)
            if pansave_api_hash_cipher
            else ""
        ),
        "pansave_telegram_phone": pansave_phone,
        "pansave_bot_username": pansave_bot_username,
        "pansave_telegram_proxy": pansave_proxy_url,
        "wecom_configured": bool(wecom_corp_id and wecom_agent_id and wecom_secret),
        "wecom_callback_configured": bool(callback_token and encoding_key),
        "wecom_corp_id": wecom_corp_id,
        "wecom_agent_id": wecom_agent_id,
        "wecom_secret": wecom_secret,
        "wecom_to_user": wecom_to_user,
        "wecom_api_base": wecom_api_base,
        "wecom_admin_userid": wecom_admin_userid,
        "wecom_callback_token": callback_token,
        "wecom_encoding_aes_key": encoding_key,
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
    if payload.get("p123_delivery_mode") not in (None, "", "telegram", "p115"):
        raise HTTPException(400, "123交付方式无效")
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
            "p123_delivery_mode", "p123_staging_cid", "p123_staging_name",
            "wecom_corp_id", "wecom_agent_id",
            "wecom_secret", "wecom_to_user", "wecom_api_base",
            "wecom_admin_userid", "wecom_callback_token",
            "wecom_encoding_aes_key", "site_public_url",
        ):
            if key in payload and str(payload[key]).strip():
                set_setting(connection, key, payload[key])
        if "dian_signin_enabled" in payload:
            set_setting(connection, "dian_signin_enabled", "1" if payload["dian_signin_enabled"] else "0")
        for key, destination in (
            ("emby_library_notification_enabled", "p115"),
            ("p123_emby_library_notification_enabled", "p123"),
        ):
            if key not in payload:
                continue
            was_enabled = setting(connection, key) != "0"
            is_enabled = bool(payload[key])
            set_setting(connection, key, "1" if is_enabled else "0")
            if not is_enabled or not was_enabled:
                connection.execute(
                    "DELETE FROM emby_library_monitor_state WHERE destination = ?",
                    (destination,),
                )
        for key in ("emby_webhook_enabled", "p123_emby_webhook_enabled"):
            if key in payload:
                set_setting(connection, key, "1" if payload[key] else "0")
    await asyncio.gather(
        asyncio.to_thread(configure_telegram_menu),
        asyncio.to_thread(configure_wecom_menu),
    )
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
    result = await asyncio.to_thread(
        perform_dian_signin,
        mode,
        source="manual",
    )
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
    token_response = await asyncio.to_thread(
        P115Client.login_qrcode_token,
        app_name,
    )
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
    completed = sync_emby_requests(force=True, destination=destination)
    return {"ok": True, "completed": completed, "removed": completed}


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
