"""Authenticated client and parsers for https://www.教父.com (观影).

The site is intentionally kept behind a small adapter: the application stores
credentials/cookies, while this module handles the browser proof-of-work,
login, captcha and resource-page normalization.  It does not run on import.
"""

from __future__ import annotations

import base64
import html
import json
import re
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests


DEFAULT_BASE_URL = "https://www.xn--wcv59z.com"
ALLOWED_HOSTS = {
    "xn--wcv59z.com",
    "www.xn--wcv59z.com",
    "www.xn--74qz10cqsltibh40akss.com",  # www.肖申克的救赎.com
    "www.xn--dpqv20e8ug6r8a.com",       # www.阿甘正传.com
    "www.xn--10vr61a3xc5x3b.com",       # www.盗梦空间.com
    "www.xn--kivn76b41nnhi.com",        # www.星际穿越.com
}
ALLOWED_LINK_SCHEMES = ("magnet:", "ed2k:")


class GuanyingError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class CaptchaChallenge:
    image: str
    text: str
    image_type: str = "png"


def normalize_base_url(value: str) -> str:
    clean = str(value or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlparse(clean if "://" in clean else f"https://{clean}")
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("观影地址必须是 HTTPS 地址")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("观影域名无效") from error
    if hostname not in ALLOWED_HOSTS:
        raise ValueError("观影地址必须使用地址发布页列出的官方域名")
    return f"https://{hostname}"


def link_kind(value: Any) -> str:
    link = str(value or "").strip()
    lower = link.lower()
    if re.match(r"^https?://(?:[^/]+\.)?115(?:cdn)?\.com/", lower):
        return "115"
    if "115.com/s/" in lower or "115cdn.com/s/" in lower:
        return "115"
    if lower.startswith("magnet:?"):
        return "magnet"
    if lower.startswith("ed2k://"):
        return "ed2k"
    return ""


def allowed_links(values: Iterable[Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        link = str(value or "").strip()
        kind = link_kind(link)
        if not kind or link in seen:
            continue
        seen.add(link)
        result.append({"url": link, "kind": kind})
    return result


def _walk_links(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        for match in re.findall(
            r"(?:https?://[^\s\"'<>]+|magnet:\?[^\s\"'<>]+|ed2k://\|file\|[^\s\"'<>]+)",
            html.unescape(value),
            flags=re.I,
        ):
            yield match.rstrip("),.;]")
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_links(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_links(child)


def _json_assignments(document: str) -> list[Any]:
    values: list[Any] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?:_obj\.[A-Za-z0-9_]+|window\.[A-Za-z0-9_]+)\s*=\s*", document):
        fragment = document[match.end():].lstrip()
        if not fragment or fragment[0] not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(fragment)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        values.append(value)
    return values


def extract_media_candidates(document: str) -> list[dict[str, Any]]:
    """Extract movie/TV identifiers from either embedded JSON or normal links."""

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, media_id: Any, title: Any = "", year: Any = "") -> None:
        clean_kind = {"movie": "mv", "film": "mv", "series": "tv"}.get(
            str(kind or "").lower(), str(kind or "").lower()
        )
        clean_id = str(media_id or "").strip()
        if clean_kind not in {"mv", "tv", "ac"} or not clean_id:
            return
        key = (clean_kind, clean_id)
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            "kind": clean_kind,
            "id": clean_id,
            "title": str(title or "").strip(),
            "year": str(year or "")[:4],
        })

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("dir") or value.get("type") or value.get("media_type")
            media_id = value.get("id") or value.get("bid") or value.get("media_id")
            if kind and media_id:
                add(kind, media_id, value.get("title") or value.get("name"), value.get("year"))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for assignment in _json_assignments(document):
        visit(assignment)
    for match in re.finditer(
        r"href=[\"']/(mv|tv|ac)/([A-Za-z0-9_-]+)[\"'][^>]*>(.*?)</a>",
        document,
        flags=re.I | re.S,
    ):
        title = re.sub(r"<[^>]+>", " ", match.group(3))
        add(match.group(1), match.group(2), html.unescape(title).strip())
    return candidates


def normalize_resources(payload: Any, *, media_kind: str, media_id: str) -> list[dict[str, Any]]:
    """Flatten the site's downlist/panlist response into allowed resource rows."""

    raw_items: list[dict[str, Any]] = []

    def visit(value: Any, group: str = "") -> None:
        if isinstance(value, dict):
            link_values = list(_walk_links(value))
            links = allowed_links(link_values)
            if links:
                raw_items.append({"payload": value, "links": links, "group": group})
                return
            for key, child in value.items():
                visit(child, str(key or group))
        elif isinstance(value, list):
            for child in value:
                visit(child, group)
        elif isinstance(value, str):
            links = allowed_links(_walk_links(value))
            if links:
                raw_items.append({"payload": {"title": value}, "links": links, "group": group})

    visit(payload)
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(raw_items):
        item = row["payload"]
        if not isinstance(item, dict):
            item = {}
        title = str(
            item.get("title") or item.get("name") or item.get("text")
            or item.get("remark") or row["group"] or f"观影资源 {index + 1}"
        ).strip()
        for link in row["links"]:
            url = link["url"]
            fingerprint = f"{media_kind}:{media_id}:{link['kind']}:{url}"
            key = re.sub(r"[^A-Za-z0-9]", "", fingerprint.lower())[:160]
            if not key:
                key = str(index)
            digest = __import__("hashlib").sha256(fingerprint.encode()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            resources.append({
                "source": "guanying",
                "resource_key": digest,
                "slug": digest,
                "title": title,
                "share_url": url,
                "links": [url],
                "link_type": link["kind"],
                "share_type_label": {"115": "115", "magnet": "磁力", "ed2k": "ED2K"}[link["kind"]],
                "provider": link["kind"],
                "media_kind": media_kind,
                "media_id": media_id,
                "raw": item,
            })
    return resources


class GuanyingClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 25,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def import_cookies(self, payload: str) -> None:
        if not payload:
            return
        try:
            values = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            cookie = SimpleCookie(); cookie.load(payload)
            values = {key: morsel.value for key, morsel in cookie.items()}
        if isinstance(values, dict):
            for key, value in values.items():
                self.session.cookies.set(str(key), str(value))

    def export_cookies(self) -> str:
        return json.dumps(self.session.cookies.get_dict(), ensure_ascii=False, separators=(",", ":"))

    def _raw(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(
            method,
            urljoin(self.base_url + "/", path.lstrip("/")),
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code >= 500:
            raise GuanyingError("观影服务暂时不可用", status=response.status_code)
        return response

    def ensure_browser_verified(self) -> None:
        page = self._raw("GET", "/")
        if "浏览器安全验证" not in page.text:
            return
        challenge_response = self._raw("GET", "/res/pow")
        try:
            challenge = challenge_response.json()
            modulus = int(str(challenge["N"]), 16)
            result = int(str(challenge["x"]), 16)
            steps = int(challenge["t"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GuanyingError("观影浏览器验证数据无效", code="POW_INVALID") from error
        started = time.monotonic()
        for _index in range(steps):
            result = (result * result) % modulus
        elapsed = time.monotonic() - started
        if elapsed < 3:
            time.sleep(3 - elapsed)
        verified = self._raw("POST", "/res/pow", data={"y": format(result, "x")})
        try:
            success = bool(verified.json().get("success"))
        except (ValueError, json.JSONDecodeError):
            success = False
        if not success:
            raise GuanyingError("观影浏览器安全验证失败", code="POW_FAILED")

    def login(self, username: str, password: str, captcha_code: str = "") -> dict[str, Any]:
        self.ensure_browser_verified()
        response = self._raw("POST", "/user/login", data={
            "username": str(username or "").strip(),
            "password": str(password or ""),
            "cookietime": "10506240",
            "code": str(captcha_code or ""),
            "siteid": "1",
            "dosubmit": "1",
        })
        try:
            result = response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise GuanyingError("观影登录返回了无效数据", status=response.status_code) from error
        if int(result.get("code") or 0) == 200:
            return {"authenticated": True, "cookies": self.export_cookies(), "message": result.get("msg") or "观影登录成功"}
        if result.get("captcha"):
            return {"authenticated": False, "captcha_required": True, "message": result.get("msg") or "需要完成点选验证码"}
        raise GuanyingError(str(result.get("msg") or "观影登录失败"), status=response.status_code, code="LOGIN_FAILED")

    def captcha(self) -> CaptchaChallenge:
        response = self._raw("POST", "/res/captcha/2", data={"webp": ""})
        try:
            payload = response.json()
            image = str(payload["img"])
            text = str(payload["text"])
            image_type = str(payload.get("type") or "png")
            base64.b64decode(image)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GuanyingError("观影验证码读取失败", code="CAPTCHA_INVALID") from error
        return CaptchaChallenge(image=image, text=text, image_type=image_type)

    def verify_captcha(self, points: list[dict[str, Any]], width: int = 350, height: int = 200) -> str:
        clean = []
        for point in points:
            x, y = int(point.get("x") or 0), int(point.get("y") or 0)
            if not (0 <= x <= width and 0 <= y <= height):
                raise GuanyingError("验证码点击位置无效", code="CAPTCHA_POINTS_INVALID")
            clean.append(f"{x},{y}")
        if not clean:
            raise GuanyingError("请按顺序点击验证码文字", code="CAPTCHA_POINTS_REQUIRED")
        info = f"{'-'.join(clean)};{int(width)};{int(height)}"
        response = self._raw("POST", "/res/captcha/2", data={"do": "check", "info": info})
        try:
            success = int(response.json().get("code") or 0) == 200
        except (ValueError, json.JSONDecodeError):
            success = False
        if not success:
            raise GuanyingError("验证码点选错误，请重新尝试", code="CAPTCHA_FAILED")
        return info

    def authenticated(self) -> bool:
        self.ensure_browser_verified()
        response = self._raw("GET", "/")
        return "未登录，访问受限" not in response.text and "/user/login" not in response.url

    def search(self, title: str, *, aliases: Optional[list[str]] = None, year: str = "", media_type: str = "tv") -> list[dict[str, Any]]:
        if not self.authenticated():
            raise GuanyingError("观影登录会话已失效", status=401, code="SESSION_EXPIRED")
        queries = [str(title or "").strip(), *[str(value or "").strip() for value in aliases or []]]
        candidates: list[dict[str, Any]] = []
        for query in dict.fromkeys(value for value in queries if value):
            page = self._raw("GET", "/search", params={"q": query, "type": "", "mode": "1"})
            candidates.extend(extract_media_candidates(page.text))
            if candidates:
                break
        wanted_kind = "tv" if media_type == "tv" else "mv"
        filtered = [item for item in candidates if item["kind"] in ({wanted_kind, "ac"} if wanted_kind == "tv" else {wanted_kind})]
        if year:
            exact = [item for item in filtered if not item.get("year") or str(item.get("year")) == str(year)[:4]]
            if exact:
                filtered = exact
        resources: list[dict[str, Any]] = []
        seen_ids: set[tuple[str, str]] = set()
        for candidate in filtered[:5]:
            key = (candidate["kind"], candidate["id"])
            if key in seen_ids:
                continue
            seen_ids.add(key)
            response = self._raw("GET", f"/res/downurl/{candidate['kind']}/{candidate['id']}")
            try:
                payload: Any = response.json()
            except (ValueError, json.JSONDecodeError):
                payload = {"html": response.text}
            for resource in normalize_resources(payload, media_kind=candidate["kind"], media_id=candidate["id"]):
                resource["media_title"] = candidate.get("title") or title
                resource["media_year"] = candidate.get("year") or year
                resources.append(resource)
        return resources
