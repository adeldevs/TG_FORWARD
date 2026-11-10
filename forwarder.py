from pathlib import Path
import os
import json
import asyncio
import logging

from decouple import config as getenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError


# ============================================================
# CONFIG
# ============================================================

api_id = int(getenv("API_ID"))
api_hash = getenv("API_HASH")

source_channel_id = int(getenv("SOURCE_CHANNEL"))
target_channel_id = int(getenv("TARGET_CHANNEL"))

source_channel = None
target_channel = None

session_name = getenv("SESSION_NAME")

STATE_FILE = getenv("STATE_FILE")
LOG_FILE = getenv("LOG_FILE")


# ============================================================
# COOLDOWN SETTINGS
# ============================================================

# Wait between individual messages
MESSAGE_DELAY = 5

# After every N messages, take a longer break
BATCH_SIZE = 20

# Long break after each batch
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

# File extensions used to classify documents sent as files.
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
    raw = getenv("ALLOW_MEDIA_TYPES", default="all").strip()
    if not raw or raw.lower() == "all":
        return None
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def classify_media(message):
    """Classify a message's media: photo, video, audio, text, document or None."""
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.audio:
        return "audio"
    if message.voice:
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
    return None


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
    global source_channel
    global target_channel

    print("Resolving Telegram channels...")

    dialogs = await client.get_dialogs()

    for dialog in dialogs:

        if dialog.id == source_channel_id:
            source_channel = dialog.entity

        if dialog.id == target_channel_id:
            target_channel = dialog.entity

    if source_channel is None:
        raise ValueError(
            f"Source channel {source_channel_id} "
            "was not found in your Telegram dialogs."
        )

    if target_channel is None:
        raise ValueError(
            f"Target channel {target_channel_id} "
            "was not found in your Telegram dialogs."
        )

    print(f"Source : {source_channel.title}")
    print(f"Target : {target_channel.title}")

    logger.info(
        f"Resolved source={source_channel.title} "
        f"target={target_channel.title}"
    )


# ============================================================
# STATE
# ============================================================

def save_last_id(msg_id):

    state = {
        "last_id": msg_id,
        "source": source_channel_id,
        "target": target_channel_id
    }

    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

    logger.info(
        f"Saved last message ID {msg_id}"
    )


def load_last_id():

    SKIP_BEFORE_ID = 963090

    if not os.path.exists(STATE_FILE):
        return SKIP_BEFORE_ID

    with open(STATE_FILE, "r") as f:
        state = json.load(f)

    last_id = state.get("last_id", 0)

    if last_id < SKIP_BEFORE_ID:
        last_id = SKIP_BEFORE_ID

    logger.info(
        f"Resuming from message ID {last_id}"
    )

    return last_id


# ============================================================
# FORWARD OLD MESSAGES
# ============================================================

