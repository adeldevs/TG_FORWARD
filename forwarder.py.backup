import os
import json
import asyncio
import logging

from decouple import config as getenv

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError


# ---- CONFIG ----
api_id = int(getenv("API_ID"))
api_hash = getenv("API_HASH")

source_channel = getenv("SOURCE_CHANNEL")
target_channel = getenv("TARGET_CHANNEL")

session_name = getenv("SESSION_NAME")

STATE_FILE = getenv("STATE_FILE")   # file to store last processed message ID

LOG_FILE = getenv("LOG_FILE")  # log file

# ---------------- LOGGING ----------------
logging.basicConfig(
    filename=LOG_FILE,
    filemode='a',  # append mode
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- SESSION ----------------
client = TelegramClient(session_name, api_id, api_hash)

# ---------------- STATE HELPERS ----------------
def save_last_id(msg_id):
    """Save last forwarded message ID with source & target channels."""
    state = {
        "last_id": msg_id,
        "source": source_channel,
        "target": target_channel
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    logger.info(f"Saved last message ID {msg_id} | Source: {source_channel} | Target: {target_channel}")


def load_last_id():
    """Load last forwarded message ID."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            last_id = state.get("last_id", 0)
            src = state.get("source", source_channel)
            tgt = state.get("target", target_channel)
            logger.info(f"Loaded last message ID {last_id} | Source: {src} | Target: {tgt}")
            return last_id
    return 0


# ---------------- FORWARD OLD MESSAGES ----------------
async def copy_old_messages():
    last_id = load_last_id()
    print(f"Resuming from last message ID: {last_id}")
    logger.info(f"Resuming from last message ID: {last_id}")

    async for message in client.iter_messages(source_channel, reverse=True, min_id=last_id):
        try:
            if message.text:
                await client.send_message(target_channel, message.text)
                logger.info(f"Forwarded text message ID {message.id}")
            elif message.media:
                await client.send_file(target_channel, message.media, caption=message.text or "")
                logger.info(f"Forwarded media message ID {message.id}")

            save_last_id(message.id)
            await asyncio.sleep(2)  # prevent flood

        except FloodWaitError as e:
            logger.warning(f"Flood wait triggered ({e.seconds} sec). Sleeping...")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Error forwarding message {message.id}: {e}")
            await asyncio.sleep(10)


# ---------------- FORWARD NEW MESSAGES ----------------
@client.on(events.NewMessage(chats=source_channel))
async def handler(event):
    try:
        if event.message.text:
            await client.send_message(target_channel, event.message.text)
            logger.info(f"Forwarded new text message ID {event.message.id}")
        elif event.message.media:
            await client.send_file(target_channel, event.message.media, caption=event.message.text or "")
            logger.info(f"Forwarded new media message ID {event.message.id}")

        save_last_id(event.message.id)

    except FloodWaitError as e:
        logger.warning(f"Flood wait while forwarding new message ({e.seconds} sec). Sleeping...")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error(f"Error forwarding new message {event.message.id}: {e}")


# ---------------- MAIN ----------------
async def main():
    print("Fetching old messages and forwarding...")
    logger.info("Starting old message forwarding...")
    await copy_old_messages()
    print("✅ Done copying history. Now listening for new messages...")
    logger.info("Done copying history. Now listening for new messages...")


with client:
    client.loop.run_until_complete(main())
    client.run_until_disconnected()