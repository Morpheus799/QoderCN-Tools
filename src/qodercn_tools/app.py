"""FastAPI application exposing the QoderCN gateway tools.

Which tools are exposed, and at what paths, is driven entirely by settings.routes
(from IMAGEGEN_URL / WEBSEARCH_URL / IMAGESEARCH_URL). Request parameters follow the
upstream gateway; only the qoder cosy auth is handled internally.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .auth import require_api_key
from .config import Settings, load_settings
from .credentials import CredentialError
from .gateway import GatewayClient, GatewayError
from .imageproc import dewatermark_data_url, sanitize_data_url


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

    handlers = {"webSearch": web_search, "imageSearch": image_search, "imageGen": image_gen}
    for name, path in settings.routes.items():
        app.add_api_route(
            path, handlers[name], methods=["POST"], name=name, dependencies=[Depends(require_api_key)]
        )

    return app
