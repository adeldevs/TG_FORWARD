import os
import json
import asyncio

from aiohttp import web
from decouple import config as getenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession

api_id = int(getenv("API_ID"))
api_hash = getenv("API_HASH")

source_channel_id = int(getenv("SOURCE_CHANNEL"))
target_channel_id = int(getenv("TARGET_CHANNEL"))

session_name = getenv("SESSION_NAME", default="forwarder_session")
STATE_FILE = getenv("STATE_FILE", default="state.json")
LOG_FILE = getenv("LOG_FILE", default="forwarder.log")

MESSAGE_DELAY = 5
BATCH_SIZE = 20
BATCH_COOLDOWN = 60

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",
    ".mpeg", ".mpg", ".ts", ".m2ts", ".3gp", ".ogv",
}

status = {
    "state": "starting",
    "current_message_id": None,
    "current_filename": None,
    "uploaded": 0,
    "skipped": 0,
    "scanned": 0,
    "last_id": 0,
    "latest_id": None,
    "error": None,
}

source_channel = None
target_channel = None

session_string = getenv("SESSION_STRING", default=None)
if not session_string and os.path.exists("session_string.txt"):
    try:
        session_string = open("session_string.txt").read().strip()
    except Exception:
        session_string = None

if session_string:
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
else:
    client = TelegramClient(StringSession(), api_id, api_hash)


SKIP_BEFORE_ID = 963090

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            st = json.load(open(STATE_FILE))
            if st.get("last_id", 0) < SKIP_BEFORE_ID:
                st["last_id"] = SKIP_BEFORE_ID
            return st
        except Exception:
            pass
    return {"last_id": SKIP_BEFORE_ID, "source": source_channel_id, "target": target_channel_id}


def save_state(msg_id):
    state = {"last_id": msg_id, "source": source_channel_id, "target": target_channel_id}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


async def resolve_channels():
    global source_channel, target_channel
    dialogs = await client.get_dialogs()
    for dialog in dialogs:
        if dialog.id == source_channel_id:
            source_channel = dialog.entity
        if dialog.id == target_channel_id:
            target_channel = dialog.entity
    if source_channel is None:
        raise ValueError(f"Source channel {source_channel_id} not found")
    if target_channel is None:
        raise ValueError(f"Target channel {target_channel_id} not found")


def media_type_of(message):
    if message.photo:
        return "PHOTO", None
    if message.video:
        return "VIDEO", None
    if message.document and not message.voice and not message.audio:
        filename = None
        if message.file:
            filename = message.file.name
        if filename:
            import os as _os
            ext = _os.path.splitext(filename)[1].lower()
            if ext in VIDEO_EXTENSIONS:
                return "FILE", filename
    return None, None


async def migration_task():
    global status, source_channel, target_channel
    try:
        if not client.is_connected():
            await client.connect()
        if not await client.is_user_authorized():
            status["state"] = "needs_login"
            return
        await resolve_channels()

        @client.on(events.NewMessage)
        async def live_handler(event):
            global status
            if event.chat_id != source_channel_id:
                return
            message = event.message
            # only handle messages newer than the historical snapshot
            if status.get("latest_id") and message.id <= status["latest_id"]:
                return
            if not client.is_connected():
                await client.connect()
                await resolve_channels()

            media_type, filename = media_type_of(message)
            if not media_type:
                save_state(message.id)
                status["last_id"] = message.id
                status["skipped"] = status.get("skipped", 0) + 1
                return
            try:
                await client.send_file(target_channel, message.media, caption=message.text or "")
                save_state(message.id)
                status["uploaded"] = status.get("uploaded", 0) + 1
                status["last_id"] = message.id
                status["current_filename"] = filename or media_type
                status["current_message_id"] = message.id
            except Exception as e:
                status["error"] = str(e)
                if not client.is_connected():
                    try:
                        await client.connect()
                        await resolve_channels()
                    except Exception:
                        pass

        status["state"] = "running"

        state = load_state()
        last_id = state.get("last_id", 0)
        status["last_id"] = last_id

        latest_messages = await client.get_messages(source_channel, limit=1)
        latest_id = latest_messages[0].id if latest_messages else last_id
        status["latest_id"] = latest_id

        uploaded = status["uploaded"]
        skipped = status["skipped"]
        scanned = status["scanned"]

        async for message in client.iter_messages(source_channel, reverse=True, min_id=last_id):
            if status.get("latest_id") and message.id > status["latest_id"]:
                break
            if not client.is_connected():
                await client.connect()
                await resolve_channels()

            scanned += 1
            status["scanned"] = scanned
            status["current_message_id"] = message.id

            media_type, filename = media_type_of(message)

            if not media_type:
                skipped += 1
                status["skipped"] = skipped
                save_state(message.id)
                status["last_id"] = message.id
                continue

            display_name = filename or media_type
            status["current_filename"] = display_name

            try:
                await client.send_file(target_channel, message.media, caption=message.text or "")
                save_state(message.id)
                uploaded += 1
                status["uploaded"] = uploaded
                status["last_id"] = message.id

                await asyncio.sleep(MESSAGE_DELAY)
                if uploaded % BATCH_SIZE == 0:
                    await asyncio.sleep(BATCH_COOLDOWN)
            except Exception as e:
                status["error"] = str(e)
                if not client.is_connected():
                    try:
                        await client.connect()
                        await resolve_channels()
                    except Exception:
                        pass
                await asyncio.sleep(30)

        status["state"] = "complete"
    except Exception as e:
        status["state"] = "error"
        status["error"] = str(e)


