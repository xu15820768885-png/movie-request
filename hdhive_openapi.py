"""Small, isolated HDHive OpenAPI client for the movie-request service.

The application owns token persistence and encryption.  This module only knows
how to build OAuth URLs and call HDHive, so importing it cannot change the
existing TMDB, Dian, Emby, or 115 flows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import secrets
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urlparse

import requests


@dataclass
class TokenSet:
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_in: int = 0
    refresh_expires_in: int = 0
    scopes: list[str] = field(default_factory=list)


class HDHiveOpenAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "",
        retry_after: int = 0,
        limit_scope: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.limit_scope = limit_scope
        self.payload = payload or {}


class HDHiveOpenAPI:
    """HDHive client with one refresh-and-retry and service-specific proxying."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://re0.me",
        access_token: str = "",
        refresh_token: str = "",
        timeout: int = 20,
        proxy_url: str = "",
        on_token_refresh: Optional[Callable[[TokenSet], None]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token.strip()
        self.refresh_token = refresh_token.strip()
        self.timeout = timeout
        self.proxy_url = proxy_url.strip()
        self.on_token_refresh = on_token_refresh
        self.session = session or requests.Session()
        # HDHive must not accidentally inherit the NAS rotating global proxy.
        self.session.trust_env = False

    def build_authorize_url(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        scope: str,
        state: str = "",
    ) -> str:
        params = {
            "client_id": client_id.strip(),
            "redirect_uri": redirect_uri.strip(),
            "scope": scope.strip(),
            "state": state or secrets.token_urlsafe(32),
        }
        return f"{self.base_url}/openapi/authorize?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        result = self._request_public(
            "POST",
            "/api/public/openapi/oauth/token",
            json_body={
                "grant_type": "authorization_code",
                "code": code.strip(),
                "redirect_uri": redirect_uri.strip(),
            },
        )
        return self._token_set(result)

    def refresh_access_token(self, refresh_token: str = "") -> TokenSet:
        current = (refresh_token or self.refresh_token).strip()
        if not current:
            raise ValueError("refresh_token is required")
        result = self._request_public(
            "POST",
            "/api/public/openapi/oauth/refresh",
            json_body={"refresh_token": current},
        )
        tokens = self._token_set(result)
        self.access_token = tokens.access_token
        self.refresh_token = tokens.refresh_token or current
        if self.on_token_refresh:
            self.on_token_refresh(tokens)
        return tokens

    def revoke_refresh_token(self, refresh_token: str = "") -> dict[str, Any]:
        current = (refresh_token or self.refresh_token).strip()
        if not current:
            raise ValueError("refresh_token is required")
        return self._request_public(
            "POST",
            "/api/public/openapi/oauth/revoke",
            json_body={"refresh_token": current},
        )

    def ping(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/ping")

    def quota(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/quota")

    def usage(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/usage")

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/me")

    def checkin(self, is_gambler: bool = False) -> dict[str, Any]:
        body = {"is_gambler": True} if is_gambler else {}
        return self._request(
            "POST",
            "/api/open/checkin",
            json_body=body,
        )

    def weekly_free_quota(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/vip/weekly-free-quota")

    def resources(self, media_type: str, tmdb_id: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/open/resources/{media_type}/{int(tmdb_id)}",
        )

    def resource_file_list(self, slug: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/open/resources/file-list/{slug.strip()}",
        )

    def unlock(self, slug: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/open/resources/unlock",
            json_body={"slug": slug.strip()},
        )

    def unlock_many(self, slugs: list[str]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/open/resources/unlock",
            json_body={"slugs": [value.strip() for value in slugs if value.strip()]},
        )

    def share(self, slug: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/open/shares/{slug.strip()}",
        )

    def media_page(self, media_url: str) -> str:
        """Read a same-origin movie/TV page to resolve its subscription target."""

        clean = str(media_url or "").strip()
        parsed = urlparse(clean)
        base = urlparse(self.base_url)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
                raise ValueError("media_url must use the configured HDHive origin")
            page_path = parsed.path
        else:
            page_path = parsed.path or clean
        if not page_path.startswith("/"):
            page_path = f"/{page_path}"
        parts = [value for value in page_path.split("/") if value]
        if (
            len(parts) != 2
            or parts[0] not in ("movie", "tv")
            or not parts[1].replace("-", "").replace("_", "").isalnum()
        ):
            raise ValueError("media_url must be an HDHive movie or TV page")

        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url
            else None
        )
        try:
            response = self.session.request(
                "GET",
                self.base_url + page_path,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "movie-request-hdhive/1.0",
                },
                timeout=self.timeout,
                proxies=proxies,
            )
        except requests.RequestException as error:
            raise HDHiveOpenAPIError(f"无法连接影巢：{error}") from error
        if not response.ok:
            raise HDHiveOpenAPIError(
                f"影片页面 HTTP {response.status_code}",
                status=response.status_code,
            )
        return str(response.text or "")

    def subscriptions(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/api/open/subscriptions", params=params)

    def create_subscription(
        self,
        *,
        target_type: str,
        target_id: int,
        target_key: str,
        media_filters: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "target_type": target_type,
            "target_id": int(target_id),
            "target_key": target_key,
        }
        if media_filters:
            body["media_filters"] = media_filters
        return self._request("POST", "/api/open/subscriptions", json_body=body)

    def check_subscription(
        self, *, target_type: str, target_key: str
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/open/subscriptions/check",
            params={"target_type": target_type, "target_key": target_key},
        )

    def delete_subscription(self, subscription_id: int) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/api/open/subscriptions/{int(subscription_id)}"
        )

    def messages(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/api/open/messages", params=params)

    def unread_message_count(self, **params: Any) -> dict[str, Any]:
        return self._request(
            "GET", "/api/open/messages/unread-count", params=params
        )

    def mark_messages_read(self, message_ids: list[int]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/open/messages/read",
            json_body={"ids": [int(value) for value in message_ids]},
        )

    def mark_all_messages_read(self) -> dict[str, Any]:
        return self._request("POST", "/api/open/messages/read-all", json_body={})

    def _request_public(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._do_request(
            method,
            path,
            json_body=json_body,
            include_user=False,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        retry_refresh: bool = True,
    ) -> dict[str, Any]:
        try:
            return self._do_request(
                method,
                path,
                params=params,
                json_body=json_body,
                include_user=True,
            )
        except HDHiveOpenAPIError as error:
            if (
                retry_refresh
                and error.code == "OPENAPI_REFRESH_REQUIRED"
                and self.refresh_token
            ):
                self.refresh_access_token()
                return self._request(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    retry_refresh=False,
                )
            raise

    def _do_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        include_user: bool,
    ) -> dict[str, Any]:
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "movie-request-hdhive/1.0",
        }
        if include_user and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url
            else None
        )
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                params={key: value for key, value in (params or {}).items() if value is not None},
                json=json_body,
                headers=headers,
                timeout=self.timeout,
                proxies=proxies,
            )
        except requests.RequestException as error:
            raise HDHiveOpenAPIError(f"无法连接影巢：{error}") from error
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = {}
        if response.ok:
            return payload if isinstance(payload, dict) else {"data": payload}
        data = payload if isinstance(payload, dict) else {}
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        retry_header = response.headers.get("Retry-After", "")
        retry_after = int(
            retry_header
            or nested.get("retry_after_seconds")
            or data.get("retry_after_seconds")
            or 0
        )
        raise HDHiveOpenAPIError(
            str(data.get("message") or data.get("description") or f"HTTP {response.status_code}"),
            status=response.status_code,
            code=str(data.get("code") or response.status_code),
            retry_after=retry_after,
            limit_scope=str(nested.get("limit_scope") or data.get("limit_scope") or ""),
            payload=data,
        )

    @staticmethod
    def _token_set(response: dict[str, Any]) -> TokenSet:
        payload = response.get("data", response)
        if not isinstance(payload, dict):
            return TokenSet()
        scopes_raw = payload.get("scopes")
        if isinstance(scopes_raw, list):
            scopes = [str(value) for value in scopes_raw]
        else:
            scopes = str(payload.get("scope") or "").split()
        return TokenSet(
            access_token=str(payload.get("access_token") or "").strip(),
            refresh_token=str(payload.get("refresh_token") or "").strip(),
            token_type=str(payload.get("token_type") or "Bearer").strip(),
            expires_in=int(payload.get("expires_in") or 0),
            refresh_expires_in=int(payload.get("refresh_expires_in") or 0),
            scopes=scopes,
        )
