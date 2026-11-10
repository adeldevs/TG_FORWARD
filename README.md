# Telegram Channel Message Forwarder

This script uses [Telethon](https://docs.telethon.dev/) to **forward messages from one Telegram channel to another**, while keeping track of the last processed message.
It supports **resuming from the last message**, handles **FloodWait errors**, and logs all activities.

---

## 🚀 Features

* Forward both **text and media** messages.
* Save the **last forwarded message ID** in a state file.
* **Resume** from where it left off.
* Handles **FloodWait** automatically.
* Logging with timestamps.

---

## 📦 Requirements

* Python 3.10+
* [Telegram API](https://my.telegram.org/auth)
* [Telethon](https://pypi.org/project/Telethon/)
* [python-decouple](https://pypi.org/project/python-decouple/)

---

## ⚙️ Setup with Virtual Environment (venv)

1. **Clone this repository** (or copy the script to your project folder).

   ```bash
   git clone https://github.com/MehediMK/telegram_messages_forwarding_bot.git
   cd telegram_messages_forwarding_bot
   ```

2. **Create a virtual environment**:

   ```bash
   python -m venv venv
   ```

3. **Activate the virtual environment**:

   * On **Linux / macOS**:

     ```bash
     source venv/bin/activate
     ```
   * On **Windows (PowerShell)**:

     ```powershell
     venv\Scripts\Activate
     ```

4. **Install dependencies** inside the venv:

   ```bash
   pip install -r requirements.txt
   ```

---

## ⚙️ Configuration

Create a `.env` file in the project root with the following variables:

```ini
# Telegram API credentials
API_ID=your_api_id
API_HASH=your_api_hash

# Channels (use @username or channel ID)
SOURCE_CHANNEL=@source_channel
TARGET_CHANNEL=@target_channel

# Telethon session name
SESSION_NAME=forwarder_session

# State + log files
STATE_FILE=state.json
LOG_FILE=forwarder.log
```

---

## ▶️ Usage

Activate the virtual environment first, then run the script:

```bash
source venv/bin/activate   # (Linux/macOS)
venv\Scripts\Activate      # (Windows PowerShell)

python forwarder.py
```

* First, it will **copy all old messages** from the source to the target channel.
* Then, it will **keep listening** for new messages and forward them automatically.

---

## 📁 Files

* `forwarder.py` → Main script
* `.env` → Environment configuration
* `state.json` → Stores the last processed message ID
* `forwarder.log` → Logs of forwarded messages

---

## 📝 Example Log Output

```
2025-08-20 10:15:02 - INFO - Forwarded text message ID 12345
2025-08-20 10:16:05 - INFO - Forwarded media message ID 12346
2025-08-20 10:17:10 - WARNING - Flood wait triggered (30 sec). Sleeping...
```

---

## ⚠️ Notes

* Your bot account (or user session) **must be a member/admin** in both source and target channels.
* If the script is interrupted, it will **resume from the last message** using `state.json`.
* Avoid lowering the delay (`await asyncio.sleep(2)`) too much, otherwise Telegram may block your account for spam.

---

## 📜 License

MIT License – free to use and modify.
