"""CLI entrypoint: `qodercn-tools` / `python -m qodercn_tools`.

Configuration comes from a project-root .env file (see .env.example) and/or real
environment variables. The only flag is --env-file to point at a different .env.
"""

from __future__ import annotations

import argparse
import socket

import uvicorn

from .app import create_app
from .config import ConfigError, Settings, load_settings


def _resolve_port(ip: str, port: int) -> int:
    """Auto-pick a free port when port == -1, else verify it is not occupied."""
    if port == -1:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((ip, 0))
            return s.getsockname()[1]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((ip, port))  # no SO_REUSEADDR: an active listener reliably fails
        except OSError as exc:
            raise SystemExit(f"PORT {port} on {ip} is already in use: {exc}")
    return port


def _summary(settings: Settings, port: int) -> str:
    auth = f"on ({len(settings.api_keys)} key(s))" if settings.auth_enabled else "OFF"
    routes = ", ".join(f"{p} [{n}]" for n, p in settings.routes.items())
    lines = [
        f"QoderCN Tools listening on {settings.ip}:{port}",
        f"  services : {routes}",
        f"  auth     : {auth}",
        f"  images   : rm_exif={settings.rm_exif_info} rm_blind_wm={settings.rm_blind_wm}",
        f"  upstream : {settings.base_url}",
    ]
    if settings.ip not in ("127.0.0.1", "localhost", "::1") and not settings.auth_enabled:
        lines.append("  WARNING  : bound to a public address with auth OFF")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="qodercn-tools", description="Serve the QoderCN gateway tools over HTTP.")
    parser.add_argument("--env-file", default=None, help="path to a .env file (default: project-root .env)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}")

    port = _resolve_port(settings.ip, settings.port)
    print(_summary(settings, port), flush=True)
    app = create_app(settings)
    uvicorn.run(app, host=settings.ip, port=port)


if __name__ == "__main__":
    main()
