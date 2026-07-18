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
from threading import Lock, Thread
from typing import Any, Optional
from urllib.parse import quote, urlparse

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
CACHE_LOCK = Lock()
TMDB_RESPONSE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
EMBY_LIBRARY_CACHE: dict[str, Any] = {
    "key": "",
    "expires": 0.0,
    "ids": set(),
}


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


def emby_library_tmdb_ids(force: bool = False) -> set[int]:
    with db() as connection:
        base_url = setting(connection, "emby_url")
        api_key = setting(connection, "emby_api_key")
    if not base_url or not api_key:
        return set()
    cache_key = hashlib.sha256(f"{base_url}|{api_key}".encode()).hexdigest()
    now = time.monotonic()
    with CACHE_LOCK:
        if (
            not force
            and EMBY_LIBRARY_CACHE["key"] == cache_key
            and EMBY_LIBRARY_CACHE["expires"] > now
        ):
            return set(EMBY_LIBRARY_CACHE["ids"])
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
            EMBY_LIBRARY_CACHE.update(
                {"key": cache_key, "expires": now + 300, "ids": set(values)}
            )
        return values
    except (requests.RequestException, ValueError, TypeError):
        with CACHE_LOCK:
            if EMBY_LIBRARY_CACHE["key"] == cache_key:
                return set(EMBY_LIBRARY_CACHE["ids"])
        return set()


def sync_emby_requests(force: bool = False) -> int:
    tmdb_ids = emby_library_tmdb_ids(force=force)
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
    params = dict(params)
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
    with CACHE_LOCK:
        cached = TMDB_RESPONSE_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    try:
        response = requests.get(
            f"https://api.themoviedb.org/3{path}",
            params=params,
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        cache_seconds = 3600 if "append_to_response" in cache_params else 300
        with CACHE_LOCK:
            if len(TMDB_RESPONSE_CACHE) >= 512:
                expired = [key for key, value in TMDB_RESPONSE_CACHE.items() if value[0] <= now]
                for key in expired:
                    TMDB_RESPONSE_CACHE.pop(key, None)
                if len(TMDB_RESPONSE_CACHE) >= 512:
                    TMDB_RESPONSE_CACHE.pop(next(iter(TMDB_RESPONSE_CACHE)))
            TMDB_RESPONSE_CACHE[cache_key] = (now + cache_seconds, data)
        return data
    except requests.RequestException as error:
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
    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "original_title": original,
        "year": str(date)[:4],
        "overview": item.get("overview") or "暂无简介",
        "poster_path": poster_path,
        "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else "",
        "rating": round(float(item.get("vote_average") or 0), 1),
        "vote_count": int(item.get("vote_count") or 0),
        "in_library": tmdb_id in library_ids,
    }


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
            "请点击左下角“菜单”，可以查看求片需求、完成情况，或发布片库公告。"
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


@APP.on_event("startup")
def startup() -> None:
    init_db()
    Thread(target=configure_telegram_menu, name="telegram-menu", daemon=True).start()
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
        results.append(tmdb_media_item(item, media_type, library_ids))
    return {"results": results[:20]}


@APP.get("/api/charts/{chart_name}")
def charts(chart_name: str, movie_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    require_user(movie_session)
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
    library_ids = emby_library_tmdb_ids()
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
    require_user(movie_session)
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
    library_ids = emby_library_tmdb_ids()
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
                f"https://image.tmdb.org/t/p/original{data.get('backdrop_path')}"
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
    return basic


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
    return {"ok": True, "updated": sync_emby_requests(force=True)}


if __name__ == "__main__":
    init_db()
    uvicorn.run(APP, host="0.0.0.0", port=PORT)
