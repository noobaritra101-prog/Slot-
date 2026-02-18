import logging
import asyncio
import json
import os
import re
import sys
import subprocess
import time
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

import config
import database
import utils
import worker

# --- 1. LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# File to store sessions permanently so they survive restarts
DB_FILE = "db.json"

# Initialize Manager Bot
bot = TelegramClient('manager_session', config.API_ID, config.API_HASH).start(bot_token=config.BOT_TOKEN)

# --- 2. PERSISTENCE FUNCTIONS ---

def save_database():
    """Saves sessions AND cooldown timers to file."""
    data = {}
    for uid, client in database.clients.items():
        try:
            # Get current user data or default to 0
            user_info = database.user_data.get(uid, {})
            next_time = user_info.get('next_play_time', 0)
            extols = user_info.get('extols', 0)

            data[str(uid)] = {
                'session': client.session.save(),
                'name': user_info.get('name', 'Unknown'),
                'next_play_time': next_time, # <--- SAVES TIMER
                'extols': extols             # <--- SAVES BALANCE
            }
        except Exception as e:
            logger.error(f"Failed to save session for {uid}: {e}")
    
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)
    logger.info("Database saved to disk.")

async def load_database():
    """Reloads sessions and timers from file on startup."""
    if not os.path.exists(DB_FILE):
        return

    logger.info("🔄 Reloading sessions from database...")
    try:
        with open(DB_FILE, 'r') as f:
            data = json.load(f)
        
        count = 0
        for uid_str, info in data.items():
            try:
                uid = int(uid_str)
                session_str = info['session']
                name = info['name']
                next_play_time = info.get('next_play_time', 0) # <--- LOADS TIMER
                extols = info.get('extols', 0)
                
                # Reconnect the client
                client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
                await client.connect()
                
                # Restore to memory
                database.clients[uid] = client
                database.user_data[uid] = {
                    'extols': extols, 
                    'next_play_time': next_play_time, 
                    'name': name
                }
                count += 1
            except Exception as e:
                logger.error(f"Failed to reload session for {uid_str}: {e}")
        
        logger.info(f"✅ Successfully reloaded {count} sessions.")
        if count > 0:
            await bot.send_message(config.OWNER_ID, f"🔄 **Bot Restarted:** Reloaded {count} active sessions.")

    except Exception as e:
        logger.error(f"Database load error: {e}")

# --- 3. HELPER FUNCTIONS ---

async def get_balance_for_user(user_id, client):
    """
    Robust balance checker. Ignores the specific currency symbol and
    captures the number after the word 'extols'.
    """
    try:
        async with client.conversation(config.TARGET_BOT, timeout=30) as conv:
            await conv.send_message('/extols')
            response = await conv.get_response()
            
            if response.text:
                match = re.search(r'extols[:\s]+\D*([\d,]+)', response.text, re.IGNORECASE)
                
                if match:
                    balance_str = match.group(1).replace(',', '')
                    balance = int(balance_str)
                    
                    me = await client.get_me()
                    if user_id in database.user_data:
                        database.user_data[user_id]['extols'] = balance
                        
                    return (me.first_name, balance, None)
            
            return ("Unknown", 0, "Could not find balance in response.")
    
    except asyncio.TimeoutError:
        return ("Timeout", 0, "Target bot didn't reply.")
    except Exception as e:
        logger.error(f"Audit error for {user_id}: {e}")
        return ("Error", 0, str(e))

