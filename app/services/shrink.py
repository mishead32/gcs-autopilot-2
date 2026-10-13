"""Make a proof photo smaller before it is ever stored.

Why this exists, plainly: a phone photo is three to five megabytes, and the
only thing anybody does with it is glance at it to check the work was done.
Nobody zooms into the grain. Stored at full size it costs money three times
over — to keep, to send to whoever opens the task, and to send again every
time somebody else opens it.

So an image is capped at a sensible viewing size and re-encoded. A 4 MB photo
comes out around 300 KB and looks identical on a screen. Text in a screenshot
stays readable, which is the one thing that must not be sacrificed — a bill
number nobody can read is not proof of anything.

Two rules hold throughout:

  * Never make a file bigger. If the re-encode comes out larger than what
    arrived — already-small images, line art — the original is kept.
  * Never lose a file. Anything that is not an image we can safely open, or
    that fails to decode for any reason, is stored exactly as it arrived.
    A PDF, a spreadsheet and an iPhone HEIC all pass straight through.

The browser does this too, before uploading, which is where the real saving
is: the photo never crosses the network at full size. This module is the
safety net for everything that arrives uncompressed anyway — the plain form
post with JavaScript off, an older phone browser, a direct API call.
"""
from __future__ import annotations

import io
import os

# The longest edge of a stored image. 1600 is comfortably more than any
# screen in the office shows a proof photo at, and keeps printed text in a
# screenshot legible.
MAX_EDGE = int(os.getenv("IMAGE_MAX_EDGE", "1600"))

# JPEG quality. 82 is the point where a photo stops looking re-encoded; going
# lower saves little and starts to show on flat areas like a white bill.
JPEG_QUALITY = int(os.getenv("IMAGE_QUALITY", "82"))

# A PNG above this, after resizing, is a photograph or a dense screenshot
# that a JPEG stores far better. Below it, PNG is left alone — small PNGs are
# usually logos or line art, which JPEG makes both bigger and blurrier.
PNG_TO_JPEG_OVER = 600 * 1024

SHRINKABLE = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


def _as_image(data: bytes):
    """Decode, or None if this is not an image we can safely work with."""
    try:
        from PIL import Image, ImageOps
    except ImportError:          # Pillow absent: everything passes through
        return None, None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None, None
    # A phone writes the photo sideways and records "rotate this on display".
    # Re-encoding drops that note, so the rotation is applied for real first,
    # or every portrait photo comes out on its side.
    try:
        from PIL import ImageOps as _Ops
        img = _Ops.exif_transpose(img) or img
    except Exception:
        pass
    return img, Image


def shrink(data: bytes, content_type: str, filename: str = "") -> tuple[bytes, str, str]:
    """Return (data, content_type, filename) — smaller when that is possible.

    The filename comes back too because a PNG that became a JPEG should not
    still be called .png: the browser copes, but the person who downloads it
    gets a file their computer opens with the wrong program.
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in SHRINKABLE or not data:
        return data, content_type, filename

    img, Image = _as_image(data)
    if img is None:
        return data, content_type, filename

    # Resize only downwards. Enlarging a small photo to the cap would be both
    # pointless and bigger.
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    want_jpeg = ctype in ("image/jpeg", "image/jpg")
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info)

    out = _encode(img, Image, jpeg=want_jpeg)

    # A big PNG with no transparency is a photo in the wrong clothes.
    if not want_jpeg and not has_alpha and len(out) > PNG_TO_JPEG_OVER:
        asjpeg = _encode(img, Image, jpeg=True)
        if len(asjpeg) < len(out):
            out, want_jpeg = asjpeg, True

    # The promise: never hand back something larger than what arrived.
    if len(out) >= len(data):
        return data, content_type, filename

    if want_jpeg and ctype not in ("image/jpeg", "image/jpg"):
        return out, "image/jpeg", _retitle(filename, ".jpg")
    return out, content_type, filename


def _encode(img, Image, jpeg: bool) -> bytes:
    buf = io.BytesIO()
    if jpeg:
        # JPEG has no alpha channel; pasting onto white keeps a transparent
        # screenshot from coming out with a black background.
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            flat = Image.new("RGB", img.size, (255, 255, 255))
            flat.paste(img, mask=img.split()[-1])
            img = flat
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # optimize + progressive costs a little time at upload and saves on
        # every single view afterwards.
        img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    else:
        img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _retitle(filename: str, ext: str) -> str:
    if not filename:
        return filename
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return (stem or "image") + ext


def saved_label(before: int, after: int) -> str:
    """'3.8 MB -> 0.3 MB (92% smaller)', for the log line."""
    if after >= before or not before:
        return f"{before / 1048576:.1f} MB (kept as uploaded)"
    pct = round((1 - after / before) * 100)
    return f"{before / 1048576:.1f} MB -> {after / 1048576:.1f} MB ({pct}% smaller)"
