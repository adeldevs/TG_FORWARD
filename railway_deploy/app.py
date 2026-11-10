import os
import re
import json
import glob
import asyncio
import threading
import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from dotenv import load_dotenv

load_dotenv()

ENV_PATH = ".env"
STATE_FILE = "state.json"
CUSTOM_CHANNELS_FILE = "custom_channels.json"
CHANNELS_CACHE_FILE = "channels_cache.json"

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

# ------------------------------------------------------------------
# Helper: read / write / remove .env values
# ------------------------------------------------------------------
def _read_env():
    """Return a dict of the current .env values."""
    if not os.path.exists(ENV_PATH):
        return {}
    env = {}
    with open(ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _write_env(values: dict):
    """Write or overwrite keys in .env, preserving unchanged lines."""
    lines = []
    kept_keys = set()
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
            for line in f:
                line_str = line.rstrip("\n")
                if not line_str or line_str.startswith("#"):
                    lines.append(line_str)
                    continue
                if "=" in line_str:
                    k = line_str.split("=", 1)[0].strip()
                    kept_keys.add(k)
                    if k in values:
                        lines.append(f"{k}={values[k]}")
                    else:
                        lines.append(line_str)
                else:
                    lines.append(line_str)
    for k, v in values.items():
        if k not in kept_keys:
            lines.append(f"{k}={v}")
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


def _remove_env_keys(keys_to_remove: list):
    """Remove specific keys from .env."""
    if not os.path.exists(ENV_PATH):
        return
    rem_set = set(keys_to_remove)
    new_lines = []
    with open(ENV_PATH, "r") as f:
        for line in f:
            line_str = line.rstrip("\n")
            if "=" in line_str and not line_str.startswith("#"):
                k = line_str.split("=", 1)[0].strip()
                if k in rem_set:
                    continue
            new_lines.append(line_str)
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(new_lines) + "\n")


# ------------------------------------------------------------------
# Channel storage & caching helpers
# ------------------------------------------------------------------
def _parse_id_list(raw_val: str):
    """Parse comma-separated or single numeric channel IDs."""
    if not raw_val:
        return []
    result = []
    for part in str(raw_val).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            pass
    return result


def _get_sources():
    env = _read_env()
    raw = env.get("SOURCE_CHANNELS") or env.get("SOURCE_CHANNEL") or os.getenv("SOURCE_CHANNELS") or os.getenv("SOURCE_CHANNEL") or ""
    return _parse_id_list(raw)


def _get_targets():
    env = _read_env()
    raw = env.get("TARGET_CHANNELS") or env.get("TARGET_CHANNEL") or os.getenv("TARGET_CHANNELS") or os.getenv("TARGET_CHANNEL") or ""
    return _parse_id_list(raw)


