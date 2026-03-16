"""Quick test for WhatsApp send_text and send_document."""

import asyncio
import sys

sys.path.insert(0, "/root/autoreporte")

from channels.whatsapp import send_document, send_text

RECIPIENT = "34635459494"
DOCX_PATH = "/root/autoreporte/outputs/Reporte Empresa Test SL.docx"
DOCX_NAME = "Reporte Empresa Test SL.docx"


async def main() -> None:
    print("=== Test 1: send_text ===")
    ok = await send_text(RECIPIENT, "Test desde sistema autoreporte ✅")
    print(f"send_text: {'OK' if ok else 'FAILED'}\n")

    print("=== Test 2: send_document ===")
    ok = await send_document(RECIPIENT, DOCX_PATH, DOCX_NAME)
    print(f"send_document: {'OK' if ok else 'FAILED'}\n")


asyncio.run(main())
