import asyncio
import httpx

MSG91_KEY = "568090A9cERa9aQ8qr6a9c6896P1"
PHONE = "+919901232826"

async def test():
    async with httpx.AsyncClient() as client:
        payload = {
            "sender": "PRCRAI",
            "route": "4",
            "country": "91",
            "sms": [{
                "message": "ProcureAI: Namaskara! Your ITC alert system is working. This is a test message.",
                "to": [PHONE]
            }]
        }
        response = await client.post(
            "https://api.msg91.com/api/v5/sendhttp.php",
            json=payload,
            headers={"authkey": MSG91_KEY}
        )
        print("Status:", response.status_code)
        print("Response:", response.text)

asyncio.run(test())