HTML = """<!doctype html>
<html><head><meta charset=utf-8><title>Forwarder Status</title>
<style>body{font-family:sans-serif;margin:20px} .card{border:1px solid #ccc;padding:12px;margin:10px 0;border-radius:8px}</style>
</head><body>
<h1>Telegram Forwarder</h1>
<div class=card id=s>loading...</div>
<div class=card>
<h3>Login (if needed)</h3>
<input id=phone placeholder="+91..." /> <button onclick=sendPhone()>Send Code</button><br/>
<input id=code placeholder="code" /> <button onclick=sendCode()>Submit Code</button>
<div id=l></div>
</div>
<script>
async function refresh(){
  const r=await fetch('/api/status'); const d=await r.json();
  document.getElementById('s').innerHTML=
    'State: '+d.state+'<br>'+
    'Current msg: '+d.current_message_id+'<br>'+
    'Current file: '+(d.current_filename||'-')+'<br>'+
    'Uploaded: '+d.uploaded+'<br>'+
    'Skipped: '+d.skipped+'<br>'+
    'Scanned: '+d.scanned+'<br>'+
    'Last id: '+d.last_id+'<br>'+
    'Latest id: '+d.latest_id+'<br>'+
    'Error: '+(d.error||'-');
}
async function sendPhone(){
  const phone=document.getElementById('phone').value;
  const r=await fetch('/api/login/phone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})});
  const d=await r.json();
  document.getElementById('l').innerText=JSON.stringify(d);
}
async function sendCode(){
  const code=document.getElementById('code').value;
  const r=await fetch('/api/login/code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
  const d=await r.json();
  document.getElementById('l').innerText=JSON.stringify(d);
}
setInterval(refresh,2000); refresh();
</script></body></html>"""


async def index(request):
    return web.Response(text=HTML, content_type="text/html")


async def api_status(request):
    return web.json_response(status)


async def login_phone(request):
    data = await request.json()
    phone = data.get("phone")
    app = request.app
    if not client.is_connected():
        await client.connect()
    res = await client.send_code_request(phone)
    app["phone"] = phone
    app["code_hash"] = res.phone_code_hash
    return web.json_response({"ok": True})


async def login_code(request):
    data = await request.json()
    code = data.get("code")
    app = request.app
    phone = app.get("phone")
    code_hash = app.get("code_hash")
    if not client.is_connected():
        await client.connect()
    await client.sign_in(phone, code, phone_code_hash=code_hash)
    try:
        with open("session_string.txt", "w") as f:
            f.write(client.session.save())
    except Exception:
        pass
    asyncio.create_task(migration_task())
    return web.json_response({"ok": True})


async def main():
    app = web.Application()
    app["phone"] = None
    app["code_hash"] = None
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/login/phone", login_phone)
    app.router.add_post("/api/login/code", login_code)

    if session_string:
        try:
            await client.connect()
            if await client.is_user_authorized():
                asyncio.create_task(migration_task())
            else:
                status["state"] = "needs_login"
        except Exception as e:
            status["state"] = "needs_login"
            status["error"] = str(e)
    else:
        status["state"] = "needs_login"

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(getenv("PORT", default="8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # keep running
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
