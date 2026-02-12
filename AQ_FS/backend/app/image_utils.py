"""Screenshot optimization — compress before sending to Claude API."""
from PIL import Image
import io
import base64


def optimize_screenshot(screenshot_bytes: bytes, max_width: int = 1280, quality: int = 75) -> bytes:
    """
    Resize and compress a screenshot for API consumption.
    1920x1080 PNG (~2-4MB) → 1280x720 JPEG (~150-300KB)
    Claude sees it just as well. Saves tokens and latency.
    """
    img = Image.open(io.BytesIO(screenshot_bytes))

    # Resize if wider than max_width
    w, h = img.size
    if w > max_width:
        ratio = max_width / w
        img = img.resize((max_width, int(h * ratio)), Image.LANCZOS)

    # Convert RGBA to RGB (JPEG doesn't support alpha)
    if img.mode == 'RGBA':
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    # Save as JPEG
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    return buf.getvalue()


def screenshot_to_b64(screenshot_bytes: bytes, compress: bool = True,
                      max_width: int = 1280, quality: int = 75) -> tuple[str, str]:
    """
    Convert screenshot bytes to base64 string.
    Returns (base64_string, media_type).
    """
    if compress:
        optimized = optimize_screenshot(screenshot_bytes, max_width=max_width, quality=quality)
        return base64.b64encode(optimized).decode(), "image/jpeg"
    else:
        return base64.b64encode(screenshot_bytes).decode(), "image/png"
