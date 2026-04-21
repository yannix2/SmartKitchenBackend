import logging

import requests
from requests.exceptions import ReadTimeout, ConnectionError

from app.core.config import settings

logger = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
        "apikey": settings.SUPABASE_SERVICE_KEY,
    }
def upload_file(bucket: str, path: str, data: bytes, content_type: str) -> str:
    """Upload bytes to Supabase Storage. Creates or replaces the object. Returns the public URL."""
    url = f"{settings.SUPABASE_URL}/storage/v1/object/{bucket}/{path}"
    headers = {**_headers(), "Content-Type": content_type, "x-upsert": "true"}
    resp = requests.post(url, data=data, headers=headers)
    resp.raise_for_status()
    return f"{settings.SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"


def delete_file(bucket: str, path: str) -> None:
    url = f"{settings.SUPABASE_URL}/storage/v1/object/{bucket}/{path}"
    requests.delete(url, headers=_headers())


def find_and_download_proof(order_id: str) -> tuple[bytes, str, str] | None:
    """
    Look for order-proofs/{order_id}.jpg/jpeg/png/webp in the public bucket.
    Returns (file_bytes, filename, content_type) or None if not found.
    """
    ext_to_mime = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }
    for ext, mime in ext_to_mime.items():
        filename = f"{order_id}.{ext}"
        url = f"{settings.SUPABASE_URL}/storage/v1/object/public/order-proofs/{filename}"
        try:
            resp = requests.get(url, timeout=15)
        except (ReadTimeout, ConnectionError) as exc:
            logger.warning("Supabase timeout fetching %s: %s — skipping", filename, exc)
            continue
        if resp.status_code == 200:
            return resp.content, filename, mime
    return None
