"""Quick integration tests for WhatsApp send and receive flows."""

import asyncio
import sys
import time

sys.path.insert(0, "/root/autoreporte")

import httpx

from channels.whatsapp import send_document, send_text

RECIPIENT = "34635459494"
DOCX_PATH = "/root/autoreporte/outputs/Reporte Empresa Test SL.docx"
DOCX_NAME = "Reporte Empresa Test SL.docx"
API_BASE = "http://localhost:8001/api/v1"


async def test_send_text() -> None:
    print("=== Test 1: send_text ===")
    ok = await send_text(RECIPIENT, "Test desde sistema autoreporte ✅")
    print(f"send_text: {'OK' if ok else 'FAILED'}\n")


async def test_send_document() -> None:
    print("=== Test 2: send_document ===")
    ok = await send_document(RECIPIENT, DOCX_PATH, DOCX_NAME)
    print(f"send_document: {'OK' if ok else 'FAILED'}\n")


async def test_receive_flow() -> None:
    """Simulate Meta sending a document webhook to our endpoint."""
    print("=== Test 3: receive webhook (document) ===")

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1365581438947154",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1032288006634946"},
                            "messages": [
                                {
                                    "from": "34635459494",
                                    "id": f"wamid.test{int(time.time())}",
                                    "timestamp": str(int(time.time())),
                                    "type": "document",
                                    "document": {
                                        "id": "FAKE_MEDIA_ID",
                                        "filename": "empresa_test.pdf",
                                        "mime_type": "application/pdf",
                                    },
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_BASE}/webhook/whatsapp",
            json=payload,
            timeout=10.0,
        )
        print(f"Webhook response: {response.status_code} — {response.text}")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print("receive_flow: OK\n")


async def main() -> None:
    await test_send_text()
    await test_send_document()
    await test_receive_flow()


asyncio.run(main())
