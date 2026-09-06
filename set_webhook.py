import asyncio
import httpx

TOKEN = "8981520434:AAFKY2BouPoF0pg4rXuDmyyE0eXlodrW1yQ"
WEBHOOK_URL = "https://procureai-api-iftl.onrender.com/internal/webhook/telegram"

async def set_webhook():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            json={"url": WEBHOOK_URL}
        )
        print(response.json())

asyncio.run(set_webhook())
