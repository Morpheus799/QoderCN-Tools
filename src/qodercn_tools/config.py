"""Runtime configuration, driven by a project-root .env file (with real
environment variables taking precedence).

Recognised variables (see .env.example):
  API_KEY_FILE   path (relative to project) to a key file, one key per line, # comments.
                 Empty/unset => no auth. Set but no valid keys => startup error.
  RM_BLIND_WM    true/false — remove the invisible blind watermark from generated images.
  RM_EXIF_INFO   true/false — strip the AIGC tracking metadata from generated images.
  IMAGEGEN_URL / WEBSEARCH_URL / IMAGESEARCH_URL
                 route path for each tool. Unset => that tool is not exposed.
                 All unset, an invalid path, or a collision => startup error.
  PORT           listen port. -1 => auto-pick a free port. Occupied => startup error.
  IP             bind address (e.g. 127.0.0.1 or 0.0.0.0).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .cosy import DEFAULT_COSY_VERSION
from .gateway import DEFAULT_BASE_URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# (service name, env var, example/default path)
SERVICES: tuple[tuple[str, str, str], ...] = (
    ("imageGen", "IMAGEGEN_URL", "/imageGen"),
    ("webSearch", "WEBSEARCH_URL", "/webSearch"),
    ("imageSearch", "IMAGESEARCH_URL", "/imageSearch"),
)
RESERVED_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._~/-]*$")
# API keys may only contain letters, digits and _ - @ + = & * (avoid header/URL
# characters that trip up clients), and be at most 50 chars. Anything else fails startup.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_@+=&*-]+$")
_API_KEY_MAX_LEN = 50

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    ip: str = "127.0.0.1"
    port: int = 8790  # -1 => auto-pick a free port
    routes: dict[str, str] = field(default_factory=dict)  # service name -> path
    auth_enabled: bool = False
    api_keys: list[str] = field(default_factory=list)
    rm_blind_wm: bool = False
    rm_exif_info: bool = True
    # upstream (advanced; sensible defaults)
    base_url: str = DEFAULT_BASE_URL
    auth_file: str | None = None
    cosy_version: str = DEFAULT_COSY_VERSION
    proxy_url: str | None = None
    timeout: float = 60.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _resolve_routes() -> dict[str, str]:
    routes: dict[str, str] = {}
    seen: dict[str, str] = {}  # path -> service (collision detection)
    for name, var, _default in SERVICES:
        raw = os.environ.get(var)
        if raw is None:
            continue  # unset => do not expose this service
        path = raw.strip()
        if not path:
            continue  # empty => treated as unset
        if not _ROUTE_RE.match(path) or ".." in path:
            raise ConfigError(f"{var}={raw!r} is not a valid route path (must start with '/')")
        if path in RESERVED_PATHS:
            raise ConfigError(f"{var}={path!r} collides with a reserved path ({', '.join(sorted(RESERVED_PATHS))})")
        if path in seen:
            raise ConfigError(f"{var} route {path!r} collides with {seen[path]}")
        seen[path] = var
        routes[name] = path
    if not routes:
        raise ConfigError(
            "no services enabled: set at least one of IMAGEGEN_URL / WEBSEARCH_URL / IMAGESEARCH_URL"
        )
    return routes


def _load_api_keys() -> tuple[bool, list[str]]:
    raw = os.environ.get("API_KEY_FILE")
    if raw is None or not raw.strip():
        return False, []  # unset/empty => no auth
    path = Path(raw.strip())
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise ConfigError(f"API_KEY_FILE not found: {path}")

    keys: list[str] = []
    bad_line_nums: list[int] = []
    long_line_nums: list[int] = []
    bad_chars: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        key = line.strip()
        if not key or key.startswith("#"):
            continue
        if len(key) > _API_KEY_MAX_LEN:
            long_line_nums.append(lineno)
            continue
        if not _API_KEY_RE.match(key):
            bad_line_nums.append(lineno)
            bad_chars.update(c for c in key if not _API_KEY_RE.match(c))
            continue
        keys.append(key)

    # report line numbers + offending characters only, never the key material
    problems: list[str] = []
    if long_line_nums:
        problems.append(f"key(s) longer than {_API_KEY_MAX_LEN} chars on line(s) {long_line_nums}")
    if bad_line_nums:
        problems.append(f"invalid character(s) {sorted(bad_chars)} on line(s) {bad_line_nums}")
    if problems:
        raise ConfigError(
            f"API_KEY_FILE {path}: " + "; ".join(problems)
            + f"; keys may only contain A-Za-z0-9_-@+=&* and be at most {_API_KEY_MAX_LEN} chars"
        )
    if not keys:
        raise ConfigError(f"API_KEY_FILE {path} contains no valid keys")
    return True, keys


def _parse_port() -> int:
    raw = os.environ.get("PORT")
    if raw is None or not raw.strip():
        return 8790
    try:
        port = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"PORT must be an integer, got {raw!r}") from exc
    if port == -1:
        return -1
    if not (1 <= port <= 65535):
        raise ConfigError(f"PORT out of range: {port}")
    return port


def load_settings(env_file: str | None = None) -> Settings:
    """Load .env (from project root by default) then resolve + validate settings."""
    path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if path.is_file():
        load_dotenv(path, override=False)

    auth_enabled, api_keys = _load_api_keys()
    return Settings(
        ip=os.environ.get("IP", "127.0.0.1").strip() or "127.0.0.1",
        port=_parse_port(),
        routes=_resolve_routes(),
        auth_enabled=auth_enabled,
        api_keys=api_keys,
        rm_blind_wm=_env_bool("RM_BLIND_WM", False),
        rm_exif_info=_env_bool("RM_EXIF_INFO", True),
        base_url=os.environ.get("QODERCN_BASE_URL")
        or os.environ.get("LINGMA_REMOTE_BASE_URL")
        or DEFAULT_BASE_URL,
        auth_file=os.environ.get("QODERCN_AUTH_FILE") or os.environ.get("LINGMA_AUTH_FILE"),
        cosy_version=os.environ.get("QODERCN_COSY_VERSION", DEFAULT_COSY_VERSION),
        proxy_url=os.environ.get("QODERCN_UPSTREAM_PROXY") or os.environ.get("LINGMA_REMOTE_PROXY_URL"),
        timeout=float(os.environ.get("QODERCN_TIMEOUT", "60")),
    )