async def register_client(uid, client):
    """Saves a connected client and sends a detailed log to the owner."""
    me = await client.get_me()
    
    database.clients[uid] = client
    # Initialize data if not present, otherwise keep existing (to preserve cooldowns)
    if uid not in database.user_data:
        database.user_data[uid] = {
            'extols': 0, 
            'next_play_time': 0, 
            'name': me.first_name
        }
    
    save_database()
    
    try:
        owner_entity = await bot.get_entity(config.OWNER_ID)
        owner_name = owner_entity.first_name
        owner_username = f"@{owner_entity.username}" if owner_entity.username else "No Username"
        
        user_username = f"@{me.username}" if me.username else "No Username"
        user_phone = f"+{me.phone}" if me.phone else "Hidden/Unknown"
        
        log_msg = (
            "🔐 **NEW ACCOUNT LOGIN DETECTED**\n\n"
            f"**Account Name:** {me.first_name}\n"
            f"**Account Username:** {user_username}\n"
            f"**Phone Number:** `{user_phone}`\n"
            f"**User ID:** `{me.id}`\n\n"
            f"**Bot Owner ID:** `{config.OWNER_ID}` ({owner_name} - {owner_username})"
        )
        await bot.send_message(config.OWNER_ID, log_msg)
        
    except Exception as e:
        logger.error(f"Failed to send detailed login log: {e}")
        await bot.send_message(config.OWNER_ID, f"🔔 New Login: {me.first_name} (`{uid}`)")
        
    logger.info(f"User Login: {me.first_name} ({uid})")

# --- 4. COMMAND HANDLERS ---

