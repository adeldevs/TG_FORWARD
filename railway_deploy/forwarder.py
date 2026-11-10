from pathlib import Path
import os
import json
import asyncio
import logging

from decouple import config as getenv
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError


# ============================================================
# CONFIG
# ============================================================

api_id = int(getenv("API_ID"))
api_hash = getenv("API_HASH")

def parse_id_list(raw_val):
    if not raw_val:
        return []
    res = []
    for part in str(raw_val).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            res.append(int(part))
        except ValueError:
            pass
    return res

raw_sources = getenv("SOURCE_CHANNELS", default=getenv("SOURCE_CHANNEL", default=""))
source_channel_ids = parse_id_list(raw_sources)

raw_targets = getenv("TARGET_CHANNELS", default=getenv("TARGET_CHANNEL", default=""))
target_channel_ids = parse_id_list(raw_targets)

source_channels = []
target_channels = []

session_name = getenv("SESSION_NAME", default="forwarder_session")

STATE_FILE = getenv("STATE_FILE", default="state.json")
LOG_FILE = getenv("LOG_FILE", default="forwarder.log")


# ============================================================
# COOLDOWN SETTINGS
# ============================================================

MESSAGE_DELAY = 5
BATCH_SIZE = 20
BATCH_COOLDOWN = 60


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    filename=LOG_FILE,
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# ============================================================
# MEDIA TYPE FILTER
# ============================================================

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv",
    ".m4v", ".mpeg", ".mpg", ".ts", ".m2ts", ".3gp", ".ogv",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".wma",
    ".opus", ".amr",
}

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".log", ".py", ".html",
    ".css", ".js", ".xml", ".rtf", ".doc", ".docx", ".pdf",
}


def allowed_media_types():
    """Return the set of media types allowed to forward, or None for all."""
    raw = getenv("ALLOW_MEDIA_TYPES", default="all").strip().lower()
    if not raw or raw == "all":
        return None
    return {t.strip() for t in raw.split(",") if t.strip()}


def classify_media(message):
    """Classify a message: photo, video, audio, text, document, or other."""
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.audio or message.voice:
        return "audio"
    if message.document:
        filename = message.file.name if message.file else None
        if filename:
            ext = Path(filename).suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                return "video"
            if ext in AUDIO_EXTENSIONS:
                return "audio"
            if ext in TEXT_EXTENSIONS:
                return "text"
        return "document"
    if message.text:
        return "text"
    return "other"


# ============================================================
# TELEGRAM SESSION
# ============================================================

session_string = getenv("SESSION_STRING", default=None)

if session_string:
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash
    )
else:
    client = TelegramClient(
        session_name,
        api_id,
        api_hash
    )


# ============================================================
# RESOLVE CHANNELS
# ============================================================

async def resolve_channels():
    global source_channels, target_channels

    print("Resolving Telegram channels...")
    if not client.is_connected():
        await client.connect()

    source_channels = []
    for sid in source_channel_ids:
        try:
            ent = await client.get_entity(sid)
            source_channels.append(ent)
            name = getattr(ent, 'title', None) or getattr(ent, 'username', str(sid))
            print(f"  Source : {name} ({sid})")
        except Exception as e:
            logger.warning(f"Could not resolve source channel {sid}: {e}")
            print(f"  [!] Failed to resolve source {sid}: {e}")

    target_channels = []
    for tid in target_channel_ids:
        try:
            ent = await client.get_entity(tid)
            target_channels.append(ent)
            name = getattr(ent, 'title', None) or getattr(ent, 'username', str(tid))
            print(f"  Target : {name} ({tid})")
        except Exception as e:
            logger.warning(f"Could not resolve target channel {tid}: {e}")
            print(f"  [!] Failed to resolve target {tid}: {e}")

    if not source_channels:
        raise ValueError("No source channels could be resolved. Check your configuration.")
    if not target_channels:
        raise ValueError("No target channels could be resolved. Check your configuration.")

    logger.info(f"Resolved {len(source_channels)} source(s) and {len(target_channels)} target(s)")


# ============================================================
# STATE
# ============================================================

def save_last_id(chat_id, msg_id):
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except Exception:
            state = {}

    if "checkpoints" not in state:
        state["checkpoints"] = {}

    state["checkpoints"][str(chat_id)] = msg_id
    state["last_id"] = msg_id
    state["sources"] = source_channel_ids
    state["targets"] = target_channel_ids

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    logger.info(f"Saved last message ID {msg_id} for channel {chat_id}")


