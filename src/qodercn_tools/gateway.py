"""Async HTTP client for the three QoderCN gateway tools.

All three POST the Encode=0 envelope {"payload":"<inner json>","encodeVersion":"1"}
(generateImage additionally carries sessionId/requestId) to
https://lingma.alibabacloud.com, cosy-signed per request.
"""

from __future__ import annotations

import json
import time

import httpx
import websockets

from .cosy import DEFAULT_COSY_VERSION, build_headers, compact_json, new_uuid
from .credentials import load_credential

# QoderCN personal accounts talk to gateway.qoder.com.cn; enterprise Lingma uses
# lingma.alibabacloud.com. Override with QODERCN_BASE_URL / --base-url.
DEFAULT_BASE_URL = "https://gateway.qoder.com.cn"

ONE_SEARCH_PATH = "/algo/api/v1/webSearch/oneSearch"
IMAGE_SEARCH_PATH = "/algo/api/v2/service/pro/imageSearch"
GENERATE_IMAGE_PATH = "/algo/api/v2/service/pro/generateImage"
ASR_WS_PATH = "/api/v2/service/ws/asr"


class GatewayError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"gateway status {status}: {detail}")
        self.status = status
        self.detail = detail


class GatewayClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        auth_file: str | None = None,
        cosy_version: str = DEFAULT_COSY_VERSION,
        proxy_url: str | None = None,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_file = auth_file
        self.cosy_version = cosy_version
        self.proxy_url = proxy_url
        self._ws_timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout, proxy=proxy_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ws_base(self) -> str:
        if self.base_url.startswith("https://"):
            return "wss://" + self.base_url[len("https://"):]
        if self.base_url.startswith("http://"):
            return "ws://" + self.base_url[len("http://"):]
        return self.base_url

    async def asr_connect(self, forward_headers: dict[str, str] | None = None):
        """Open the gateway's ASR WebSocket, cosy-signed, forwarding caller headers.

        The signature is a GET on ASR_WS_PATH with empty body (method is not part of
        the cosy preimage). Raises on handshake failure (e.g. bad credentials).
        """
        cred = load_credential(self.auth_file)
        headers = build_headers(cred, ASR_WS_PATH, "", self.cosy_version)
        headers.pop("Content-Type", None)
        headers.pop("Accept", None)
        # Merge case-insensitively to avoid duplicate header names (e.g. User-Agent),
        # which websockets rejects; a forwarded header replaces the same cosy header.
        if forward_headers:
            by_lower = {k.lower(): k for k in headers}
            for k, v in forward_headers.items():
                existing = by_lower.get(k.lower())
                if existing is not None:
                    headers.pop(existing, None)
                headers[k] = v
        url = self._ws_base() + ASR_WS_PATH
        return await websockets.connect(
            url,
            additional_headers=headers,
            proxy=self.proxy_url,  # None => no proxy (do not read env)
            open_timeout=self._ws_timeout,
            close_timeout=5,
            max_size=None,  # transcripts are small; audio is client->server only
        )

    async def _post_encoded(self, path: str, inner: dict, extra_outer: dict | None = None) -> dict:
        outer = {"payload": compact_json(inner), "encodeVersion": "1"}
        if extra_outer:
            outer.update(extra_outer)
        body = compact_json(outer)

        cred = load_credential(self.auth_file)
        headers = build_headers(cred, path, body, self.cosy_version)

        resp = await self._client.post(
            self.base_url + path, params={"Encode": "0"}, content=body.encode("utf-8"), headers=headers
        )
        if resp.status_code >= 400:
            raise GatewayError(resp.status_code, resp.text[:200])
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(resp.status_code, f"invalid JSON response: {exc}") from exc

    async def web_search(self, query: str, time_range: str = "NoLimit", contents: dict | None = None) -> dict:
        inner = {
            "contents": contents or {"mainText": False, "markdownText": False, "summary": True},
            "query": query,
            "timeRange": time_range,
        }
        return await self._post_encoded(ONE_SEARCH_PATH, inner)

    async def image_search(self, query: str, count: int = 5) -> dict:
        if count <= 0 or count > 10:
            count = 5
        inner = {"count": count, "query": query}
        return await self._post_encoded(IMAGE_SEARCH_PATH, inner)

    async def generate_image(self, prompt: str, size: str = "1024x1024", model: str = "qmodel_38max") -> dict:
        size = size or "1024x1024"
        model = model or "qmodel_38max"
        business = {
            "begin_at": int(time.time() * 1000),
            "id": new_uuid(),
            "product": "cli",
            "stage": "start",
            "type": "text2img",
            "version": self.cosy_version,
        }
        inner = {
            "metadata": {"business": business},
            "model": model,
            "prompt": prompt,
            "size": size,
        }
        extra_outer = {"sessionId": new_uuid(), "requestId": new_uuid()}
        return await self._post_encoded(GENERATE_IMAGE_PATH, inner, extra_outer)