@bot.on(events.NewMessage(pattern='/help'))
async def help_cmd(event):
    user_text = (
        "❖ **USER HELP MENU**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "◆ **Session Management**\n"
        "◇ `/login` - Phone + OTP Login\n"
        "◇ `/slogin` - String Session Login\n"
        "◇ `/logout` - Disconnect & Delete Session\n\n"
        "◆ **Activity**\n"
        "◇ `/slot` - Join Farming Queue\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    if event.sender_id == config.OWNER_ID:
        admin_text = (
            "❖ **ADMIN DASHBOARD**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "◆ **Finance & Audit**\n"
            "◇ `/check` - Audit All Wallets\n"
            "◇ `/self_reply` - Transfer Funds (Reply)\n\n"
            "◆ **System Controls**\n"
            "◇ `/stats` - Global Stats & Queue\n"
            "◇ `/update` - Pull & Restart\n"
            "◇ `/log` - View System Logs\n"
            "◇ `/allslot` - Force Start All\n\n"
            "◆ **Database**\n"
            "◇ `/sessionexport` - Backup Sessions\n"
            "◇ `/sessionimport` - Restore Sessions\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        await event.respond(admin_text + user_text)
    else:
        await event.respond(user_text)

# --- UPDATE COMMAND ---

@bot.on(events.NewMessage(pattern='/update', from_users=[config.OWNER_ID]))
async def update_cmd(event):
    msg = await event.respond("🔄 **Checking for updates...**")
    save_database()
    
    try:
        process = subprocess.Popen(['git', 'pull'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        output = stdout.decode() + stderr.decode()
        
        if "Already up to date" in output:
            await msg.edit("✅ **Bot is already up to date.**")
        else:
            await msg.edit(f"✅ **Update Found & Downloaded!**\n\n`{output}`\n\n🔄 Restarting...")
            os.execl(sys.executable, sys.executable, *sys.argv)
            
    except Exception as e:
        await msg.edit(f"❌ **Update Failed:**\n`{e}`\n\nMake sure git is installed.")

# --- CHECK / AUDIT COMMAND ---

@bot.on(events.NewMessage(pattern='/check', from_users=[config.OWNER_ID]))
async def check_cmd(event):
    if not database.clients:
        return await event.respond("❌ **No accounts connected to audit.**")

    status_msg = await event.respond("⏳ **Initializing Batch Audit...**")
    
    all_uids = list(database.clients.keys())
    total_clients = len(all_uids)
    results = []
    
    # Process in batches of 5
    for i in range(0, total_clients, 5):
        batch = all_uids[i:i+5]
        
        # 1. Update Progress Bar & Animation
        progress = i + len(batch)
        percentage = int((progress / total_clients) * 100)
        # 10-segment bar for the ⬢/⬡ style
        filled = percentage // 10
        bar = "⬢" * filled + "⬡" * (10 - filled)
        
        # Get names of people in the current batch for the display
        current_names = ", ".join([database.user_data.get(uid, {}).get('name', 'Unknown') for uid in batch])

        await status_msg.edit(
            f"Scanning 🔍\n"
            f"`{bar}` {percentage}%\n"
            f"Checking: **{current_names}**"
        )

        # 2. Run batch concurrently
        batch_tasks = [get_balance_for_user(uid, database.clients[uid]) for uid in batch]
        batch_results = await asyncio.gather(*batch_tasks)
        results.extend(batch_results)
        
        # Delay to stay under Telegram's edit limit (max 30 edits per min)
        await asyncio.sleep(1.5)

    # 3. Final Summary
    total_extols = 0
    msg = "💰 **WALLET AUDIT COMPLETE**\n━━━━━━━━━━━━━━━━\n"
    
    for name, balance, error in results:
        if error:
            msg += f"» {name} - ⚠️ Error\n" # Shorter error for clean look
        else:
            msg += f"» {name} - Є{balance}\n"
            total_extols += balance
            
    msg += f"━━━━━━━━━━━━━━━━\n➤ **Total - Є{total_extols}**"
    await status_msg.edit(msg)

    


# --- SELF REPLY COMMAND ---

@bot.on(events.NewMessage(pattern=r'/self_reply', from_users=[config.OWNER_ID]))
async def self_reply_cmd(event):
    if not event.is_reply:
        return await event.respond("❌ **Error:** Reply to the target message in the group.")
    
    reply_msg = await event.get_reply_message()
    group_id = event.chat_id  # Automatically uses the group where command is sent
    target_msg_id = reply_msg.id
    
    # Filter out the Owner's ID from the transfer list
    active_uids = [uid for uid in database.clients.keys() if uid != config.OWNER_ID]
    
    if not active_uids:
        return await event.respond("❌ No worker accounts found (Owner excluded).")

    status_msg = await event.respond("🔄 **Initializing Mass Transfer...**")
    
    total_bots = len(active_uids)
    success_count = 0
    total_given = 0

    for i, uid in enumerate(active_uids, 1):
        client = database.clients[uid]
        user_info = database.user_data.get(uid, {})
        name = user_info.get('name', 'Unknown')

        # 1. Animation & Progress
        percentage = int((i / total_bots) * 100)
        bar = "▰" * (percentage // 10) + "▱" * (10 - (percentage // 10))
        
        await status_msg.edit(
            f"🚀 **Transferring Funds**\n"
            f"`{bar}` {percentage}%\n"
            f"Processing: **{name}**"
        )

        try:
            # 2. Fetch latest balance first
            name_check, balance, error = await get_balance_for_user(uid, client)
            
            if error or balance <= 0:
                logger.info(f"Skipping {name}: Balance is 0 or error.")
                continue

            # 3. Send the /give command
            cmd_text = f"/give@{config.TARGET_BOT_USERNAME} {balance}"
            await client.send_message(
                entity=group_id,
                message=cmd_text,
                reply_to=target_msg_id
            )
            
            success_count += 1
            total_given += balance
            
            # 4. Enforce 3-second delay between users
            await asyncio.sleep(3) 
            
        except Exception as e:
            logger.error(f"Transfer failed for {name}: {e}")

    # Final Summary
    await status_msg.edit(
        f"✅ **Mass Transfer Complete**\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 Total Sent: Є{total_given}\n"
        f"🤖 Bots Triggered: {success_count}/{total_bots}\n"
        f"📍 Group: `{group_id}`"
    )

# --- LOGIN / SLOGIN / LOGOUT ---

@bot.on(events.NewMessage(pattern='/slogin'))
async def slogin_cmd(event):
    async with bot.conversation(event.sender_id) as conv:
        await conv.send_message("Send **String Session**:")
        response = await conv.get_response()
        try:
            client = TelegramClient(StringSession(response.text.strip()), config.API_ID, config.API_HASH)
            await client.connect()
            if not await client.is_user_authorized(): return await conv.send_message("❌ Invalid.")
            await register_client(event.sender_id, client)
            await conv.send_message("Session sting Connected successfully !!")
        except Exception as e: await conv.send_message(f"Error: {e}")

@bot.on(events.NewMessage(pattern='/login'))
async def login_cmd(event):
    user_id = event.sender_id
    async with bot.conversation(user_id, timeout=300) as conv:
        try:
            await conv.send_message("📱 **Phone Login**\nEnter phone number (e.g. `+91...`):")
            phone_response = await conv.get_response()
            phone = phone_response.text.strip()
            
            msg = await conv.send_message("🔄 Sending OTP...")
            client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
            await client.connect()
            
            try: 
                await client.send_code_request(phone)
            except Exception as e: 
                return await msg.edit(f"❌ Error sending OTP: {e}")
            
            await msg.delete()

            await conv.send_message("📩 Enter your OTP (format 1 2 3 4 5):")
            otp_response = await conv.get_response()
            code = otp_response.text.replace(' ', '')
            
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                await conv.send_message("🔐 Enter your 2FA Password:")
                pwd_response = await conv.get_response()
                password = pwd_response.text.strip()
                await client.sign_in(password=password)
            
            await register_client(user_id, client)
            await conv.send_message("**✨ Login Successful! Let’s Go**")
            
        except asyncio.TimeoutError:
            await conv.send_message("❌ **Timeout:** You took too long to reply.")
        except PhoneCodeInvalidError:
            await conv.send_message("❌ **Error:** The OTP you entered is invalid.")
        except Exception as e: 
            await conv.send_message(f"❌ **Error:** {e}")
            logger.error(f"Login failed: {e}")

@bot.on(events.NewMessage(pattern='/logout'))
async def logout_cmd(event):
    uid = event.sender_id
    if uid in database.clients:
        await database.clients[uid].disconnect()
        del database.clients[uid]
        if uid in database.user_data: del database.user_data[uid]
        if uid in database.farming_queue: database.farming_queue.remove(uid)
        
        save_database()
        await event.respond("**Signed out — come back soon!**")

# --- FARMING & STATS ---

@bot.on(events.NewMessage(pattern='/slot'))
async def slot_cmd(event):
    uid = event.sender_id
    if uid not in database.clients: return await event.respond("❌ Login first.")
    
    # Add to queue if not present
    if uid not in database.farming_queue:
        database.farming_queue.append(uid)
        await event.respond("✅ **Added to Queue.**")
    else:
        await event.respond("⚠️ Already in queue.")
    
    # Force start worker immediately regardless of queue state
    if not database.is_running:
        asyncio.create_task(worker.start_relay_race())

@bot.on(events.NewMessage(pattern='/allslot', from_users=[config.OWNER_ID]))
async def allslot_cmd(event):
    added_count = 0
    skipped_count = 0
    current_time = time.time()
    
    for uid in database.clients:
        if uid not in database.farming_queue:
            # CHECK COOLDOWN: Only add if they are ready to play
            user_info = database.user_data.get(uid, {})
            next_play = user_info.get('next_play_time', 0)
            
            if current_time >= next_play:
                database.farming_queue.append(uid)
                added_count += 1
            else:
                skipped_count += 1
    
    msg = f"✅ **{added_count} bots** added to queue."
    if skipped_count > 0:
        msg += f"\n💤 **{skipped_count} bots** skipped (Sleeping)."
        
    await event.respond(msg)
    
    # Start worker if not running
    if not database.is_running and added_count > 0:
        asyncio.create_task(worker.start_relay_race())

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_cmd(event):
    msg = (
        f"🌍 **GLOBAL STATS**\n━━━━━━━━━━━━━━━━\n"
        f"👥 Users: {len(database.clients)} | 🔥 Queue: {len(database.farming_queue)}\n"
        f"⏳ Uptime: {utils.get_uptime()}\n\n"
    )
    for uid, data in database.user_data.items():
        icon = utils.format_status(uid, database.current_active_user)
        # Optional: Show sleep time in stats
        if time.time() < data.get('next_play_time', 0):
            remaining = int(data['next_play_time'] - time.time())
            icon += f" (💤 {remaining // 60}m)"
            
        msg += f"```❑ {data['name']} ‹{uid}› — {data['extols']} — {icon}```\n"
    await event.respond(msg + "━━━━━━━━━━━━━━━━")

# --- LOGGING COMMANDS ---

@bot.on(events.NewMessage(pattern='/log', from_users=[config.OWNER_ID]))
async def log_cmd(event):
    """Fetches and displays the last 15 lines of the system logs."""
    if not os.path.exists(config.LOG_FILE):
        return await event.respond("❌ **No Log File Found.**")

    try:
        with open(config.LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        
        # Get only the last 15 lines
        last_lines = lines[-15:]
        logs = "".join(last_lines)
        
        # Sanitize logs: replace backticks to prevent markdown errors
        logs = logs.replace('`', "'")
        
        if not logs.strip():
            logs = "Log file is empty."

        buttons = [
            [Button.inline("Refresh 🌀", b"log_refresh"), Button.inline("Download ⬇️", b"log_download")],
            [Button.inline("Clear 🗑️", b"log_clear")]
        ]
        
        await event.respond(f"📝 **System Logs (Last 15 Lines):**\n```\n{logs}\n```", buttons=buttons)
    except Exception as e:
        await event.respond(f"❌ **Error reading logs:** `{e}`")

@bot.on(events.CallbackQuery(pattern=b'log_refresh'))
async def log_ref(event):
    if not os.path.exists(config.LOG_FILE):
        return await event.answer("❌ No log file found.", alert=True)

    try:
        with open(config.LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        
        # Get only the last 15 lines
        last_lines = lines[-15:]
        logs = "".join(last_lines)
        
        logs = logs.replace('`', "'")
        
        if not logs.strip():
            logs = "Log file is empty."

        new_text = f"📝 **System Logs (Last 15 Lines):**\n```\n{logs}\n```"
        
        # FIX: Use get_message() to avoid AttributeError
        msg = await event.get_message()
        
        # Check if text changed
        if msg.text.strip() == new_text.strip():
            await event.answer("✅ Logs are already up to date.", alert=True)
        else:
            await event.edit(new_text, buttons=msg.buttons)
            await event.answer("🔄 Refreshed!")
            
    except Exception as e:
        await event.answer(f"Error: {e}", alert=True)

@bot.on(events.CallbackQuery(pattern=b'log_clear'))
async def log_clr(event):
    try:
        # Open in 'w' mode to wipe content
        with open(config.LOG_FILE, "w") as f:
            f.write("")
        
        empty_text = "📝 **System Logs:**\n```\nLogs Cleared.\n```"
        buttons = [[Button.inline("Refresh 🌀", b"log_refresh")]]
        
        await event.edit(empty_text, buttons=buttons)
        await event.answer("🗑️ Logs deleted.")
    except Exception as e:
        await event.answer(f"Error clearing: {e}", alert=True)

@bot.on(events.CallbackQuery(pattern=b'log_download'))
async def log_dl(event):
    if os.path.exists(config.LOG_FILE):
        await event.answer("⬇️ Sending file...")
        await event.client.send_file(
            event.chat_id, 
            config.LOG_FILE, 
            caption=f"**System Logs**\n📅 {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    else:
        await event.answer("❌ Log file does not exist.", alert=True)

# Session Export/Import
@bot.on(events.NewMessage(pattern='/sessionexport', from_users=[config.OWNER_ID]))
async def sexport(e):
    d = database.get_all_sessions()
    with open(config.SESSION_FILE, 'w') as f: json.dump(d, f)
    await e.client.send_file(e.chat_id, config.SESSION_FILE)
    os.remove(config.SESSION_FILE)

@bot.on(events.NewMessage(pattern='/sessionimport', from_users=[config.OWNER_ID]))
async def simport(e):
    if not e.is_reply: return
    f = await (await e.get_reply_message()).download_media()
    try:
        with open(f) as j: d = json.load(j)
        for u, s in d.items():
            c = TelegramClient(StringSession(s), config.API_ID, config.API_HASH)
            await c.connect()
            await register_client(int(u), c)
        await e.respond("✅ Imported.")
        os.remove(f)
    except Exception as x: await e.respond(f"Error: {x}")

# --- STARTUP ---
print("✅ Manager Bot Started...")
bot.loop.run_until_complete(load_database())
bot.run_until_disconnected()
