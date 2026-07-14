#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from typing import Any, Optional

import requests
import uvicorn
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "movie-request.db"
WEB_PATH = Path(__file__).parent / "web" / "index.html"
PORT = int(os.getenv("PORT", "5056"))
SESSION_DAYS = 30
STATUS_NAMES = {
    "pending": "待处理",
    "approved": "已收到",
    "searching": "寻找中",
    "available": "已入库",
    "rejected": "暂时无法完成",
}

APP = FastAPI(title="映单", docs_url=None, redoc_url=None)
TELEGRAM_OFFSET = 0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                active INTEGER NOT NULL DEFAULT 1,
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
            """
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


def set_setting(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value or "").strip()),
    )


def session_user(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as connection:
        row = connection.execute(
            "SELECT u.id, u.username, u.display_name, u.role, u.active "
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
    item["poster_url"] = (
        f"https://image.tmdb.org/t/p/w500{item['poster_path']}"
        if item.get("poster_path")
        else ""
    )
    item["status_name"] = STATUS_NAMES.get(item["status"], item["status"])
    return item


def emby_api_url(base_url: str, path: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.lower().endswith("/emby"):
        return f"{base}/{path.lstrip('/')}"
    return f"{base}/emby/{path.lstrip('/')}"


def emby_library_tmdb_ids() -> set[int]:
    with db() as connection:
        base_url = setting(connection, "emby_url")
        api_key = setting(connection, "emby_api_key")
    if not base_url or not api_key:
        return set()
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
        return values
    except (requests.RequestException, ValueError, TypeError):
        return set()


def sync_emby_requests() -> int:
    tmdb_ids = emby_library_tmdb_ids()
    if not tmdb_ids:
        return 0
    placeholders = ",".join("?" for _ in tmdb_ids)
    with db() as connection:
        cursor = connection.execute(
            f"UPDATE movie_requests SET status = 'available', updated_at = ? "
            f"WHERE tmdb_id IN ({placeholders}) AND status != 'available'",
            (now_iso(), *tmdb_ids),
        )
        return cursor.rowcount


def tmdb_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    with db() as connection:
        credential = setting(connection, "tmdb_token")
    if not credential:
        raise HTTPException(503, "管理员还没有配置 TMDB 凭证")
    headers = {"Accept": "application/json"}
    if credential.startswith("eyJ") or len(credential) > 80:
        headers["Authorization"] = f"Bearer {credential}"
    else:
        params["api_key"] = credential
    try:
        response = requests.get(
            f"https://api.themoviedb.org/3{path}",
            params=params,
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        code = getattr(error.response, "status_code", "")
        if code in (401, 403):
            raise HTTPException(502, "TMDB 凭证无效，请管理员检查设置") from error
        raise HTTPException(502, "暂时无法连接 TMDB，请稍后重试") from error


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
            if text in ("求片需求", "/requests") or text.startswith("/requests@"):
                send_telegram(telegram_request_summary(False))
            elif text in ("完成情况", "/completed") or text.startswith("/completed@"):
                sync_emby_requests()
                send_telegram(telegram_request_summary(True))
            elif text in ("/start", "/menu") or text.startswith("/menu@"):
                send_telegram("请点击左下角“菜单”，选择“求片需求”或“完成情况”。")
        if not data:
            time.sleep(3)


def emby_sync_loop() -> None:
    while True:
        try:
            sync_emby_requests()
        except Exception:
            pass
        time.sleep(300)


@APP.on_event("startup")
def startup() -> None:
    init_db()
    Thread(target=telegram_poll_loop, name="telegram-bot", daemon=True).start()
    Thread(target=emby_sync_loop, name="emby-sync", daemon=True).start()


@APP.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_PATH)


@APP.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@APP.get("/api/bootstrap")
def bootstrap(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    with db() as connection:
        ready = bool(connection.execute("SELECT 1 FROM users LIMIT 1").fetchone())
    return {"needs_setup": not ready, "user": session_user(movie_session)}


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
def search(q: str, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_user(movie_session)
    query = q.strip()
    if len(query) < 1:
        return {"results": []}
    data = tmdb_get(
        "/search/multi",
        {"query": query, "language": "zh-CN", "include_adult": "false", "page": 1},
    )
    library_ids = emby_library_tmdb_ids()
    results = []
    for item in data.get("results", []):
        media_type = item.get("media_type")
        if media_type not in ("movie", "tv"):
            continue
        date = item.get("release_date") or item.get("first_air_date") or ""
        title = item.get("title") or item.get("name") or "未命名"
        original = item.get("original_title") or item.get("original_name") or ""
        poster_path = item.get("poster_path") or ""
        results.append(
            {
                "tmdb_id": item.get("id"),
                "media_type": media_type,
                "title": title,
                "original_title": original,
                "year": date[:4],
                "overview": item.get("overview") or "暂无简介",
                "poster_path": poster_path,
                "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else "",
                "rating": round(float(item.get("vote_average") or 0), 1),
                "in_library": int(item.get("id") or 0) in library_ids,
            }
        )
    return {"results": results[:20]}


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
    if tmdb_id in emby_library_tmdb_ids():
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
    send_telegram(
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
    send_telegram(message)
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
    send_telegram(f"🗑️ 求片需求已删除\n\n{actor}：{row['title']} ({row['year']})")
    return {"ok": True}


@APP.get("/api/admin/users")
def list_users(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    with db() as connection:
        rows = connection.execute(
            "SELECT id, username, display_name, role, active, created_at FROM users ORDER BY role, created_at"
        ).fetchall()
    return {"users": [dict(row) for row in rows]}


@APP.post("/api/admin/users")
async def create_user(request: Request, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    username = clean_username(payload.get("username"))
    password = clean_password(payload.get("password"))
    display_name = str(payload.get("display_name") or username).strip()[:40]
    try:
        with db() as connection:
            connection.execute(
                "INSERT INTO users(username, display_name, password_hash, role, created_at) "
                "VALUES(?, ?, ?, 'member', ?)",
                (username, display_name, hash_password(password), now_iso()),
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
    if "display_name" in payload:
        fields.append("display_name = ?")
        values.append(str(payload["display_name"]).strip()[:40])
    if "active" in payload:
        fields.append("active = ?")
        values.append(1 if payload["active"] else 0)
    if payload.get("password"):
        fields.append("password_hash = ?")
        values.append(hash_password(clean_password(payload["password"])))
    if not fields:
        return {"ok": True}
    values.append(user_id)
    with db() as connection:
        connection.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
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
        telegram_proxy = setting(connection, "telegram_proxy")
    return {
        "tmdb_configured": bool(tmdb),
        "telegram_configured": bool(telegram and chat_id),
        "telegram_chat_id": chat_id,
        "emby_configured": bool(emby_url and emby_key),
        "emby_url": emby_url,
        "telegram_proxy": telegram_proxy,
    }


@APP.patch("/api/admin/settings")
async def update_settings(request: Request, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    payload = await request.json()
    with db() as connection:
        for key in (
            "tmdb_token", "telegram_token", "telegram_chat_id", "telegram_proxy",
            "emby_url", "emby_api_key"
        ):
            if key in payload and str(payload[key]).strip():
                set_setting(connection, key, payload[key])
    configure_telegram_menu()
    return {"ok": True}


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
def emby_test(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    with db() as connection:
        base_url = setting(connection, "emby_url")
        api_key = setting(connection, "emby_api_key")
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
        count = len(emby_library_tmdb_ids())
        return {
            "ok": True,
            "server_name": info.get("ServerName") or "Emby",
            "library_tmdb_count": count,
        }
    except requests.RequestException as error:
        raise HTTPException(502, "无法连接 Emby，请检查地址和 API 密钥") from error


@APP.post("/api/admin/emby-sync")
def emby_sync(movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_admin(movie_session)
    return {"ok": True, "updated": sync_emby_requests()}


if __name__ == "__main__":
    init_db()
    uvicorn.run(APP, host="0.0.0.0", port=PORT)