async def copy_old_messages():

    last_id = load_last_id()

    print()
    print("======================================")
    print(" HISTORICAL MEDIA MIGRATION")
    print("======================================")
    print(f"Starting from message ID: {last_id}")
    print(f"Delay between uploads  : {MESSAGE_DELAY}s")
    print(f"Batch size             : {BATCH_SIZE}")
    print(f"Batch cooldown         : {BATCH_COOLDOWN}s")
    print("======================================")
    print()

    logger.info(
        f"Starting media migration from message ID {last_id}"
    )

    # Get the latest message ID so we can show progress.
    latest_messages = await client.get_messages(
        source_channel,
        limit=1
    )

    latest_id = latest_messages[0].id if latest_messages else last_id

    print(f"Latest source message : {latest_id}")
    print(f"Messages remaining    : ~{max(0, latest_id - last_id)}")
    print()

    uploaded = 0
    skipped = 0
    scanned = 0

    async for message in client.iter_messages(
        source_channel,
        reverse=True,
        min_id=last_id
    ):

        if not client.is_connected():
            await client.connect()
            await resolve_channels()

        scanned += 1

        try:

            # ------------------------------------------------
            # DETERMINE WHETHER THIS IS MEDIA WE WANT
            # ------------------------------------------------

            media_type = classify_media(message)
            filename = message.file.name if message.file else None

            allowed = allowed_media_types()
            if not media_type or (allowed is not None and media_type not in allowed):

                skipped += 1
                save_last_id(message.id)

                print(
                    f"[SKIP] {message.id} "
                    f"({message.date.strftime('%Y-%m-%d %H:%M')})"
                )

                continue

            # ------------------------------------------------
            # SHOW UPLOAD INFORMATION
            # ------------------------------------------------

            remaining = max(0, latest_id - message.id)

            display_name = filename or media_type

            print()
            print(
                f"[UPLOAD] message {message.id} "
                f"| {media_type} "
                f"| {display_name}"
            )

            print(
                f"         Uploaded : {uploaded}"
            )

            print(
                f"         Skipped  : {skipped}"
            )

            print(
                f"         Scanned  : {scanned}"
            )

            print(
                f"         Remaining: ~{remaining} message IDs"
            )

            # ------------------------------------------------
            # UPLOAD MEDIA
            # ------------------------------------------------

            await client.send_file(
                target_channel,
                message.media,
                caption=message.text or ""
            )

            # ------------------------------------------------
            # SAVE STATE ONLY AFTER SUCCESS
            # ------------------------------------------------

            save_last_id(message.id)

            uploaded += 1

            print(
                f"[DONE] message {message.id} uploaded successfully"
            )

            # ------------------------------------------------
            # NORMAL DELAY
            # ------------------------------------------------

            await asyncio.sleep(MESSAGE_DELAY)

            # ------------------------------------------------
            # BATCH COOLDOWN
            # ------------------------------------------------

            if uploaded % BATCH_SIZE == 0:

                print()
                print(
                    f"--- {uploaded} media uploads completed ---"
                )

                print(
                    f"Cooling down for {BATCH_COOLDOWN} seconds..."
                )

                logger.info(
                    f"Batch cooldown after {uploaded} uploads"
                )

                await asyncio.sleep(BATCH_COOLDOWN)

                print(
                    "Cooldown finished. Continuing..."
                )
                print()

        # ----------------------------------------------------
        # TELEGRAM FLOOD WAIT
        # ----------------------------------------------------

        except FloodWaitError as e:

            print()
            print(
                f"Telegram requested a {e.seconds}s cooldown."
            )

            print(
                f"Sleeping for {e.seconds} seconds..."
            )

            logger.warning(
                f"FloodWaitError: sleeping {e.seconds}s"
            )

            await asyncio.sleep(e.seconds)

            print(
                "Telegram cooldown finished."
            )

            print(
                "Retrying message..."
            )

            # Do NOT save the state here.
            # The same message will be retried.

        # ----------------------------------------------------
        # OTHER ERRORS
        # ----------------------------------------------------

        except Exception as e:

            print()
            print(
                f"ERROR on message {message.id}: {e}"
            )

            logger.error(
                f"Error forwarding message "
                f"{message.id}: {e}"
            )

            print(
                "Checkpoint NOT advanced."
            )

            print(
                "Waiting 30 seconds before retrying..."
            )

            if not client.is_connected():
                try:
                    await client.connect()
                    await resolve_channels()
                except Exception:
                    pass

            await asyncio.sleep(30)

    print()
    print("======================================")
    print(" HISTORICAL MEDIA MIGRATION COMPLETE")
    print("======================================")
    print(f"Media uploaded : {uploaded}")
    print(f"Messages skipped: {skipped}")
    print(f"Messages scanned: {scanned}")
    print()
    print("Now listening for new messages...")
    print()

    logger.info(
        f"Historical media migration complete. "
        f"Uploaded={uploaded}, "
        f"Skipped={skipped}, "
        f"Scanned={scanned}"
    )

# ============================================================
# FORWARD NEW MESSAGES
# ============================================================

@client.on(events.NewMessage)
async def handler(event):

    # Only process messages from our source channel
    if event.chat_id != source_channel_id:
        return

    message = event.message

    if not client.is_connected():
        await client.connect()
        await resolve_channels()

    try:

        # ------------------------------------------------
        # DETERMINE MEDIA TYPE
        # ------------------------------------------------

        media_type = classify_media(message)
        filename = message.file.name if message.file else None

        allowed = allowed_media_types()
        if not media_type or (allowed is not None and media_type not in allowed):

            logger.info(
                f"Skipped new message {message.id} "
                f"(not supported media)"
            )

            # Advance checkpoint because this message is
            # intentionally ignored.
            save_last_id(message.id)

            return

        # ------------------------------------------------
        # UPLOAD
        # ------------------------------------------------

        print(
            f"[LIVE UPLOAD] {message.id} "
            f"| {media_type} "
            f"| {filename or media_type}"
        )

        await client.send_file(
            target_channel,
            message.media,
            caption=message.text or ""
        )

        # Save only after successful upload
        save_last_id(message.id)

        logger.info(
            f"Forwarded new media message {message.id}"
        )

        print(
            f"[LIVE DONE] {message.id}"
        )

    except FloodWaitError as e:

        print(
            f"Telegram requested {e.seconds}s cooldown "
            f"for new message {message.id}."
        )

        await asyncio.sleep(e.seconds)

        # State is NOT advanced.
        # The message can be handled again.

    except Exception as e:

        logger.error(
            f"Error forwarding new message "
            f"{message.id}: {e}"
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    await resolve_channels()

    print()
    print("Fetching old messages and forwarding...")
    print()

    await copy_old_messages()


# ============================================================
# RUN
# ============================================================

with client:

    client.loop.run_until_complete(main())

    client.run_until_disconnected()
