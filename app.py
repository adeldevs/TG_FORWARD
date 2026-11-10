import os
import re
import asyncio
from flask import Flask, render_template, request, redirect, url_for, jsonify
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

# ------------------------------------------------------------------
# Helper: read / write .env keeping any other lines the user may have
# ------------------------------------------------------------------
ENV_PATH = ".env"


def _read_env():
    """Return a dict of the current .env values (or empty dict)."""
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
    """Write *only* the given keys to .env, preserving any other lines."""
    existing = _read_env()
    # start from whatever is already in the file
    lines = []
    kept_keys = set()
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    lines.append(line)
                    continue
                if "=" in line:
                    k = line.split("=", 1)[0].strip()
                    kept_keys.add(k)
                    if k in values:
                        # overwrite with new value
                        lines.append(f"{k}={values[k]}")
                    else:
                        # keep old line
                        lines.append(line)
                else:
                    lines.append(line)
    # now ensure the requested keys exist (add at end if missing)
    for k, v in values.items():
        if k not in kept_keys:
            lines.append(f"{k}={v}")
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------
# Flask app & Telethon client (lazy‑loaded)
# ------------------------------------------------------------------
app = Flask(__name__)
_client = None


def _get_client():
    """Return a Telethon client, initialising it once."""
    global _client
    if _client is None:
        session_string = os.getenv("SESSION_STRING", None)
        api_id = int(os.getenv("API_ID", "0"))
        api_hash = os.getenv("API_HASH", "")
        session_name = os.getenv("SESSION_NAME", "forwarder_session")
        if session_string:
            _client = TelegramClient(StringSession(session_string), api_id, api_hash)
        else:
            _client = TelegramClient(session_name, api_id, api_hash)
    return _client


# ------------------------------------------------------------------
# Root – redirect to config
# ------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("config"))


# ------------------------------------------------------------------
# 1️⃣  Config page – fill API / channel info
# ------------------------------------------------------------------
@app.route("/config", methods=["GET", "POST"])
def config():
    env = _read_env()
    if request.method == "POST":
        new_vals = {
            "API_ID": request.form.get("API_ID", "").strip(),
            "API_HASH": request.form.get("API_HASH", "").strip(),
            "SESSION_NAME": request.form.get("SESSION_NAME", "").strip(),
            "SESSION_STRING": request.form.get("SESSION_STRING", "").strip(),
            "SOURCE_CHANNEL": request.form.get("SOURCE_CHANNEL", "").strip(),
            "TARGET_CHANNEL": request.form.get("TARGET_CHANNEL", "").strip(),
        }
        filtered = {k: v for k, v in new_vals.items() if v}
        _write_env(filtered)
        for k, v in filtered.items():
            os.environ[k] = v
        return redirect(url_for("login"))
    return render_template(
        "config.html",
        api_id=env.get("API_ID", ""),
        api_hash=env.get("API_HASH", ""),
        session_name=env.get("SESSION_NAME", ""),
        session_string=env.get("SESSION_STRING", ""),
        source_channel=env.get("SOURCE_CHANNEL", ""),
        target_channel=env.get("TARGET_CHANNEL", ""),
    )


# ------------------------------------------------------------------
# 2️⃣  Login page – phone number
# ------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form["phone"]
        client = _get_client()
        async def send_code():
            await client.connect()
            await client.send_code_request(phone)
        asyncio.run(send_code())
        return redirect(url_for("otp", phone=phone))
    return render_template("login.html")


