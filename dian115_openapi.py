"""Official sealed Dian115 OpenAPI SDK supplied by the approved developer app."""
import base64
import hashlib
import hmac
import json
import os
import time

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_PKG = {"v":2,"codec":"dian115-openapi-v2","developer_application_id":4,"app_type":"desktop","package_id":"dys_pkg_33bc2910a85e23c7d59b54e447b438df","package_fingerprint":"dys_fp_7cab9042826017387d3fa56e7b1afabd","package_nonce":"yBm2J24xhQBt7SLUtQsUZPrW8AkzcJPKkQKPpQ2w6u8","issued_at":"2026-07-24T03:14:33Z","sdk_identity":"c1d26669456f091ac27e763d2ae280dfcf2118fdb321e07ee0e3671f03a6440df92d047c000c57c810a80387828aa25583a3a8607886512aca53b5f73a6a907fb55f87dbc63afd1d6b30f133aae16498c78ee5dd6cd3d647573d04171b62472687d5b22f8301aa6a94db01720e57eab91f4e8a9ec347ef50052146b69b7fd89d3ced0f6b5925ace281a172b861213da2dba6e4bb8c9735323612188e84404a600f2d7649ba811aa80f54122c99590789be1d493c8a4b7352e70c34514b553155268f61c27a98f1993fa48965101ce9d0842b305d4c9f2cb916f9819ad38e877cb92883a5f2a004733887b6b0c9d57213fd5da17c81b93245c41c0a16a859486dc8c78ebb7c4f2a49b3bf730e8d052e36dd2ffe88ae18c1eb42ddfd9c3b812bbf8fa576847e65bd9980ab8a60a5f5a14b7c73c2c0b01687030ec17a5eda8a8be12c657edd8eac166a99505cffeeba82459cd408f5f26334128ba24afc6b90e6018afa0f7fc66c0bda35838d935d6498fb24e8336c87253617632d258deb5a5ba4ad1d2c7c5a12233a044a2fcd38131d219ea244053d361bd25a2bc3b633a458d0ea14250c1bfed0fbfe9219b73742c917fadb697349505144de8261ab0deb261f409ea91a22cc81adbb9f46642b120935c6","seal":"0tS5db50l1HDS-9KR_KZkRYuDEIDfdakIbQ-g-hZvXIwqfH6YYetvLFGNLoSGM0Jo4O-X7uPiRqrQ8OzQL-9kdvv9oa49ExvYY9-rdhunR8hpuD3K_HVcTpvdbmDfsi4a3MXZJYjQQizVLnq6wXTd21Om6g","key_parts":["bd05lWX0ZPnAuTG27DvJSGSFnX8d0XjpLFlffrgVFLk","EK8z1nrC89GuCNb01x02904JdM7UrCXVdnzvBRlA7Tc","YU20Axs4dy8"]}

def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _from_b64u(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * ((4 - len(data) % 4) % 4))

def _runtime_key() -> bytes:
    a = bytearray(_from_b64u(_PKG["key_parts"][0]))
    r = _from_b64u(_PKG["key_parts"][1])
    mask = r[-11:] + r[:-11]
    key = bytes(x ^ y for x, y in zip(a, mask))
    if hashlib.sha256(key).digest()[:8] != _from_b64u(_PKG["key_parts"][2]):
        raise RuntimeError("OpenAPI SDK package key check failed")
    return key

def _sdk_secret() -> dict:
    raw = _from_b64u(_PKG["seal"])
    plain = AESGCM(_runtime_key()).decrypt(raw[:12], raw[12:], None)
    return json.loads(plain.decode())

_SECRET = _sdk_secret()
CODEC = _PKG["codec"]
SDK_IDENTITY = _PKG["sdk_identity"]
DEVELOPER_APPLICATION_ID = int(_PKG["developer_application_id"])
PACKAGE_ID = _PKG["package_id"]
PACKAGE_FINGERPRINT = _PKG["package_fingerprint"]

def _derive(api_key: str, sdk_token: str, nonce: str, ts: int):
    ikm = (api_key + "\n" + sdk_token).encode()
    salt = hashlib.sha256((api_key + ":" + sdk_token).encode()).digest()
    info = f"{CODEC}:{nonce}:{ts}".encode()
    keymat = HKDF(algorithm=hashes.SHA256(), length=64, salt=salt, info=info).derive(ikm)
    return keymat[:32], keymat[32:]

