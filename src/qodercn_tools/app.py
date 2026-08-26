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
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .auth import check_ws_api_key, require_api_key
from .config import Settings, load_settings
from .credentials import CredentialError
from .gateway import GatewayClient, GatewayError
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

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "services": settings.routes}

    async def web_search(req: WebSearchRequest, request: Request):
        return await request.app.state.gateway.web_search(req.query, req.timeRange, req.contents.model_dump())

    async def image_search(req: ImageSearchRequest, request: Request):
        return await request.app.state.gateway.image_search(req.query, req.count)

    async def polish(req: PolishRequest, request: Request):
        return await request.app.state.gateway.polish(req.text)

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

    post_handlers = {"webSearch": web_search, "imageSearch": image_search, "imageGen": image_gen, "polish": polish}
    for name, path in settings.routes.items():
        if name == "asr":
            app.add_api_websocket_route(path, asr, name="asr")
        else:
            app.add_api_route(
                path, post_handlers[name], methods=["POST"], name=name,
                dependencies=[Depends(require_api_key)],
            )

    return app