# ------------------------------------------------------------------
# 3️⃣  OTP verification page
# ------------------------------------------------------------------
@app.route("/otp", methods=["GET", "POST"])
def otp():
    phone = request.args.get("phone", "")
    if request.method == "POST":
        code = request.form["code"]
        client = _get_client()
        async def sign_in():
            await client.sign_in(code=int(code))
        asyncio.run(sign_in())
        # persist session string for future runs
        session_str = client.session.save()
        env = _read_env()
        if "SESSION_STRING" not in env:
            with open(ENV_PATH, "a") as f:
                f.write(f"\nSESSION_STRING={session_str}")
        else:
            with open(ENV_PATH, "r") as f:
                content = f.read()
            content = re.sub(r"^SESSION_STRING=.*", f"SESSION_STRING={session_str}", content, flags=re.MULTILINE)
            with open(ENV_PATH, "w") as f:
                f.write(content)
        return redirect(url_for("channels"))
    return render_template("otp.html", phone=phone)


# ------------------------------------------------------------------
# 4️⃣  Channels page – list, select, add custom
# ------------------------------------------------------------------
@app.route("/channels", methods=["GET", "POST"])
def channels():
    client = _get_client()
    env = _read_env()
    source_id = int(os.getenv("SOURCE_CHANNEL", "0"))
    target_id = int(os.getenv("TARGET_CHANNEL", "0"))

    async def fetch_dialogs():
        if not client.is_connected():
            await client.connect()
        dialogs = await client.get_dialogs()
        chans = []
        for d in dialogs:
            if d.is_channel or d.is_group:
                chans.append((d.id, d.title, d.is_channel, d.is_group))
        return chans

    try:
        channels_list = asyncio.run(fetch_dialogs())
    except Exception as exc:
        return render_template(
            "channels.html",
            channels=[],
            source_id=source_id,
            target_id=target_id,
            media_types=env.get("ALLOW_MEDIA_TYPES", "all"),
            error=f"Telegram not logged in yet: {exc}. Go to /login first.",
        )

    if request.method == "POST":
        action = request.form.get("action")
        if action == "set_source":
            cid = int(request.form["channel_id"])
            os.environ["SOURCE_CHANNEL"] = str(cid)
            _write_env({"SOURCE_CHANNEL": cid})
            return redirect(url_for("channels"))
        elif action == "set_target":
            cid = int(request.form["channel_id"])
            os.environ["TARGET_CHANNEL"] = str(cid)
            _write_env({"TARGET_CHANNEL": cid})
            return redirect(url_for("channels"))
        elif action == "add_channel":
            link = request.form["link"].strip()
            async def resolve():
                if link.startswith("@"):
                    entity = await client.get_input_entity(link[1:])
                else:
                    entity = await client.get_input_entity(link)
                return entity.id, entity.title
            try:
                eid, title = asyncio.run(resolve())
            except Exception as exc:
                return jsonify(error=str(exc)), 400
            os.environ["SOURCE_CHANNEL"] = str(eid)
            _write_env({"SOURCE_CHANNEL": eid})
            return redirect(url_for("channels"))
        elif action == "set_media_types":
            types = request.form.getlist("media_types")
            value = ",".join(types)
            os.environ["ALLOW_MEDIA_TYPES"] = value
            _write_env({"ALLOW_MEDIA_TYPES": value})
            return redirect(url_for("channels"))

    return render_template(
        "channels.html",
        channels=channels_list,
        source_id=source_id,
        target_id=target_id,
        media_types=env.get("ALLOW_MEDIA_TYPES", "all") if (env := _read_env()) else "all",
    )


@app.route("/set-media-types", methods=["POST"])
def set_media_types():
    client = _get_client()
    types = request.form.getlist("media_types")
    value = ",".join(types)
    os.environ["ALLOW_MEDIA_TYPES"] = value
    _write_env({"ALLOW_MEDIA_TYPES": value})
    return redirect(url_for("channels"))


@app.route("/guide")
def guide():
    return render_template("guide.html")


@app.route("/current")
def current():
    """AJAX endpoint – returns the two IDs so the page can display them."""
    return jsonify({
        "from": os.getenv("SOURCE_CHANNEL", "0"),
        "to": os.getenv("TARGET_CHANNEL", "0"),
        "media_types": os.getenv("ALLOW_MEDIA_TYPES", "all"),
    })


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))