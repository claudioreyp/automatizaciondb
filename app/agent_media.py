"""Opt-in read-only media derivative for WhatsApp Cloud transport.

The stored file and the POS/public download contracts remain unchanged.
"""
from io import BytesIO
import warnings

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

WHATSAPP_IMAGE_MAX_BYTES = 5 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DECODED_PIXELS = 25_000_000


def whatsapp_cloud_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    def reject(reason):
        raise HTTPException(422, {"code": "WHATSAPP_IMAGE_NOT_COMPATIBLE", "reason": reason,
            "message": "La imagen no puede enviarse por WhatsApp sin modificar su calidad. El archivo original se conserva."})
    if not data or len(data) > MAX_SOURCE_BYTES:
        reject("source_size_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                expected = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(image.format)
                declared = media_type.split(";", 1)[0].strip().lower()
                # Local Windows MIME registries can leave a valid .webp upload
                # unspecified. Only the opt-in derivative may identify it from
                # verified image bytes; an explicit conflicting MIME still fails.
                if expected is None or declared not in {expected, "application/octet-stream"}:
                    reject("invalid_format")
                if image.width * image.height > MAX_DECODED_PIXELS or image.width <= 0 or image.height <= 0:
                    reject("decoded_pixel_limit")
                if getattr(image, "n_frames", 1) != 1:
                    reject("animated_image_not_supported")
                image.verify()
            if expected in {"image/jpeg", "image/png"} and len(data) <= WHATSAPP_IMAGE_MAX_BYTES:
                return data, expected
            with Image.open(BytesIO(data)) as image:
                image.load()
                if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}:
                    reject("lossless_png_mode_not_supported")
                original_size = image.size
                output = BytesIO()
                metadata = {key: image.info[key] for key in ("icc_profile", "exif", "transparency") if key in image.info}
                image.save(output, format="PNG", optimize=True, compress_level=9, **metadata)
                result = output.getvalue()
                if len(result) > WHATSAPP_IMAGE_MAX_BYTES:
                    reject("lossless_derivative_exceeds_transport_limit")
                # The encoder must retain geometry; no resize, crop or JPEG re-encode.
                with Image.open(BytesIO(result)) as derived:
                    if derived.size != original_size:
                        reject("lossless_geometry_mismatch")
                return result, "image/png"
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        reject("invalid_or_unsafe_image")
