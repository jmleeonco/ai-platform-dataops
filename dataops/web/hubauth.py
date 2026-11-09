"""Trust the hub (CONTRACT §79.2 d-c/d-d): requests the hub proxies to this app carry `Authorization: Bearer <HS256>`
signed with the secret in DATAOPS_HUB_SECRET_FILE (iss hub, aud dataops, sub user:<name>, role <hub role>, iat, exp,
jti; at most 5 min), plus `X-Hub-User` and `X-Hub-Role`.

A valid token makes the request a hub request: the Origin check accepts the hub's host (X-Forwarded-Host), the person
is the token's user, and the role gate applies — admin may write, research (hub lead) reads, general gets 403. The
role and the user come from the signed claims; the X-Hub-* headers can only lower the role, never raise it, and a user
header that disagrees with the token is ignored. An absent, unreadable or invalid token changes nothing: the request
is a direct-site request. No secret file = no hub trust.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

ISSUER, AUDIENCE = "hub", "dataops"
MAX_TTL = 300                   # the hub mints 5-minute tokens
LEEWAY = 30
ROLE_OF_HUB = {"admin": "admin", "lead": "research"}         # any other hub role: general
RANK = {"general": 0, "research": 1, "admin": 2}
_cache: dict = {}


class HubTokenError(Exception):
    pass


def secret(path: Path | None) -> str:
    """The shared secret ('' when unset or unreadable); re-read when the file changes (rotation needs no restart)."""
    if path is None:
        return ""
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache.get("key") != key:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        _cache.update(key=key, value=value)
    return _cache["value"]


def _b64d(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as e:
        raise HubTokenError("malformed token") from e


def verify(token: str, key: str, now: float | None = None) -> dict:
    if not key:
        raise HubTokenError("no hub secret")
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise HubTokenError("malformed token")
    try:
        header, claims = json.loads(_b64d(parts[0])), json.loads(_b64d(parts[1]))
    except (ValueError, UnicodeDecodeError) as e:
        raise HubTokenError("malformed token") from e
    if not isinstance(header, dict) or header.get("alg") != "HS256" or not isinstance(claims, dict):
        raise HubTokenError("only HS256 tokens")
    sig = hmac.new(key.encode("utf-8"), f"{parts[0]}.{parts[1]}".encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, _b64d(parts[2])):
        raise HubTokenError("bad signature")
    aud = claims.get("aud")
    if claims.get("iss") != ISSUER or not (aud == AUDIENCE or (isinstance(aud, list) and AUDIENCE in aud)):
        raise HubTokenError("wrong issuer or audience")
    current = time.time() if now is None else now
    try:
        exp, iat = int(claims["exp"]), int(claims["iat"])
    except (KeyError, TypeError, ValueError) as e:
        raise HubTokenError("no exp/iat") from e
    if exp < current - LEEWAY or iat > current + LEEWAY or exp - iat > MAX_TTL:
        raise HubTokenError("expired or too long-lived")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.startswith("user:") or len(sub) <= 5:
        raise HubTokenError("sub is not user:<name>")
    return claims


def identity(headers, key: str) -> dict | None:
    """{user, role} of a hub request, or None (no / invalid token: the direct site's rules apply)."""
    auth = headers.get("authorization") or ""
    if not auth.lower().startswith("bearer ") or not key:
        return None
    try:
        claims = verify(auth[7:], key)
    except HubTokenError:
        return None
    user = claims["sub"][5:][:64]
    role = ROLE_OF_HUB.get(str(claims.get("role")), "general")
    header_role = (headers.get("x-hub-role") or "").strip()
    if header_role in RANK and RANK[header_role] < RANK[role]:
        role = header_role                       # the header may only lower the signed role
    return {"user": user, "role": role}
