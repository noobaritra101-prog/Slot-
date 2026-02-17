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

DB_FILE = "db.json"

# Initialize Manager Bot
bot = TelegramClient('manager_session', config.API_ID, config.API_HASH).start(bot_token=config.BOT_TOKEN)

# --- 2. PERSISTENCE FUNCTIONS ---

def save_database():
    """Saves sessions AND cooldown timers to file."""
    data = {}
    for uid, client in database.clients.items():
        try:
            user_info = database.user_data.get(uid, {})
            next_time = user_info.get('next_play_time', 0)
            extols = user_info.get('extols', 0)

            data[str(uid)] = {
                'session': client.session.save(),
                'name': user_info.get('name', 'Unknown'),
                'next_play_time': next_time,
                'extols': extols
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
                next_play_time = info.get('next_play_time', 0)
                extols = info.get('extols', 0)
                
                client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
                await client.connect()
                
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
    me = await client.get_me()
    database.clients[uid] = client
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
            "❖ **ADMIN DASHBOARD** <tg-emoji emoji-id='6330146053244853999'>🪲</tg-emoji>\n"
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
        await event.respond(admin_text + user_text, parse_mode='html')
    else:
        await event.respond(user_text)

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
        await msg.edit(f"❌ **Update Failed:**\n`{e}`")

@bot.on(events.NewMessage(pattern='/check', from_users=[config.OWNER_ID]))
async def check_cmd(event):
    status_msg = await event.respond("⏳ **Auditing Wallets...**\nChecking balances...")
    tasks = [get_balance_for_user(uid, client) for uid, client in database.clients.items()]
    results = await asyncio.gather(*tasks)
    
    total_extols = 0
    msg = "💰 **WALLET AUDIT** <tg-emoji emoji-id='6330094878709520814'>☣️</tg-emoji>\n━━━━━━━━━━━━━━━━\n"
    for name, balance, error in results:
        if error:
            msg += f"» {name} - ⚠️ {error}\n"
        else:
            msg += f"» {name} - Є{balance}\n"
            total_extols += balance
    msg += f"━━━━━━━━━━━━━━━━\n➤ **Total - Є{total_extols}**"
    await status_msg.edit(msg, parse_mode='html')

@bot.on(events.NewMessage(pattern=r'/self_reply (all|(?:\d+)) (-?\d+) (\d+)', from_users=[config.OWNER_ID]))
async def self_reply_cmd(event):
    if not event.is_reply:
        return await event.respond("❌ **Error:** Reply to a message.")
    
    target_mode = event.pattern_match.group(1) 
    group_id = int(event.pattern_match.group(2))
    amount = int(event.pattern_match.group(3))
    reply_msg = await event.get_reply_message()
    
    active_clients = list(database.clients.values()) if target_mode == 'all' else [database.clients[int(target_mode)]] if int(target_mode) in database.clients else []
    
    if not active_clients:
        return await event.respond("❌ No clients found.")

    await event.respond(f"💸 **Sending Funds...**\nTarget: `{group_id}` | Amount: Є{amount}")
    count = 0
    for client in active_clients:
        try:
            await client.send_message(entity=group_id, message=f"/give@{config.TARGET_BOT_USERNAME} {amount}", reply_to=reply_msg.id)
            count += 1
            await asyncio.sleep(2) 
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
    await event.respond(f"✅ **Done.** Triggered {count} bots.")

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
            await conv.send_message("Session string Connected successfully !!")
        except Exception as e: await conv.send_message(f"Error: {e}")

@bot.on(events.NewMessage(pattern='/login'))
async def login_cmd(event):
    user_id = event.sender_id
    async with bot.conversation(user_id, timeout=300) as conv:
        try:
            await conv.send_message("📱 **Phone Login**\nEnter phone number:")
            phone = (await conv.get_response()).text.strip()
            client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
            await client.connect()
            await client.send_code_request(phone)
            await conv.send_message("📩 Enter OTP (format 1 2 3 4 5):")
            code = (await conv.get_response()).text.replace(' ', '')
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                await conv.send_message("🔐 Enter 2FA Password:")
                await client.sign_in(password=(await conv.get_response()).text.strip())
            await register_client(user_id, client)
            await conv.send_message("**✨ Login Successful!**")
        except Exception as e: await conv.send_message(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern='/logout'))
async def logout_cmd(event):
    uid = event.sender_id
    if uid in database.clients:
        await database.clients[uid].disconnect()
        del database.clients[uid]
        if uid in database.user_data: del database.user_data[uid]
        save_database()
        await event.respond("**Signed out!**")

@bot.on(events.NewMessage(pattern='/slot'))
async def slot_cmd(event):
    uid = event.sender_id
    if uid not in database.clients: return await event.respond("❌ Login first.")
    if uid not in database.farming_queue:
        database.farming_queue.append(uid)
        await event.respond("✅ **Added to Queue.**")
    else:
        await event.respond("⚠️ Already in queue.")
    if not database.is_running:
        asyncio.create_task(worker.start_relay_race())

@bot.on(events.NewMessage(pattern='/allslot', from_users=[config.OWNER_ID]))
async def allslot_cmd(event):
    added_count = 0
    current_time = time.time()
    for uid in database.clients:
        if uid not in database.farming_queue:
            if current_time >= database.user_data.get(uid, {}).get('next_play_time', 0):
                database.farming_queue.append(uid)
                added_count += 1
    await event.respond(f"✅ **{added_count} bots** added to queue.")
    if not database.is_running and added_count > 0:
        asyncio.create_task(worker.start_relay_race())

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_cmd(event):
    msg = (
        f"🌍 **GLOBAL STATS**\n━━━━━━━━━━━━━━━━\n"
        f"👥 Users: {len(database.clients)} | <tg-emoji emoji-id='6329851762085731491'>🔥</tg-emoji> Queue: {len(database.farming_queue)}\n"
        f"⏳ Uptime: {utils.get_uptime()}\n\n"
    )
    for uid, data in database.user_data.items():
        icon = utils.format_status(uid, database.current_active_user)
        if time.time() < data.get('next_play_time', 0):
            icon += f" (💤 {int(data['next_play_time'] - time.time()) // 60}m)"
        msg += f"```❑ {data['name']} ‹{uid}› — {data['extols']} — {icon}```\n"
    await event.respond(msg + "━━━━━━━━━━━━━━━━", parse_mode='html')

# (Log commands omitted for brevity, but remain same as your original provided code)

if __name__ == "__main__":
    bot.loop.run_until_complete(load_database())
    logger.info("Bot is running...")
    bot.run_until_disconnected()
