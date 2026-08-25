"""Inbound API-key authentication.

Accepts the key via `x-api-key` or `Authorization: Bearer <key>`. Constant-time
compared against the configured keys. Auth is skipped entirely when API_KEY_FILE
is unset/empty (settings.auth_enabled is False).
"""

from __future__ import annotations

import hmac

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