def _get_custom_channels():
    if not os.path.exists(CUSTOM_CHANNELS_FILE):
        return []
    try:
        with open(CUSTOM_CHANNELS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_custom_channels(items):
    with open(CUSTOM_CHANNELS_FILE, "w") as f:
        json.dump(items, f, indent=2)


def _get_channels_cache():
    if not os.path.exists(CHANNELS_CACHE_FILE):
        return {}
    try:
        with open(CHANNELS_CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_channels_cache(cache):
    with open(CHANNELS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _resolve_channel_info(cid: int, cache: dict, custom_list: list):
    """Return dictionary with title, username, type for a given ID."""
    str_id = str(cid)
    if str_id in cache:
        return cache[str_id]
    for c in custom_list:
        if c.get("id") == cid:
            return c
    return {"id": cid, "title": f"Channel {cid}", "username": None, "type": "Channel"}


def save_checkpoint(chat_id, msg_id):
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
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def allowed_media_types():
    raw = os.getenv("ALLOW_MEDIA_TYPES") or _read_env().get("ALLOW_MEDIA_TYPES", "all")
    raw = raw.strip().lower()
    if not raw or raw == "all":
        return None
    return {t.strip() for t in raw.split(",") if t.strip()}


def classify_media(message):
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.audio or message.voice:
        return "audio"
    if message.document:
        filename = message.file.name if message.file else None
        if filename:
            ext = os.path.splitext(filename)[1].lower()
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


# ------------------------------------------------------------------
# Thread-safe persistent asyncio loop for Telethon
# ------------------------------------------------------------------
_loop = asyncio.new_event_loop()


def _start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


_loop_thread = threading.Thread(target=_start_background_loop, args=(_loop,), daemon=True)
_loop_thread.start()


def _run(coro, timeout=30):
    """Run a coroutine safely on the background event loop."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


# ------------------------------------------------------------------
# Telethon Client Management
# ------------------------------------------------------------------
_client = None
_phone_code_hashes = {}
_forwarder_handler = None
_forwarder_stats = {
    "running": False,
    "forwarded": 0,
    "skipped": 0,
    "last_event": None,
    "error": None,
}


def _get_client():
    global _client
    if _client is None:
        env = _read_env()
        api_id_raw = env.get("API_ID") or os.getenv("API_ID", "0")
        api_id = int(api_id_raw) if api_id_raw and api_id_raw.isdigit() else 0
        api_hash = env.get("API_HASH") or os.getenv("API_HASH", "")
        session_string = env.get("SESSION_STRING") or os.getenv("SESSION_STRING", None)
        session_name = env.get("SESSION_NAME") or os.getenv("SESSION_NAME", "forwarder_session")

        if api_id and api_hash:
            if session_string:
                _client = TelegramClient(StringSession(session_string), api_id, api_hash, loop=_loop)
            else:
                _client = TelegramClient(session_name, api_id, api_hash, loop=_loop)
    return _client


async def _is_authorized():
    client = _get_client()
    if not client:
        return False
    if not client.is_connected():
        await client.connect()
    return await client.is_user_authorized()


# ------------------------------------------------------------------
# Background Forwarder Engine
# ------------------------------------------------------------------
async def _setup_forwarder_async():
    global _forwarder_handler, _forwarder_stats
    client = _get_client()
    if not client or not client.is_connected() or not await client.is_user_authorized():
        _forwarder_stats["running"] = False
        return False

    sources = _get_sources()
    targets = _get_targets()
    if not sources or not targets:
        _forwarder_stats["running"] = False
        return False

    # Remove existing handler
    if _forwarder_handler:
        try:
            client.remove_event_handler(_forwarder_handler)
        except Exception:
            pass
        _forwarder_handler = None

    # Resolve target entities
    target_entities = []
    for tid in targets:
        try:
            ent = await client.get_entity(tid)
            target_entities.append(ent)
        except Exception as e:
            print(f"[Forwarder] Warning: could not resolve target {tid}: {e}")

    if not target_entities:
        _forwarder_stats["running"] = False
        return False

    async def forwarder_event_handler(event):
        if not _forwarder_stats["running"]:
            return
        if event.chat_id not in sources:
            return

        message = event.message
        media_type = classify_media(message)
        allowed = allowed_media_types()

        if allowed is not None:
            if not media_type or media_type not in allowed:
                _forwarder_stats["skipped"] += 1
                return

        success_any = False
        for target in target_entities:
            try:
                if message.media:
                    await client.send_file(target, message.media, caption=message.text or "")
                elif message.text:
                    await client.send_message(target, message.text)
                success_any = True
            except FloodWaitError as e:
                _forwarder_stats["error"] = f"FloodWait: sleeping {e.seconds}s"
                await asyncio.sleep(e.seconds)
            except Exception as e:
                _forwarder_stats["error"] = str(e)

        if success_any:
            _forwarder_stats["forwarded"] += 1
            _forwarder_stats["last_event"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_checkpoint(event.chat_id, message.id)

    _forwarder_handler = forwarder_event_handler
    client.add_event_handler(forwarder_event_handler, events.NewMessage)
    _forwarder_stats["running"] = True
    _forwarder_stats["error"] = None
    return True


def _start_forwarder():
    try:
        return _run(_setup_forwarder_async(), timeout=15)
    except Exception as e:
        _forwarder_stats["error"] = str(e)
        return False


def _stop_forwarder():
    global _forwarder_handler, _forwarder_stats
    _forwarder_stats["running"] = False
    client = _get_client()
    if client and _forwarder_handler:
        try:
            client.remove_event_handler(_forwarder_handler)
        except Exception:
            pass
        _forwarder_handler = None


# ------------------------------------------------------------------
# Flask App Setup
# ------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)


@app.route("/favicon.ico")
def favicon():
    svg_icon = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#2563eb"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 0 0-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.75-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z"/></svg>"""
    return Response(svg_icon, mimetype="image/svg+xml")


# ------------------------------------------------------------------
# 1. Root route: smart routing
# ------------------------------------------------------------------
@app.route("/")
def index():
    env = _read_env()
    api_id = env.get("API_ID") or os.getenv("API_ID")
    api_hash = env.get("API_HASH") or os.getenv("API_HASH")

    if not api_id or not api_hash:
        return redirect(url_for("config"))

    try:
        is_auth = _run(_is_authorized(), timeout=5)
        if is_auth:
            return redirect(url_for("channels"))
    except Exception:
        pass
    return redirect(url_for("login"))


# ------------------------------------------------------------------
# 2. Config page – ONLY API credentials (no source/target channel)
# ------------------------------------------------------------------
@app.route("/config", methods=["GET", "POST"])
def config():
    global _client
    env = _read_env()
    is_auth = False
    try:
        is_auth = _run(_is_authorized(), timeout=5)
    except Exception:
        pass

    if request.method == "POST":
        new_vals = {
            "API_ID": request.form.get("API_ID", "").strip(),
            "API_HASH": request.form.get("API_HASH", "").strip(),
            "SESSION_NAME": request.form.get("SESSION_NAME", "").strip() or "forwarder_session",
            "SESSION_STRING": request.form.get("SESSION_STRING", "").strip(),
        }
        filtered = {k: v for k, v in new_vals.items() if v}
        _write_env(filtered)
        for k, v in filtered.items():
            os.environ[k] = v

        # Reset client to reload new credentials
        _client = None

        if filtered.get("SESSION_STRING"):
            try:
                if _run(_is_authorized(), timeout=10):
                    return redirect(url_for("channels"))
            except Exception:
                pass
        return redirect(url_for("login"))

    return render_template(
        "config.html",
        api_id=env.get("API_ID", os.getenv("API_ID", "")),
        api_hash=env.get("API_HASH", os.getenv("API_HASH", "")),
        session_name=env.get("SESSION_NAME", os.getenv("SESSION_NAME", "forwarder_session")),
        session_string=env.get("SESSION_STRING", os.getenv("SESSION_STRING", "")),
        is_logged_in=is_auth,
    )


# ------------------------------------------------------------------
# 3. Login page – phone number input
# ------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    env = _read_env()
    if not (env.get("API_ID") or os.getenv("API_ID")) or not (env.get("API_HASH") or os.getenv("API_HASH")):
        return redirect(url_for("config"))

    logged_out = request.args.get("logged_out") == "1"

    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if not phone:
            return render_template("login.html", error="Please enter a valid phone number.", logged_out=False)

        client = _get_client()
        async def send_code():
            if not client.is_connected():
                await client.connect()
            return await client.send_code_request(phone)

        try:
            res = _run(send_code(), timeout=20)
            _phone_code_hashes[phone] = res.phone_code_hash
            return redirect(url_for("otp", phone=phone))
        except FloodWaitError as e:
            return render_template("login.html", error=f"Telegram flood wait: please wait {e.seconds} seconds before trying again.", phone=phone, logged_out=False)
        except Exception as exc:
            return render_template("login.html", error=f"Error sending login code: {exc}", phone=phone, logged_out=False)

    return render_template("login.html", logged_out=logged_out)


# ------------------------------------------------------------------
# 4. OTP verification page (handles 2FA password too)
# ------------------------------------------------------------------
@app.route("/otp", methods=["GET", "POST"])
def otp():
    phone = request.args.get("phone", "") or request.form.get("phone", "")
    if not phone:
        return redirect(url_for("login"))

    client = _get_client()
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        password = request.form.get("password", "").strip()
        code_hash = _phone_code_hashes.get(phone)

        async def sign_in():
            if not client.is_connected():
                await client.connect()
            try:
                if code_hash:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
                else:
                    await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                if password:
                    await client.sign_in(password=password)
                else:
                    raise

        try:
            _run(sign_in(), timeout=20)
            # Save session string
            session_str = client.session.save()
            _write_env({"SESSION_STRING": session_str})
            os.environ["SESSION_STRING"] = session_str
            return redirect(url_for("channels"))
        except SessionPasswordNeededError:
            return render_template(
                "otp.html",
                phone=phone,
                requires_password=True,
                error="Two-step verification (2FA) is enabled on your account. Please enter your cloud password.",
            )
        except Exception as exc:
            return render_template(
                "otp.html",
                phone=phone,
                requires_password=False,
                error=f"Sign in failed: {exc}",
            )

    return render_template("otp.html", phone=phone, requires_password=False)


# ------------------------------------------------------------------
# 5. Channels Dashboard – Table of sources & destinations
# ------------------------------------------------------------------
@app.route("/channels")
def channels():
    client = _get_client()
    env = _read_env()
    cache = _get_channels_cache()
    custom_chans = _get_custom_channels()

    async def fetch_dialogs():
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram not logged in")

        dialogs = await client.get_dialogs()
        dialogs_dict = {}

        for d in dialogs:
            if d.is_channel or d.is_group:
                entity = d.entity
                username = getattr(entity, "username", None)
                is_ch = bool(getattr(d, "is_channel", False))
                is_gr = bool(getattr(d, "is_group", False))
                chan_data = {
                    "id": d.id,
                    "title": d.title or f"Chat {d.id}",
                    "username": username,
                    "is_channel": is_ch,
                    "is_group": is_gr,
                    "type": "Channel" if is_ch else "Group",
                }
                dialogs_dict[d.id] = chan_data

        # Merge custom channels
        for c in custom_chans:
            cid = c.get("id")
            if cid and cid not in dialogs_dict:
                dialogs_dict[cid] = c

        # Update cache
        for cid, data in dialogs_dict.items():
            cache[str(cid)] = data
        _save_channels_cache(cache)

        # Sort list by title
        result = list(dialogs_dict.values())
        result.sort(key=lambda x: (x.get("title") or "").lower())
        return result

    try:
        available_channels = _run(fetch_dialogs(), timeout=25)
    except Exception as exc:
        return render_template(
            "channels.html",
            channels=[],
            source_channels=[],
            target_channels=[],
            selected_source_ids=[],
            selected_target_ids=[],
            media_types=env.get("ALLOW_MEDIA_TYPES", "all"),
            forwarder_running=False,
            forwarder_stats=_forwarder_stats,
            error=f"Could not connect to Telegram: {exc}. Please log in first.",
        )

    # Get selected IDs
    source_ids = _get_sources()
    target_ids = _get_targets()

    # Resolve full objects with channel names
    source_channels = [_resolve_channel_info(cid, cache, custom_chans) for cid in source_ids]
    target_channels = [_resolve_channel_info(cid, cache, custom_chans) for cid in target_ids]

    # Automatically start forwarder if configured and not yet running
    if source_ids and target_ids and not _forwarder_stats["running"]:
        _start_forwarder()

    message = request.args.get("msg")

    return render_template(
        "channels.html",
        channels=available_channels,
        source_channels=source_channels,
        target_channels=target_channels,
        selected_source_ids=source_ids,
        selected_target_ids=target_ids,
        media_types=env.get("ALLOW_MEDIA_TYPES", "all"),
        forwarder_running=_forwarder_stats["running"],
        forwarder_stats=_forwarder_stats,
        message=message,
    )


# ------------------------------------------------------------------
# 6. Save multiple channel selections
# ------------------------------------------------------------------
@app.route("/set-channels", methods=["POST"])
@app.route("/channels", methods=["POST"])
def set_channels():
    source_ids = _parse_id_list(",".join(request.form.getlist("source_ids")))
    target_ids = _parse_id_list(",".join(request.form.getlist("target_ids")))

    src_str = ",".join(str(x) for x in source_ids)
    tgt_str = ",".join(str(x) for x in target_ids)

    vals = {
        "SOURCE_CHANNELS": src_str,
        "TARGET_CHANNELS": tgt_str,
        "SOURCE_CHANNEL": str(source_ids[0]) if source_ids else "",
        "TARGET_CHANNEL": str(target_ids[0]) if target_ids else "",
    }
    _write_env(vals)
    for k, v in vals.items():
        os.environ[k] = v

    # Restart forwarder if running
    if _forwarder_stats["running"]:
        _start_forwarder()

    return redirect(url_for("channels", msg="Channel selection saved successfully!"))


# ------------------------------------------------------------------
# 7. Add Custom or Public Channel
# ------------------------------------------------------------------
@app.route("/add-channel", methods=["POST"])
def add_channel():
    link = request.form.get("link", "").strip()
    role = request.form.get("role", "none")

    if not link:
        return redirect(url_for("channels"))

    client = _get_client()

    async def resolve_and_add():
        if not client.is_connected():
            await client.connect()

        target = link
        # Clean URL if provided
        if "t.me/" in target:
            target = target.split("t.me/")[-1].split("?")[0].strip()

        # Handle joinchat / invite hash
        if target.startswith("+"):
            from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
            invite_hash = target[1:]
            try:
                res = await client(ImportChatInviteRequest(invite_hash))
                entity = res.chats[0]
            except Exception:
                res = await client(CheckChatInviteRequest(invite_hash))
                entity = getattr(res, "chat", res)
        else:
            if target.startswith("@"):
                target = target[1:]
            try:
                numeric_id = int(target)
                entity = await client.get_entity(numeric_id)
            except ValueError:
                entity = await client.get_entity(target)

        peer_id = utils.get_peer_id(entity)
        title = utils.get_display_name(entity)
        username = getattr(entity, "username", None)
        is_ch = bool(getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False))
        is_gr = bool(getattr(entity, "gigagroup", False) or getattr(entity, "megagroup", False) or not is_ch)

        # Attempt to join public channel
        if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
            from telethon.tl.functions.channels import JoinChannelRequest
            try:
                await client(JoinChannelRequest(entity))
            except Exception:
                pass

        chan_data = {
            "id": peer_id,
            "title": title or (f"@{username}" if username else f"Channel {peer_id}"),
            "username": username,
            "is_channel": is_ch,
            "is_group": is_gr,
            "type": "Channel" if is_ch else "Group",
            "is_custom": True,
        }

        # Save to custom channels
        custom = _get_custom_channels()
        custom = [c for c in custom if c.get("id") != peer_id]
        custom.append(chan_data)
        _save_custom_channels(custom)

        # Save to cache
        cache = _get_channels_cache()
        cache[str(peer_id)] = chan_data
        _save_channels_cache(cache)

        return chan_data

    try:
        resolved = _run(resolve_and_add(), timeout=25)
        peer_id = resolved["id"]

        # Apply role if requested
        if role in ("source", "both"):
            sources = _get_sources()
            if peer_id not in sources:
                sources.append(peer_id)
                src_str = ",".join(str(x) for x in sources)
                _write_env({"SOURCE_CHANNELS": src_str, "SOURCE_CHANNEL": str(sources[0])})
                os.environ["SOURCE_CHANNELS"] = src_str
                os.environ["SOURCE_CHANNEL"] = str(sources[0])

        if role in ("target", "both"):
            targets = _get_targets()
            if peer_id not in targets:
                targets.append(peer_id)
                tgt_str = ",".join(str(x) for x in targets)
                _write_env({"TARGET_CHANNELS": tgt_str, "TARGET_CHANNEL": str(targets[0])})
                os.environ["TARGET_CHANNELS"] = tgt_str
                os.environ["TARGET_CHANNEL"] = str(targets[0])

        if _forwarder_stats["running"]:
            _start_forwarder()

        return redirect(url_for("channels", msg=f"Added '{resolved['title']}' successfully!"))
    except Exception as exc:
        return redirect(url_for("channels", error=f"Failed to add channel: {exc}"))


# ------------------------------------------------------------------
# 8. Single channel helpers (/set-source, /set-target, /remove-channel)
# ------------------------------------------------------------------
@app.route("/set-source", methods=["POST"])
def set_source():
    cid_raw = request.form.get("channel_id", "").strip()
    if cid_raw:
        try:
            cid = int(cid_raw)
            sources = _get_sources()
            if cid not in sources:
                sources.append(cid)
            src_str = ",".join(str(x) for x in sources)
            _write_env({"SOURCE_CHANNELS": src_str, "SOURCE_CHANNEL": str(sources[0])})
            os.environ["SOURCE_CHANNELS"] = src_str
            os.environ["SOURCE_CHANNEL"] = str(sources[0])
            if _forwarder_stats["running"]:
                _start_forwarder()
        except ValueError:
            pass
    return redirect(url_for("channels"))


@app.route("/set-target", methods=["POST"])
def set_target():
    cid_raw = request.form.get("channel_id", "").strip()
    if cid_raw:
        try:
            cid = int(cid_raw)
            targets = _get_targets()
            if cid not in targets:
                targets.append(cid)
            tgt_str = ",".join(str(x) for x in targets)
            _write_env({"TARGET_CHANNELS": tgt_str, "TARGET_CHANNEL": str(targets[0])})
            os.environ["TARGET_CHANNELS"] = tgt_str
            os.environ["TARGET_CHANNEL"] = str(targets[0])
            if _forwarder_stats["running"]:
                _start_forwarder()
        except ValueError:
            pass
    return redirect(url_for("channels"))


@app.route("/remove-channel", methods=["POST"])
def remove_channel():
    cid_raw = request.form.get("channel_id", "").strip()
    role = request.form.get("role", "source")
    if cid_raw:
        try:
            cid = int(cid_raw)
            if role == "source":
                sources = [x for x in _get_sources() if x != cid]
                src_str = ",".join(str(x) for x in sources)
                _write_env({"SOURCE_CHANNELS": src_str, "SOURCE_CHANNEL": str(sources[0]) if sources else ""})
                os.environ["SOURCE_CHANNELS"] = src_str
                os.environ["SOURCE_CHANNEL"] = str(sources[0]) if sources else ""
            elif role == "target":
                targets = [x for x in _get_targets() if x != cid]
                tgt_str = ",".join(str(x) for x in targets)
                _write_env({"TARGET_CHANNELS": tgt_str, "TARGET_CHANNEL": str(targets[0]) if targets else ""})
                os.environ["TARGET_CHANNELS"] = tgt_str
                os.environ["TARGET_CHANNEL"] = str(targets[0]) if targets else ""
            if _forwarder_stats["running"]:
                _start_forwarder()
        except ValueError:
            pass
    return redirect(url_for("channels"))


# ------------------------------------------------------------------
# 9. Media types filter
# ------------------------------------------------------------------
@app.route("/set-media-types", methods=["POST"])
def set_media_types():
    types = request.form.getlist("media_types")
    value = "all" if ("all" in types or not types) else ",".join(types)
    os.environ["ALLOW_MEDIA_TYPES"] = value
    _write_env({"ALLOW_MEDIA_TYPES": value})
    return redirect(url_for("channels", msg="Media type filters saved!"))


# ------------------------------------------------------------------
# 10. Forwarder start / stop controls
# ------------------------------------------------------------------
@app.route("/forwarder/start", methods=["POST"])
def forwarder_start():
    _start_forwarder()
    return redirect(url_for("channels"))


@app.route("/forwarder/stop", methods=["POST"])
def forwarder_stop():
    _stop_forwarder()
    return redirect(url_for("channels"))


# ------------------------------------------------------------------
# 11. Current status API (AJAX endpoint with channel names)
# ------------------------------------------------------------------
@app.route("/current")
def current():
    cache = _get_channels_cache()
    custom_chans = _get_custom_channels()
    source_ids = _get_sources()
    target_ids = _get_targets()

    sources_info = [_resolve_channel_info(cid, cache, custom_chans) for cid in source_ids]
    targets_info = [_resolve_channel_info(cid, cache, custom_chans) for cid in target_ids]

    from_names = ", ".join(s["title"] for s in sources_info) or "None selected"
    to_names = ", ".join(t["title"] for t in targets_info) or "None selected"

    return jsonify({
        "from": from_names,
        "to": to_names,
        "sources": sources_info,
        "targets": targets_info,
        "media_types": os.getenv("ALLOW_MEDIA_TYPES") or _read_env().get("ALLOW_MEDIA_TYPES", "all"),
        "forwarder": {
            "running": _forwarder_stats["running"],
            "forwarded": _forwarder_stats["forwarded"],
            "skipped": _forwarder_stats["skipped"],
            "last_event": _forwarder_stats["last_event"],
            "error": _forwarder_stats["error"],
        }
    })


# ------------------------------------------------------------------
# 12. Complete Logout & Data Wipe
# ------------------------------------------------------------------
@app.route("/logout", methods=["GET", "POST"])
def logout():
    global _client
    # 1. Stop background forwarder
    _stop_forwarder()

    # 2. Telethon client log out and disconnect
    if _client:
        async def do_logout():
            try:
                if _client.is_connected() and await _client.is_user_authorized():
                    await _client.log_out()
            except Exception:
                pass
            try:
                await _client.disconnect()
            except Exception:
                pass

        try:
            _run(do_logout(), timeout=10)
        except Exception:
            pass
        _client = None

    # 3. Delete session files and state files
    session_files = [
        "forwarder_session.session",
        "forwarder_session.session-journal",
        "forwarder_session_fresh.session",
        "railway_forwarder_session.session",
        "state.json",
        "checkpoint.json",
        "custom_channels.json",
        "channels_cache.json",
    ]
    for sf in session_files:
        try:
            if os.path.exists(sf):
                os.remove(sf)
        except Exception:
            pass

    for sf in glob.glob("*.session*"):
        try:
            os.remove(sf)
        except Exception:
            pass

    # 4. Remove session and channel keys from .env
    keys_to_clear = [
        "SESSION_STRING",
        "SOURCE_CHANNEL",
        "TARGET_CHANNEL",
        "SOURCE_CHANNELS",
        "TARGET_CHANNELS",
        "ALLOW_MEDIA_TYPES",
    ]
    _remove_env_keys(keys_to_clear)
    for k in keys_to_clear:
        os.environ.pop(k, None)

    return redirect(url_for("login", logged_out="1"))


# ------------------------------------------------------------------
# 13. Guide page
# ------------------------------------------------------------------
@app.route("/guide")
def guide():
    return render_template("guide.html")


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