def _make_proof(kid: str, sdk_identity: str, developer_application_id: int, package_id: str, package_fingerprint: str, nonce: str, ts: int, payload: str, hmac_key: bytes) -> str:
    raw = "\n".join([CODEC, kid, sdk_identity, str(developer_application_id), package_id, package_fingerprint, nonce, str(ts), payload]).encode()
    return _b64u(hmac.new(hmac_key, raw, hashlib.sha256).digest())

def _encrypt(aes_key: bytes, value) -> str:
    nonce = os.urandom(12)
    cipher = AESGCM(aes_key).encrypt(nonce, json.dumps(value, separators=(",", ":")).encode(), None)
    return _b64u(nonce + cipher)

def _decrypt(aes_key: bytes, payload: str):
    raw = _from_b64u(payload)
    return json.loads(AESGCM(aes_key).decrypt(raw[:12], raw[12:], None).decode())

class OpenAPIError(RuntimeError):
    def __init__(self, message, status=None, response=None):
        super().__init__(message)
        self.status = status
        self.response = response or {}
        self.code = self.response.get("code")
        self.limit_scope = self.response.get("limit_scope") or self.response.get("scope")
        self.limit_per_minute = self.response.get("limit_per_minute")

class Dian115OpenAPI:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def call(self, path: str, payload: dict):
        ts = int(time.time())
        nonce = _b64u(os.urandom(18))
        kid = self.api_key[:8]
        sdk_token = _SECRET["sdk_token"]
        aes_key, hmac_key = _derive(self.api_key, sdk_token, nonce, ts)
        body_payload = _encrypt(aes_key, payload or {})
        body = {
            "codec": CODEC, "kid": kid,
            "developer_application_id": DEVELOPER_APPLICATION_ID,
            "sdk_identity": SDK_IDENTITY, "package_id": PACKAGE_ID,
            "package_fingerprint": PACKAGE_FINGERPRINT, "nonce": nonce,
            "ts": ts, "payload": body_payload,
            "proof": _make_proof(kid, SDK_IDENTITY, DEVELOPER_APPLICATION_ID, PACKAGE_ID, PACKAGE_FINGERPRINT, nonce, ts, body_payload, hmac_key),
        }
        resp = requests.post(self.base_url + path, json=body, headers={"X-OpenAPI-Key": self.api_key}, timeout=self.timeout)
        env = resp.json()
        required = ("nonce", "ts", "payload", "proof")
        if any(not env.get(k) for k in required):
            raise RuntimeError("OpenAPI encrypted response envelope missing")
        resp_aes, resp_hmac = _derive(self.api_key, sdk_token, env["nonce"], int(env["ts"]))
        resp_identity = env.get("sdk_identity") or SDK_IDENTITY
        resp_developer_application_id = int(env.get("developer_application_id") or DEVELOPER_APPLICATION_ID)
        resp_package_id = env.get("package_id") or PACKAGE_ID
        resp_package_fingerprint = env.get("package_fingerprint") or PACKAGE_FINGERPRINT
        if resp_developer_application_id != DEVELOPER_APPLICATION_ID:
            raise RuntimeError("OpenAPI response developer application invalid")
        if resp_package_id != PACKAGE_ID or resp_package_fingerprint != PACKAGE_FINGERPRINT:
            raise RuntimeError("OpenAPI response package binding invalid")
        if _make_proof(env["kid"], resp_identity, resp_developer_application_id, resp_package_id, resp_package_fingerprint, env["nonce"], int(env["ts"]), env["payload"], resp_hmac) != env["proof"]:
            raise RuntimeError("OpenAPI response proof invalid")
        decoded = _decrypt(resp_aes, env["payload"])
        if not resp.ok:
            raise OpenAPIError(decoded.get("msg") or decoded.get("message") or env.get("code") or "OpenAPI error", resp.status_code, decoded)
        return decoded

    def signin(self, mode: str = "normal"):
        return self.call("/api/portal/openapi/signin", {"mode": mode})

    def list_shares(self, payload: dict):
        return self.call("/api/portal/openapi/list-shares", payload)

    def unlock(self, payload: dict):
        return self.call("/api/portal/openapi/unlock", payload)

    def check_sharecode(self, share_code: str):
        return self.call("/api/portal/openapi/check-sharecode", {"share_code": share_code})
