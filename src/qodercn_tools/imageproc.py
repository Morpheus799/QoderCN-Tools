"""Strip the gateway's AIGC tracking metadata from generated PNGs.

Port of the Go proxy's stripPNGMetadata: drops tEXt/zTXt/iTXt/eXIf/tIME chunks
(the gateway embeds an "AIGC" tEXt block with the provider's social-credit code
and per-image tracking IDs). Lossless — pixels are untouched. Never raises: on a
non-PNG or a parse failure the original bytes/URL are returned unchanged.
"""

from __future__ import annotations

import base64
import io

from PIL import Image

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_STRIP_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}


def strip_png_metadata(data: bytes) -> bytes:
    if not data.startswith(_PNG_SIGNATURE):
        return data
    out = bytearray(_PNG_SIGNATURE)
    pos = len(_PNG_SIGNATURE)
    saw_iend = False
    try:
        while pos + 8 <= len(data):
            length = int.from_bytes(data[pos : pos + 4], "big")
            ctype = data[pos + 4 : pos + 8]
            chunk_end = pos + 12 + length  # length + type + data + CRC
            if chunk_end > len(data):
                return data  # truncated / not parseable
            if ctype not in _STRIP_CHUNKS:
                out += data[pos:chunk_end]
            if ctype == b"IEND":
                saw_iend = True
                break
            pos = chunk_end
    except Exception:
        return data
    if not saw_iend:
        return data  # never saw IEND → treat as unparseable, return original
    return bytes(out)


def sanitize_data_url(url: str) -> str:
    """If url is a base64 PNG data URL, strip metadata; otherwise return unchanged."""
    prefix = "data:image/png;base64,"
    if not url.startswith(prefix):
        return url
    try:
        raw = base64.b64decode(url[len(prefix):])
    except Exception:
        return url
    cleaned = strip_png_metadata(raw)
    if cleaned is raw or cleaned == raw:
        return url
    return prefix + base64.b64encode(cleaned).decode("ascii")


# --- blind-watermark payload disruption --------------------------------------
# Port of the Go proxy's desyncRecompress: a single lossy re-encode leaves a robust
# blind watermark intact, but a NON-INVERTIBLE geometric desync (crop a margin, then
# resize back to the original size) destroys the payload because the decoder reads off
# a shifted grid; a lossy JPEG pass adds damage. Must not be undone. Corrupts the
# payload, not detectability. PNG in, PNG out.

_DEWM_CROP_PX = 32
_DEWM_JPEG_QUALITY = 30


def _dewm_crop_for(w: int, h: int) -> int:
    """Fixed 32px crop for normal images, proportional for small ones."""
    crop = _DEWM_CROP_PX
    m = min(w, h)
    if 2 * crop + 16 >= m:
        crop = m // 8
        if crop < 1 and m >= 3:
            crop = 1
    return crop


def dewatermark_png_bytes(data: bytes) -> bytes:
    if not data.startswith(_PNG_SIGNATURE):
        return data
    try:
        src = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = src.size
        crop = _dewm_crop_for(w, h)
        if crop <= 0 or w - 2 * crop < 1 or h - 2 * crop < 1:
            return data
        # 1) crop a margin, 2) lossy JPEG at the cropped resolution (destructive step)
        cropped = src.crop((crop, crop, w - crop, h - crop))
        jb = io.BytesIO()
        cropped.save(jb, format="JPEG", quality=_DEWM_JPEG_QUALITY)
        jb.seek(0)
        small = Image.open(jb).convert("RGB")
        # 3) bilinear-resize back up to the original size (never undo the crop)
        restored = small.resize((w, h), Image.BILINEAR)
        out = io.BytesIO()
        restored.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return data


def dewatermark_data_url(url: str) -> str:
    prefix = "data:image/png;base64,"
    if not url.startswith(prefix):
        return url
    try:
        raw = base64.b64decode(url[len(prefix):])
    except Exception:
        return url
    out = dewatermark_png_bytes(raw)
    if out == raw:
        return url
    return prefix + base64.b64encode(out).decode("ascii")
