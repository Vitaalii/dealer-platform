import asyncio
import os
import aiohttp
from dotenv import load_dotenv

load_dotenv()

NOCODB_TOKEN = os.getenv("NOCODB_TOKEN")
NOCODB_API_URL = os.getenv("NOCODB_API_URL")
NOCODB_TABLE_ID_CARS = os.getenv("NOCODB_TABLE_ID")

async def check_keys():
    headers = {"xc-token": NOCODB_TOKEN}
    url = f"{NOCODB_API_URL}/api/v2/tables/{NOCODB_TABLE_ID_CARS}/records"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params={"limit": 1}, ssl=False) as resp:
            if resp.status == 200:
                data = await resp.json()
                rows = data.get("list", [])
                if rows:
                    print("KEYS FOUND:", list(rows[0].keys()))
                    print("SAMPLE ROW:", rows[0])
                else:
                    print("NO DATA")
            else:
                print(f"Error: {resp.status}")

if __name__ == "__main__":
    asyncio.run(check_keys())
