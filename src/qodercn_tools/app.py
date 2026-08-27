"""FastAPI application exposing the QoderCN gateway tools.

Which tools are exposed, and at what paths, is driven by settings.routes (from
IMAGEGEN_URL / WEBSEARCH_URL / IMAGESEARCH_URL / ASR_URL / POLISH_URL). Request
parameters follow the upstream gateway; only the qoder cosy auth is handled
internally. ASR is a streaming WebSocket proxy; the rest (incl. polish) are typed POST.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import websockets
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .audio import FRAME_MS, MAX_UPLOAD_BYTES, AudioDecodeError, decode_to_pcm
from .auth import check_ws_api_key, require_api_key
from .config import Settings, load_settings
from .credentials import CredentialError
from .gateway import GatewayClient, GatewayError, Segment, TranscriptResult
from .imageproc import dewatermark_data_url, sanitize_data_url

# Client headers not forwarded to the upstream ASR handshake (WS mechanics, inbound
# auth, and cosy-* which are re-injected); everything else passes through.
_WS_DROP_HEADERS = {
    "host", "connection", "upgrade", "content-length", "content-type",
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept",
    "authorization", "x-api-key",
}


# Valid generateImage sizes (from the QoderCN CLI's ImageGen schema); not enforced
# so future sizes still pass through, but documented for callers.
IMAGE_SIZES = [
    "1024x1024", "1536x1024", "1024x1536", "768x1024", "1024x768",
    "1024x1280", "1280x1024", "1024x1792", "1792x1024", "2560x1080",
]


class WebSearchContents(BaseModel):
    """Which extra fields the gateway returns per result (upstream `contents`)."""

    mainText: bool = Field(False, description="Return the full extracted body text of each result page (~1.6KB each).")
    markdownText: bool = Field(False, description="Return the page body as markdown (best-effort; only extracted for some pages).")
    summary: bool = Field(True, description="Return an AI-generated summary per result (populates for almost all results).")


class WebSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    timeRange: str = Field(
        "NoLimit",
        description="Recency filter. Accepted: NoLimit (default), OneDay, OneWeek, OneMonth, OneYear. "
        "Any other value is silently treated as NoLimit by the gateway (no error).",
    )
    contents: WebSearchContents = Field(default_factory=WebSearchContents)


class ImageSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    count: int = Field(5, ge=1, le=10, description="Number of image results (1-10, default 5).")


class ImageGenRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    size: str = Field("1024x1024", description="Aspect-ratio preset, one of: " + ", ".join(IMAGE_SIZES))
    model: str = "qmodel_38max"


class PolishRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Raw dictation/ASR text to clean up (punctuation, casing; no rewrite/translate).")


# --- OpenAI /v1/audio/transcriptions response rendering ----------------------

_TRANSCRIPTION_FORMATS = {"json", "text", "verbose_json", "srt", "vtt"}


def _fmt_ts(ms: int, sep: str) -> str:
    """Format milliseconds as HH:MM:SS<sep>mmm (sep is ',' for SRT, '.' for VTT)."""
    ms = max(int(ms), 0)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{msec:03d}"


def _build_srt(segments: list[Segment]) -> str:
    blocks = [
        f"{i}\n{_fmt_ts(s.start_ms, ',')} --> {_fmt_ts(s.end_ms, ',')}\n{s.text}"
        for i, s in enumerate(segments, 1)
    ]
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def _build_vtt(segments: list[Segment]) -> str:
    blocks = ["WEBVTT"] + [
        f"{_fmt_ts(s.start_ms, '.')} --> {_fmt_ts(s.end_ms, '.')}\n{s.text}" for s in segments
    ]
    return "\n\n".join(blocks) + "\n"


def _verbose_json(result: TranscriptResult, language: str | None) -> dict:
    segments = [
        {
            "id": i, "seek": 0,
            "start": round(s.start_ms / 1000, 3), "end": round(s.end_ms / 1000, 3),
            "text": s.text, "tokens": [], "temperature": 0.0,
            "avg_logprob": 0.0, "compression_ratio": 0.0, "no_speech_prob": 0.0,
        }
        for i, s in enumerate(result.segments)
    ]
    return {
        "task": "transcribe",
        "language": language or result.language or "",
        "duration": round(result.duration_ms / 1000, 3),
        "text": result.text,
        "segments": segments,
    }


def _render_transcription(result: TranscriptResult, response_format: str, language: str | None):
    if response_format == "text":
        return PlainTextResponse(result.text)
    if response_format == "verbose_json":
        return JSONResponse(_verbose_json(result, language))
    if response_format == "srt":
        return PlainTextResponse(_build_srt(result.segments), media_type="application/x-subrip")
    if response_format == "vtt":
        return PlainTextResponse(_build_vtt(result.segments), media_type="text/vtt")
    return JSONResponse({"text": result.text})  # default: json


async def _relay_asr(client: WebSocket, upstream) -> None:
    """Relay WebSocket frames both ways until either side closes."""

    async def client_to_upstream() -> None:
        while True:
            msg = await client.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    async def upstream_to_client() -> None:
        async for frame in upstream:
            if isinstance(frame, (bytes, bytearray)):
                await client.send_bytes(bytes(frame))
            else:
                await client.send_text(frame)

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await upstream.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway = GatewayClient(
            base_url=settings.base_url,
            auth_file=settings.auth_file,
            cosy_version=settings.cosy_version,
            proxy_url=settings.proxy_url,
            timeout=settings.timeout,
            asr_idle_timeout=settings.asr_idle_timeout,
        )
        try:
            yield
        finally:
            await app.state.gateway.aclose()

    app = FastAPI(title="QoderCN Tools", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError):
        return JSONResponse(status_code=502, content={"error": str(exc), "upstream_status": exc.status})

    @app.exception_handler(CredentialError)
    async def _credential_error(_: Request, exc: CredentialError):
        return JSONResponse(status_code=500, content={"error": str(exc)})

    @app.exception_handler(AudioDecodeError)
    async def _audio_error(_: Request, exc: AudioDecodeError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "services": settings.routes}

    async def web_search(req: WebSearchRequest, request: Request):
        return await request.app.state.gateway.web_search(req.query, req.timeRange, req.contents.model_dump())

    async def image_search(req: ImageSearchRequest, request: Request):
        return await request.app.state.gateway.image_search(req.query, req.count)

    async def polish(req: PolishRequest, request: Request):
        return await request.app.state.gateway.polish(req.text)

    async def openai_transcribe(
        request: Request,
        file: UploadFile = File(...),
        model: str = Form("fun-asr-realtime"),
        language: str | None = Form(None),
        response_format: str = Form("json"),
        stream_realtime: bool | None = Form(None),
        prompt: str | None = Form(None),
        temperature: float | None = Form(None),
    ):
        # `model`/`prompt`/`temperature` are accepted for OpenAI compatibility but
        # have no gateway equivalent (the model is fixed fun-asr-realtime).
        if response_format not in _TRANSCRIPTION_FORMATS:
            raise HTTPException(status_code=400, detail=f"unsupported response_format: {response_format!r}")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail=f"audio exceeds {MAX_UPLOAD_BYTES} bytes")
        pcm = decode_to_pcm(data)  # AudioDecodeError -> 400
        pacing = settings.asr_realtime_pacing if stream_realtime is None else stream_realtime
        result = await request.app.state.gateway.transcribe(
            pcm.pcm,
            sample_rate=pcm.sample_rate,
            channels=pcm.channels,
            bit_depth=pcm.bit_depth,
            frame_ms=FRAME_MS,
            language=language,
            pacing=pacing,
        )
        return _render_transcription(result, response_format, language)

    async def image_gen(req: ImageGenRequest, request: Request):
        result = await request.app.state.gateway.generate_image(req.prompt, req.size, req.model)
        if isinstance(result.get("data"), list):
            for item in result["data"]:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    url = item["url"]
                    if settings.rm_exif_info:
                        url = sanitize_data_url(url)
                    if settings.rm_blind_wm:
                        url = dewatermark_data_url(url)
                    item["url"] = url
        return result

    async def asr(websocket: WebSocket):
        """WebSocket route: authenticate, then relay to the gateway ASR endpoint."""
        if not check_ws_api_key(settings, websocket.headers, websocket.query_params):
            await websocket.close(code=1008)
            return
        forward = {
            k: v for k, v in websocket.headers.items()
            if k.lower() not in _WS_DROP_HEADERS and not k.lower().startswith("cosy-")
        }
        gateway = websocket.app.state.gateway
        try:
            upstream = await gateway.asr_connect(forward)
        except (GatewayError, CredentialError, OSError, websockets.exceptions.WebSocketException) as exc:
            await websocket.close(code=1011, reason=str(exc)[:120])
            return
        await websocket.accept()
        try:
            await _relay_asr(websocket, upstream)
        except WebSocketDisconnect:
            await upstream.close()

    post_handlers = {
        "webSearch": web_search, "imageSearch": image_search, "imageGen": image_gen,
        "polish": polish, "transcriptions": openai_transcribe,
    }
    for name, path in settings.routes.items():
        if name == "asr":
            app.add_api_websocket_route(path, asr, name="asr")
        else:
            app.add_api_route(
                path, post_handlers[name], methods=["POST"], name=name,
                dependencies=[Depends(require_api_key)],
            )

    return app
