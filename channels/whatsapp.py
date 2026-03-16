"""WhatsApp channel integration (Meta Cloud API)."""

import httpx

from config import settings

WHATSAPP_API_URL = "https://graph.facebook.com/v20.0/{phone_id}/messages"


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
        return response.status_code == 200


async def send_document(recipient: str, file_url: str, filename: str) -> bool:
    """Send a document file via WhatsApp Cloud API.

    Args:
        recipient: WhatsApp phone number.
        file_url: Publicly accessible URL of the file.
        filename: Display name for the file.

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
        "type": "document",
        "document": {"link": file_url, "filename": filename},
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=10.0)
        return response.status_code == 200
