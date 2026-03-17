import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient


load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_NAME = os.getenv("SESSION_NAME", "adder_session")


async def main() -> None:
    if not API_ID or not API_HASH:
        raise RuntimeError("Set API_ID and API_HASH in your environment first")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    print(f"Session is authorized for user: {me.username or me.id}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
