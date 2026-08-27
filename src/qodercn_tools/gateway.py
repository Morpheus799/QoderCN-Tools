"""Async client for the QoderCN gateway tools.

web/image search + generateImage POST the Encode=0 envelope
{"payload":"<inner json>","encodeVersion":"1"} (generateImage also carries
sessionId/requestId); voice/polish is a plain signed POST (no envelope); ASR is a
WebSocket. All are cosy-signed per request against the configured base URL.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx
import websockets

from .audio import iter_frames, pcm_duration_ms
from .cosy import DEFAULT_COSY_VERSION, build_headers, compact_json, new_uuid
from .credentials import load_credential

# QoderCN personal accounts talk to gateway.qoder.com.cn; enterprise Lingma uses
# lingma.alibabacloud.com. Override with QODERCN_BASE_URL / --base-url.
DEFAULT_BASE_URL = "https://gateway.qoder.com.cn"

ONE_SEARCH_PATH = "/algo/api/v1/webSearch/oneSearch"
IMAGE_SEARCH_PATH = "/algo/api/v2/service/pro/imageSearch"
GENERATE_IMAGE_PATH = "/algo/api/v2/service/pro/generateImage"
ASR_WS_PATH = "/api/v2/service/ws/asr"
POLISH_PATH = "/algo/api/v2/service/voice/polish"


class GatewayError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"gateway status {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass
class Segment:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class TranscriptResult:
    text: str
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    duration_ms: int = 0


# Candidate per-sentence timestamp keys the gateway (FunASR) may include, in ms.
_TS_START_KEYS = ("begin_time", "start_time", "start", "bg")
_TS_END_KEYS = ("end_time", "stop_time", "end", "ed")


class AsrAccumulator:
    """Reduces the gateway's ASR frames into finalized sentences with best-effort
    timing. Pure/synchronous so it can be unit-tested without a WebSocket.

    Timing priority (see the plan): (1) gateway-provided timestamps when present;
    (2) otherwise the streamed-audio offset at arrival — only meaningful when the
    caller paced the send (used_pacing=True); (3) otherwise a proportional split of
    the total audio duration, applied at result() time.
    """

    def __init__(self) -> None:
        self._sentences: list[Segment] = []
        self._done = False
        self._last_end_ms = 0
        self._have_gateway_ts = False

    @property
    def done(self) -> bool:
        return self._done

    @staticmethod
    def _extract_ts(frame: dict) -> tuple[int | None, int | None]:
        def pick(keys: tuple[str, ...]) -> int | None:
            for k in keys:
                v = frame.get(k)
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    return int(v)
            return None

        return pick(_TS_START_KEYS), pick(_TS_END_KEYS)

    def feed(self, frame: dict, stream_offset_ms: int) -> None:
        ftype = frame.get("type")
        if ftype == "speech_completed":
            text = (frame.get("message") or "").strip()
            if not text:
                return
            start, end = self._extract_ts(frame)
            if start is not None and end is not None:
                self._have_gateway_ts = True
            else:
                start, end = self._last_end_ms, max(stream_offset_ms, self._last_end_ms)
            self._sentences.append(Segment(start_ms=start, end_ms=end, text=text))
            self._last_end_ms = end
        elif ftype == "speech_done":
            status = frame.get("status", 200)
            if status not in (None, 200):
                raise GatewayError(int(status), "asr did not complete cleanly")
            self._done = True
        elif ftype == "speech_err":
            raise GatewayError(int(frame.get("code") or 500), str(frame.get("message") or "asr error"))
        # speech_delta (partial) is intentionally ignored for the final transcript.

    @staticmethod
    def _proportional(segments: list[Segment], total_ms: int) -> list[Segment]:
        if not segments or total_ms <= 0:
            return segments
        total_chars = sum(max(len(s.text), 1) for s in segments)
        out: list[Segment] = []
        cursor = 0
        for i, s in enumerate(segments):
            if i == len(segments) - 1:
                end = total_ms
            else:
                end = cursor + int(total_ms * max(len(s.text), 1) / total_chars)
            out.append(Segment(start_ms=cursor, end_ms=max(end, cursor), text=s.text))
            cursor = out[-1].end_ms
        return out

    def result(self, total_duration_ms: int, language: str | None, used_pacing: bool) -> TranscriptResult:
        segments = list(self._sentences)
        if not self._have_gateway_ts and not used_pacing:
            segments = self._proportional(segments, total_duration_ms)
        text = " ".join(s.text for s in segments).strip()
        return TranscriptResult(
            text=text, segments=segments, language=language, duration_ms=total_duration_ms
        )


def _asr_injected_headers() -> dict[str, str]:
    """Headers the proxy injects into the ASR handshake: the CLI's built-in
    X-Business client identity (04-voice-asr.js) with a random business id, plus a
    random X-Asr-Session-Id. Audio-format headers are the caller's responsibility."""
    biz_id = new_uuid()
    business = json.dumps(
        {"product": "ide", "type": "asr_chat", "id": biz_id,
         "begin_at": int(time.time() * 1000), "name": f"asr_chat-{biz_id}"},
        separators=(",", ":"), ensure_ascii=False,
    )
    return {"X-Asr-Session-Id": new_uuid(), "X-Business": business}


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
        """Open the gateway's ASR WebSocket, cosy-signed.

        Injects the CLI's X-Business identity and a random X-Asr-Session-Id; the
        audio-format headers (SampleRate/Channels/…) are forwarded as-is from the
        caller (their responsibility). Caller headers win over injected ones. The
        signature is a GET on ASR_WS_PATH with empty body. Raises on handshake failure.
        """
        cred = load_credential(self.auth_file)
        headers = build_headers(cred, ASR_WS_PATH, "", self.cosy_version)
        headers.pop("Content-Type", None)
        headers.pop("Accept", None)
        # Layer case-insensitively (avoids duplicate header names, which websockets
        # rejects): cosy auth, then injected identity, then caller headers win.
        by_lower = {k.lower(): k for k in headers}

        def put(k: str, v: str) -> None:
            existing = by_lower.get(k.lower())
            if existing is not None and existing != k:
                headers.pop(existing, None)
            by_lower[k.lower()] = k
            headers[k] = v

        for k, v in _asr_injected_headers().items():
            put(k, v)
        for k, v in (forward_headers or {}).items():
            put(k, v)
        return await websockets.connect(
            self._ws_base() + ASR_WS_PATH,
            additional_headers=headers,
            proxy=self.proxy_url,  # None => no proxy (do not read env)
            open_timeout=self._ws_timeout,
            close_timeout=5,
            max_size=None,  # transcripts are small; audio is client->server only
        )

    async def transcribe(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        channels: int,
        bit_depth: int,
        frame_ms: int,
        language: str | None = None,
        pacing: bool = False,
    ) -> TranscriptResult:
        """Batch-transcribe raw PCM by streaming it over the ASR WebSocket.

        Reuses asr_connect() (declaring the audio format via headers), streams the
        PCM as frames, sends the closing text frame, and reduces the gateway's
        replies into a TranscriptResult. When pacing is True the frames are sent at
        ~1x realtime so result arrival correlates to stream position (better subtitle
        timing when the gateway returns no timestamps). Raises GatewayError on failure.
        """
        forward = {
            "SampleRate": str(sample_rate),
            "Channels": str(channels),
            "BitDepth": str(bit_depth),
            "FrameDurationMs": str(frame_ms),
        }
        if language:
            forward["Accept-Language"] = language

        total_ms = pcm_duration_ms(pcm, sample_rate, channels, bit_depth)
        upstream = await self.asr_connect(forward_headers=forward)
        acc = AsrAccumulator()
        progress = {"sent_ms": 0}

        async def sender() -> None:
            for offset_ms, chunk in iter_frames(pcm, sample_rate, channels, bit_depth, frame_ms):
                await upstream.send(chunk)
                progress["sent_ms"] = offset_ms + frame_ms
                if pacing:
                    await asyncio.sleep(frame_ms / 1000.0)
            progress["sent_ms"] = total_ms
            await upstream.send(json.dumps({"type": "voice_completed", "message": "close by user"}))

        async def receiver() -> None:
            async for frame in upstream:
                if isinstance(frame, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(frame)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(data, dict):
                    acc.feed(data, min(progress["sent_ms"], total_ms))
                    if acc.done:
                        return

        send_task = asyncio.create_task(sender())
        send_exc: Exception | None = None
        try:
            await asyncio.wait_for(receiver(), timeout=self._ws_timeout)
        except asyncio.TimeoutError:
            raise GatewayError(504, "asr transcription timed out")
        finally:
            if not send_task.done():
                send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # surface a send failure only if nothing completed
                send_exc = exc
            await upstream.close()

        if send_exc is not None and not acc.done:
            raise GatewayError(502, f"asr send failed: {send_exc}")
        return acc.result(total_ms, language, used_pacing=pacing)

    async def _post_signed(self, path: str, body: str, params: dict | None = None) -> dict:
        """Cosy-signed POST of a raw JSON body; returns parsed JSON or raises GatewayError."""
        cred = load_credential(self.auth_file)
        headers = build_headers(cred, path, body, self.cosy_version)
        resp = await self._client.post(
            self.base_url + path, params=params, content=body.encode("utf-8"), headers=headers
        )
        if resp.status_code >= 400:
            raise GatewayError(resp.status_code, resp.text[:200])
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(resp.status_code, f"invalid JSON response: {exc}") from exc

    async def _post_encoded(self, path: str, inner: dict, extra_outer: dict | None = None) -> dict:
        outer = {"payload": compact_json(inner), "encodeVersion": "1"}
        if extra_outer:
            outer.update(extra_outer)
        return await self._post_signed(path, compact_json(outer), params={"Encode": "0"})

    async def polish(self, text: str) -> dict:
        """Clean up text via voice/polish (plain signed POST, no Encode envelope).

        session_id/request_id are generated and client_type is "5" (the gateway only
        requires them present and non-empty). Returns the parsed upstream JSON.
        """
        inner = {
            "session_id": new_uuid(),
            "request_id": new_uuid(),
            "client_type": "5",
            "messages": [{"role": "user", "content": f"<transcription>{text}</transcription>"}],
        }
        return await self._post_signed(POLISH_PATH, compact_json(inner))

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
