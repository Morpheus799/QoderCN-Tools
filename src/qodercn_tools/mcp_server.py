"""MCP server (stdio) exposing the QoderCN gateway tools.

Reuses GatewayClient in-process — no HTTP hop. Tools: web_search, image_search,
polish, image_gen (saves the PNG locally and returns its path, never base64), and
transcribe (reads a LOCAL audio file). Configured by the same .env / environment
variables as the HTTP service (upstream/credential/image settings only — no routes
or inbound auth). Launch with `qodercn-tools-mcp`.

Targets the mcp>=2 SDK (FastMCP was renamed MCPServer). Anticipated failures are
raised as ToolError so their message reaches the model; anything else is a crash
whose text the SDK withholds from the client.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .app import _TRANSCRIPTION_FORMATS, _build_srt, _build_vtt, _verbose_json
from .audio import FRAME_MS, MAX_UPLOAD_BYTES, AudioDecodeError, decode_to_pcm
from .config import Settings, load_settings
from .credentials import CredentialError
from .gateway import GatewayClient, GatewayError

_DATA_URL_PREFIX = "data:image/png;base64,"
_EXPECTED_ERRORS = (GatewayError, CredentialError, AudioDecodeError)

# Set by the server lifespan; tools read them via _gw()/_cfg(). Kept as module
# globals (not via Context) so the tool bodies stay plain, directly callable
# functions that unit tests can drive with a stubbed gateway.
_gateway: GatewayClient | None = None
_settings: Settings | None = None


def _gw() -> GatewayClient:
    if _gateway is None:
        raise ToolError("gateway not initialized (server not started)")
    return _gateway


@asynccontextmanager
async def _lifespan(server: MCPServer):
    global _gateway, _settings
    _settings = load_settings(require_routes=False)
    _gateway = GatewayClient(
        base_url=_settings.base_url,
        auth_file=_settings.auth_file,
        cosy_version=_settings.cosy_version,
        proxy_url=_settings.proxy_url,
        timeout=_settings.timeout,
        asr_idle_timeout=_settings.asr_idle_timeout,
    )
    try:
        yield {"gateway": _gateway}
    finally:
        await _gateway.aclose()
        _gateway = None


mcp = MCPServer("qodercn-tools", version=__version__, lifespan=_lifespan)


def _default_image_dir() -> Path:
    env = os.environ.get("QODERCN_IMAGE_DIR")
    return Path(os.path.expanduser(env)) if env else Path(tempfile.gettempdir()) / "qodercn-images"


def _safe_filename(name: str | None) -> str | None:
    if not name:
        return None
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name.strip()))
    return name or None


@mcp.tool(structured_output=False)
async def web_search(
    query: str,
    time_range: str = "NoLimit",
    main_text: bool = False,
    markdown_text: bool = False,
    summary: bool = True,
) -> dict:
    """Search the web via the QoderCN gateway; returns the raw result JSON.

    time_range filters recency: NoLimit (default), OneDay, OneWeek, OneMonth, OneYear.
    main_text adds each page's full body (~1.6KB/result); markdown_text adds a
    best-effort markdown body; summary (default on) adds an AI summary per result.
    """
    try:
        return await _gw().web_search(
            query, time_range,
            {"mainText": main_text, "markdownText": markdown_text, "summary": summary},
        )
    except _EXPECTED_ERRORS as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(structured_output=False)
async def image_search(query: str, count: int = 5) -> dict:
    """Search for images via the QoderCN gateway; returns result JSON with image URLs.

    count is the number of results (1-10, default 5).
    """
    try:
        return await _gw().image_search(query, count)
    except _EXPECTED_ERRORS as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(structured_output=False)
async def polish(text: str) -> str:
    """Clean up dictation/ASR text: add punctuation and fix casing (and, for English,
    drop filler words / repeats). Does NOT rewrite, translate, or answer. Returns the
    cleaned text.
    """
    try:
        result = await _gw().polish(text)
    except _EXPECTED_ERRORS as exc:
        raise ToolError(str(exc)) from exc
    if isinstance(result, dict):
        content = (result.get("result") or {}).get("content")
        if isinstance(content, str):
            return content
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(structured_output=False)
async def image_gen(
    prompt: str,
    output_dir: str | None = None,
    size: str = "1024x1024",
    model: str = "qmodel_38max",
    filename: str | None = None,
    return_image: bool = False,
) -> Any:
    """Generate an image and SAVE it to a local PNG file; returns the saved path
    (NOT base64, to keep it out of the context).

    output_dir: directory to save into (yours to choose; created if missing; defaults
    to $QODERCN_IMAGE_DIR or a temp dir). size: one of 1024x1024, 1536x1024, 1024x1536,
    768x1024, 1024x768, 1024x1280, 1280x1024, 1024x1792, 1792x1024, 2560x1080.
    return_image=True also returns the image inline so you can see it (costs image
    tokens) — leave false when generating for the user. Tracking metadata / blind
    watermark are stripped per the service's RM_EXIF_INFO / RM_BLIND_WM settings.
    """
    try:
        result = await _gw().generate_image(prompt, size, model)
    except _EXPECTED_ERRORS as exc:
        raise ToolError(str(exc)) from exc

    data = result.get("data") if isinstance(result, dict) else None
    url = data[0].get("url") if isinstance(data, list) and data and isinstance(data[0], dict) else None
    if not isinstance(url, str):
        raise ToolError("gateway returned no image")
    if not url.startswith(_DATA_URL_PREFIX):
        return {"url": url, "note": "gateway returned a non-data URL; not saved locally",
                "size": size, "model": model}

    png = base64.b64decode(url[len(_DATA_URL_PREFIX):])
    from .imageproc import dewatermark_png_bytes, strip_png_metadata  # cheap, local
    if _settings is None or _settings.rm_exif_info:
        png = strip_png_metadata(png)
    if _settings is not None and _settings.rm_blind_wm:
        png = dewatermark_png_bytes(png)

    out_dir = Path(os.path.expanduser(output_dir)) if output_dir else _default_image_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create output_dir {out_dir}: {exc}") from exc
    name = _safe_filename(filename) or f"qodercn-{uuid.uuid4().hex[:8]}.png"
    if not name.lower().endswith(".png"):
        name += ".png"
    path = out_dir / name
    try:
        path.write_bytes(png)
    except OSError as exc:
        raise ToolError(f"cannot write {path}: {exc}") from exc

    info = {"path": str(path), "size": size, "model": model, "bytes": len(png)}
    if return_image:
        return [json.dumps(info, ensure_ascii=False), Image(data=png, format="png")]
    return info


@mcp.tool(structured_output=False)
async def transcribe(
    file_path: str,
    language: str | None = None,
    response_format: str = "text",
    stream_realtime: bool = False,
) -> Any:
    """Transcribe a LOCAL audio file (mp3/m4a/webm/wav/flac/…) via the gateway ASR.

    file_path: path to the audio file on this machine (read directly; ~ expanded).
    response_format: text (default, returns the transcript string), json ({"text":...}),
    verbose_json (with segments), srt or vtt (subtitles). language is an optional hint
    (e.g. en, zh). stream_realtime=true paces streaming to ~1x realtime for better
    subtitle timing when the gateway returns no per-sentence timestamps (slower); leave
    false for fastest transcription — the transcript text is exact either way.
    """
    if response_format not in _TRANSCRIPTION_FORMATS:
        raise ToolError(f"unsupported response_format: {response_format!r}")
    p = Path(os.path.expanduser(file_path))
    if not p.is_file():
        raise ToolError(f"audio file not found: {p}")
    if p.stat().st_size > MAX_UPLOAD_BYTES:
        raise ToolError(f"audio exceeds {MAX_UPLOAD_BYTES} bytes")
    try:
        pcm = decode_to_pcm(p.read_bytes())
        result = await _gw().transcribe(
            pcm.pcm, sample_rate=pcm.sample_rate, channels=pcm.channels,
            bit_depth=pcm.bit_depth, frame_ms=FRAME_MS, language=language, pacing=stream_realtime,
        )
    except _EXPECTED_ERRORS as exc:
        raise ToolError(str(exc)) from exc

    if response_format == "text":
        return result.text
    if response_format == "json":
        return {"text": result.text}
    if response_format == "verbose_json":
        return _verbose_json(result, language)
    if response_format == "srt":
        return _build_srt(result.segments)
    return _build_vtt(result.segments)  # vtt


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
