import base64
import json
import mimetypes
from pathlib import Path
from uuid import uuid4

import httpx

from .config import get_settings


async def store_private_file(
    data: bytes,
    filename: str,
    content_type: str,
    category: str = "payment-evidence",
) -> str:
    settings = get_settings()
    safe_name = f"{uuid4().hex}-{Path(filename).name}"
    if settings.supabase_url and settings.supabase_service_role_key:
        path = f"{category.strip('/')}/{safe_name}"
        url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/impulsa-private/{path}"
        headers = {
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "apikey": settings.supabase_service_role_key,
            "Content-Type": content_type,
            "x-upsert": "false",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, content=data, headers=headers)
            response.raise_for_status()
        return f"supabase://impulsa-private/{path}"

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    target = settings.upload_dir / safe_name
    target.write_bytes(data)
    return str(target)


async def load_private_file(storage_path: str) -> tuple[bytes, str]:
    settings = get_settings()
    if storage_path.startswith("supabase://impulsa-private/"):
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise FileNotFoundError("Private storage is not configured")
        path = storage_path.removeprefix("supabase://impulsa-private/")
        url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/impulsa-private/{path}"
        headers = {
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "apikey": settings.supabase_service_role_key,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        return response.content, response.headers.get("content-type", "application/octet-stream")

    target = Path(storage_path).resolve()
    upload_root = settings.upload_dir.resolve()
    if upload_root not in target.parents or not target.is_file():
        raise FileNotFoundError("Private file not found")
    return target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream"


async def analyze_payment_image(data: bytes, content_type: str) -> dict:
    settings = get_settings()
    if not settings.openai_api_key:
        return {"available": False, "reason": "vision_not_configured"}

    media_type = content_type or mimetypes.guess_type("evidence.png")[0] or "image/png"
    encoded = base64.b64encode(data).decode("ascii")
    prompt = (
        "Analiza esta captura de pago peruano Yape o Plin. Devuelve solamente JSON con: "
        "provider (yape|plin|unknown), amount, operation_number, security_code (exactamente tres digitos), "
        "occurred_at ISO-8601, recipient, confidence entre 0 y 1 y warnings como arreglo. "
        "No inventes valores ilegibles; usa null cuando no se distingan."
    )
    payload = {
        "model": settings.payment_vision_model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:{media_type};base64,{encoded}"},
                ],
            }
        ],
        "text": {"format": {"type": "json_object"}},
    }
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post("https://api.openai.com/v1/responses", json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
    text = body.get("output_text")
    if not text:
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text")
                    break
    if not text:
        return {"available": True, "error": "empty_model_output"}
    try:
        return {"available": True, **json.loads(text)}
    except json.JSONDecodeError:
        return {"available": True, "error": "invalid_model_json", "raw": text[:500]}
