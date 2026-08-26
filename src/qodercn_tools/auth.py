"""Inbound API-key authentication.

Accepts the key via `x-api-key` or `Authorization: Bearer <key>`. Constant-time
compared against the configured keys. Auth is skipped entirely when API_KEY_FILE
is unset/empty (settings.auth_enabled is False). WebSocket routes additionally
accept the key via an `?api_key=`/`?token=` query param (browser WS clients cannot
set headers).
"""

from __future__ import annotations

import hmac
from typing import Mapping

from fastapi import Header, HTTPException, Request


def _extract_key(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        value = authorization.strip()
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return value
    return None


def _matches(candidate: str, keys: list[str]) -> bool:
    return any(hmac.compare_digest(candidate, k) for k in keys)


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
) -> None:
    settings = request.app.state.settings
    if not settings.auth_enabled:
        return
    candidate = _extract_key(x_api_key, authorization)
    if not candidate or not _matches(candidate, settings.api_keys):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def check_ws_api_key(settings, headers: Mapping[str, str], query_params: Mapping[str, str]) -> bool:
    """WebSocket API-key check: key via header or ?api_key=/?token= query."""
    if not settings.auth_enabled:
        return True
    candidate = _extract_key(headers.get("x-api-key"), headers.get("authorization"))
    if not candidate:
        candidate = query_params.get("api_key") or query_params.get("token")
    if not candidate:
        return False
    return _matches(candidate.strip(), settings.api_keys)