def load_last_id(chat_id):
    SKIP_BEFORE_ID = 963090

    if not os.path.exists(STATE_FILE):
        return SKIP_BEFORE_ID

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except Exception:
        return SKIP_BEFORE_ID

    checkpoints = state.get("checkpoints", {})
    last_id = checkpoints.get(str(chat_id))
    if last_id is None:
        last_id = state.get("last_id", 0)

    if last_id < SKIP_BEFORE_ID:
        last_id = SKIP_BEFORE_ID

    logger.info(f"Resuming channel {chat_id} from message ID {last_id}")
    return last_id


# ============================================================
# FORWARD OLD MESSAGES
# ============================================================

async def copy_old_messages_for_channel(source_ent):
    peer_id = utils.get_peer_id(source_ent)
    last_id = load_last_id(peer_id)
    title = getattr(source_ent, 'title', None) or str(peer_id)

    print()
    print("======================================")
    print(f" HISTORICAL MIGRATION: {title}")
    print("======================================")
    print(f"Starting from message ID: {last_id}")
    print(f"Delay between uploads  : {MESSAGE_DELAY}s")
    print(f"Batch size             : {BATCH_SIZE}")
    print(f"Batch cooldown         : {BATCH_COOLDOWN}s")
    print("======================================")
    print()

    latest_messages = await client.get_messages(source_ent, limit=1)
    latest_id = latest_messages[0].id if latest_messages else last_id

    print(f"Latest source message : {latest_id}")
    print(f"Messages remaining    : ~{max(0, latest_id - last_id)}")
    print()

    uploaded = 0
    skipped = 0
    scanned = 0

    async for message in client.iter_messages(
        source_ent,
        reverse=True,
        min_id=last_id
    ):
        if not client.is_connected():
            await client.connect()

        scanned += 1

        try:
            media_type = classify_media(message)
            allowed = allowed_media_types()

            if allowed is not None:
                if not media_type or media_type not in allowed:
                    skipped += 1
                    save_last_id(peer_id, message.id)
                    continue

            filename = message.file.name if message.file else None
            display_name = filename or media_type

            print(f"[FORWARD] {message.id} | {media_type} | {display_name}")

            # Send to all target channels
            for tgt in target_channels:
                if message.media:
                    await client.send_file(
                        tgt,
                        message.media,
                        caption=message.text or ""
                    )
                elif message.text:
                    await client.send_message(tgt, message.text)

            save_last_id(peer_id, message.id)
            uploaded += 1

            await asyncio.sleep(MESSAGE_DELAY)

            if uploaded % BATCH_SIZE == 0:
                print(f"--- {uploaded} messages forwarded. Cooldown for {BATCH_COOLDOWN}s... ---")
                await asyncio.sleep(BATCH_COOLDOWN)

        except FloodWaitError as e:
            print(f"FloodWait: sleeping {e.seconds}s...")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"ERROR on message {message.id}: {e}")
            logger.error(f"Error on message {message.id}: {e}")
            await asyncio.sleep(15)


async def copy_old_messages():
    for src in source_channels:
        await copy_old_messages_for_channel(src)


# ============================================================
# FORWARD NEW MESSAGES (LIVE EVENT LISTENER)
# ============================================================

@client.on(events.NewMessage)
async def handler(event):
    if event.chat_id not in source_channel_ids:
        return

    message = event.message
    if not client.is_connected():
        await client.connect()

    try:
        media_type = classify_media(message)
        allowed = allowed_media_types()

        if allowed is not None:
            if not media_type or media_type not in allowed:
                save_last_id(event.chat_id, message.id)
                return

        filename = message.file.name if message.file else None
        print(f"[LIVE] New message {message.id} in {event.chat_id} ({filename or media_type})")

        for tgt in target_channels:
            if message.media:
                await client.send_file(
                    tgt,
                    message.media,
                    caption=message.text or ""
                )
            elif message.text:
                await client.send_message(tgt, message.text)

        save_last_id(event.chat_id, message.id)
        logger.info(f"Forwarded live message {message.id} from {event.chat_id}")

    except FloodWaitError as e:
        print(f"Flood wait {e.seconds}s for live message {message.id}")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error(f"Error forwarding new message {message.id}: {e}")


# ============================================================
# MAIN
# ============================================================

async def main():
    await resolve_channels()
    print("Copying historical messages if any...")
    await copy_old_messages()
    print("Now listening for new messages in real-time...")


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
        client.run_until_disconnected()
