import os
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
session = os.getenv("SESSION_NAME")

client = TelegramClient(session, api_id, api_hash)

async def main():
    await client.start()

    dialogs = await client.get_dialogs()

    for dialog in dialogs:
        if dialog.is_channel:
            print(f"{dialog.id} | {dialog.name}")

    await client.disconnect()

with client:
    client.loop.run_until_complete(main())
