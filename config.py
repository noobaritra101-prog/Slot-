# config.py
import os

# --- YOU MUST FILL THESE IN ---
# Get API_ID and API_HASH from https://my.telegram.org
API_ID = 1234567               # Replace with your API ID (Integer)
API_HASH = "YOUR_API_HASH"     # Replace with your API Hash (String)

# Get this from @BotFather
BOT_TOKEN = "YOUR_BOT_TOKEN"   # Replace with your Bot Token

# Your Numeric Telegram ID (Use @userinfobot to find it)
OWNER_ID = 123456789           # Replace with your Admin ID

# --- TARGETS ---
TARGET_BOT = "@roronoa_zoro_robot"
# Numeric ID is safer if username changes, but username works for now
TARGET_BOT_USERNAME = "roronoa_zoro_robot" 

# --- FILES ---
LOG_FILE = "bot_logs.txt"
SESSION_FILE = "sessions.json" # For backup/restore
