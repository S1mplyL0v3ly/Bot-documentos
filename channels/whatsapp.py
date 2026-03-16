"""WhatsApp channel integration (Meta Cloud API)."""

from pathlib import Path

import httpx

from config import settings

WHATSAPP_API_URL = "https://graph.facebook.com/v20.0/{phone_id}/messages"
WHATSAPP_MEDIA_URL = "https://graph.facebook.com/v20.0/{phone_id}/media"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def send_text(recipient: str, text: str) -> bool:
    """Send a plain text message via WhatsApp Cloud API.

    Args:
        recipient: WhatsApp phone number (international format, no +).
        text: Message body.

    Returns:
        True if sent successfully.
    """
    if not settings.whatsapp_token or not settings.whatsapp_phone_id:
        print("[WhatsApp] Token or phone_id not configured — skipping send.")
        return False

    url = WHATSAPP_API_URL.format(phone_id=settings.whatsapp_phone_id)
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"body": text},
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=10.0)
        if response.status_code != 200:
            print(
                f"[WhatsApp] send_text failed: {response.status_code} — {response.text}"
            )
        else:
            print(f"[WhatsApp] send_text OK: {response.json()}")
        return response.status_code == 200


async def upload_document(file_path: str) -> str | None:
    """Upload a local file to WhatsApp Media API.

    Args:
        file_path: Absolute path to the DOCX file.

    Returns:
        media_id string on success, None on failure.
    """
    if not settings.whatsapp_token or not settings.whatsapp_phone_id:
        print("[WhatsApp] Token or phone_id not configured — skipping upload.")
        return None

    upload_url = WHATSAPP_MEDIA_URL.format(phone_id=settings.whatsapp_phone_id)
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}
    path = Path(file_path)

    async with httpx.AsyncClient() as client:
        with open(path, "rb") as f:
            files = {
                "file": (path.name, f, _DOCX_MIME),
                "messaging_product": (None, "whatsapp"),
                "type": (None, _DOCX_MIME),
            }
            response = await client.post(
                upload_url, headers=headers, files=files, timeout=30.0
            )
            if response.status_code == 200:
                media_id = response.json().get("id")
                print(f"[WhatsApp] Upload OK: media_id={media_id}")
                return media_id
            print(f"[WhatsApp] Upload failed: {response.status_code} — {response.text}")
            return None


async def send_document(recipient: str, file_path: str, filename: str) -> bool:
    """Upload a local DOCX to WhatsApp Media API and send it as a document.

    Args:
        recipient: WhatsApp phone number (international format, no +).
        file_path: Absolute path to the local DOCX file.
        filename: Display name shown to the recipient.

    Returns:
        True if uploaded and sent successfully.
    """
    media_id = await upload_document(file_path)
    if not media_id:
        print("[WhatsApp] Could not upload document — skipping send.")
        return False

    url = WHATSAPP_API_URL.format(phone_id=settings.whatsapp_phone_id)
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "document",
        "document": {"id": media_id, "filename": filename},
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=10.0)
        if response.status_code != 200:
            print(
                f"[WhatsApp] send_document failed: {response.status_code} — {response.text}"
            )
        else:
            print(f"[WhatsApp] send_document OK: {response.json()}")
        return response.status_code == 200
