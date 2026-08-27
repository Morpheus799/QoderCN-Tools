"""Decode uploaded audio to the gateway's raw-PCM format via PyAV.

The gateway ASR WebSocket only accepts 16-bit little-endian PCM at a declared
sample rate. OpenAI transcription clients upload arbitrary containers/codecs
(mp3/m4a/webm/wav/…), so we decode + resample to 16 kHz mono s16le in-process
(PyAV bundles the ffmpeg libraries — no system binary required). av/numpy are
imported lazily inside decode_to_pcm so merely importing this module (and the
gateway that reuses it) stays cheap.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Iterator

TARGET_RATE = 16000
TARGET_CHANNELS = 1
TARGET_BIT_DEPTH = 16
FRAME_MS = 100
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # OpenAI's transcription file-size limit


class AudioDecodeError(RuntimeError):
    """Raised when an upload cannot be decoded to PCM (mapped to HTTP 400)."""


def pcm_duration_ms(
    pcm: bytes,
    sample_rate: int = TARGET_RATE,
    channels: int = TARGET_CHANNELS,
    bit_depth: int = TARGET_BIT_DEPTH,
) -> int:
    bytes_per_ms = sample_rate * channels * (bit_depth // 8) / 1000
    return int(len(pcm) / bytes_per_ms) if bytes_per_ms > 0 else 0


@dataclass
class PcmAudio:
    pcm: bytes
    sample_rate: int = TARGET_RATE
    channels: int = TARGET_CHANNELS
    bit_depth: int = TARGET_BIT_DEPTH

    @property
    def duration_ms(self) -> int:
        return pcm_duration_ms(self.pcm, self.sample_rate, self.channels, self.bit_depth)


def decode_to_pcm(data: bytes) -> PcmAudio:
    """Decode any container/codec to 16 kHz mono s16le PCM. Raises AudioDecodeError."""
    if not data:
        raise AudioDecodeError("empty audio upload")
    try:
        import av
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise AudioDecodeError(f"PyAV not available: {exc}") from exc

    try:
        container = av.open(io.BytesIO(data))
    except Exception as exc:
        raise AudioDecodeError(f"could not open audio: {exc}") from exc

    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise AudioDecodeError("no audio stream in upload")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
        chunks: list[bytes] = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().tobytes())
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().tobytes())
    except AudioDecodeError:
        raise
    except Exception as exc:
        raise AudioDecodeError(f"decode failed: {exc}") from exc
    finally:
        container.close()

    pcm = b"".join(chunks)
    if not pcm:
        raise AudioDecodeError("decoded to empty PCM")
    return PcmAudio(pcm=pcm)


def frame_size_bytes(
    sample_rate: int = TARGET_RATE,
    channels: int = TARGET_CHANNELS,
    bit_depth: int = TARGET_BIT_DEPTH,
    frame_ms: int = FRAME_MS,
) -> int:
    return int(sample_rate * channels * (bit_depth // 8) * frame_ms / 1000)


def iter_frames(
    pcm: bytes,
    sample_rate: int = TARGET_RATE,
    channels: int = TARGET_CHANNELS,
    bit_depth: int = TARGET_BIT_DEPTH,
    frame_ms: int = FRAME_MS,
) -> Iterator[tuple[int, bytes]]:
    """Yield (offset_ms, frame_bytes) fixed-size PCM frames covering the stream."""
    step = frame_size_bytes(sample_rate, channels, bit_depth, frame_ms)
    if step <= 0:
        raise AudioDecodeError("invalid frame size")
    ms_per_byte = 1000.0 / (sample_rate * channels * (bit_depth // 8))
    for pos in range(0, len(pcm), step):
        yield int(pos * ms_per_byte), pcm[pos : pos + step]
