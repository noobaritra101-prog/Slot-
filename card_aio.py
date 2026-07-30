import logging
import random
import asyncio
import signal
import time
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ChatType, ParseMode

# Configure standard logging to capture startup and runtime errors
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("AnimeNexus")

# Import configuration, state databases, and engine instances
import config
from config import (
    bot, dp, main_router, check_autoleave, is_ghost_banned, check_spam,
    is_shadow_banned, ensure_group, periodic_save, backup_to_group,
    load_from_group, load_settings, _flush_db
)

# Import handlers to register them on the router
import handlers
import a_handlers
import vlog
import store 
import market 
import mines
import gcard

# Import the Aviator server startup task
from aviator import start_aviator_server

from handlers import trigger_drop
from market import market_engine_loop
from versus import active_versus  # already registers handlers via main_router
from vlog import vlog_cleanup_loop

# ==========================================
# BOT ADDED TO GROUP — DB LOG
# ==========================================
from aiogram.types import ChatMemberUpdated
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION

@dp.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added_to_group(event: ChatMemberUpdated):
    """Fires whenever the bot itself is added to (or promoted in) a chat."""
    chat = event.chat
    added_by = event.from_user

    # Only log actual groups/supergroups
    from aiogram.enums import ChatType as _CT
    if chat.type not in [_CT.GROUP, _CT.SUPERGROUP]:
        return

    # Register in DB
    config.ensure_group(chat.id, chat.title or str(chat.id))

    # DM the person who added the bot
    if added_by:
        try:
            await bot.send_message(
                chat_id=added_by.id,
                text=(
                    f"🌸 Thanks for adding me to <b>{chat.title}</b> (<code>{chat.id}</code>)!\n\n"
                    f"Keep supporting 🤍"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"[DM] Could not message adder {added_by.id}: {e}")

    # Send log to DB group
    try:
        added_mention = (
            f'<a href="tg://user?id={added_by.id}">'
            f'{str(added_by.first_name).replace("<","&lt;").replace(">","&gt;")}</a>'
            if added_by else "Unknown"
        )
        from datetime import datetime, timezone as _tz
        await bot.send_message(
            chat_id=config.DATABASE_BACKUP_ID,
            text=(
                f"<b>「 ➕ BOT ADDED TO GROUP 」</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• 🏘️ <b>Group:</b> {chat.title} (<code>{chat.id}</code>)\n"
                f"• 👤 <b>Added By:</b> {added_mention} (<code>{added_by.id if added_by else '?'}</code>)\n"
                f"• 🕐 <b>Time:</b> {datetime.now(_tz.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"[LOG] bot_added_to_group log failed: {e}")

# ==========================================
# AIOGRAM HANDLER & CONTROL MIDDLEWARE
# ==========================================

# Per-user callback cooldown — prevents button mashing before board updates
# { uid: last_callback_timestamp }
_cb_cooldown: dict[int, float] = {}
CB_COOLDOWN_SEC = 1.2  # seconds between allowed taps (tune as needed)

class GlobalGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data: dict):
        is_msg = isinstance(event, Message)
        is_callback = isinstance(event, CallbackQuery)
        
        if not is_msg and not is_callback:
            return await handler(event, data)
            
        user = event.from_user
        uid = user.id if user else None
        if not uid: 
            return await handler(event, data)

        # Increment global message log counter (Messages only)
        if is_msg:
            config.total_messages += 1
            if event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
                if await check_autoleave(event.chat.id): 
                    return

        # ADMIN IMMUNITY: Skip all ban/spam filtering blocks
        if uid not in config.ADMIN_IDS:
            
            # Hard restrict: globally (ghost) banned users
            if is_ghost_banned(uid):
                # Allow /profile command to bypass ghost ban (so users can check their status)
                if is_msg and event.text and event.text.startswith("/profile"):
                    pass
                else:
                    return
            
            # Anti-Spam throttle execution (Catches BOTH text and buttons)
            if check_spam(uid):
                if is_msg:
                    try: 
                        safe_name = str(user.first_name).replace("<", "&lt;").replace(">", "&gt;")
                        await event.reply(
                            f"⚠️ <b><a href='tg://user?id={uid}'>{safe_name}</a></b>, you have been shadow-banned for spamming.\n"
                            f"🔇 You are muted for 10 minutes.", 
                            parse_mode=ParseMode.HTML
                        )
                    except Exception: 
                        pass
                elif is_callback:
                    try:
                        await event.answer("⚠️ You have been shadow-banned for 10 minutes due to button spamming!", show_alert=True)
                    except Exception: 
                        pass
                return

            # Shadow ban: block user dynamically
            if is_shadow_banned(uid):
                # Allow /profile command to bypass shadow ban
                if is_msg and event.text and event.text.startswith("/profile"):
                    pass 
                else:
                    if is_callback:
                        try: 
                            await event.answer("🔇 You are currently shadow-banned. Please wait.", show_alert=True)
                        except Exception: 
                            pass
                    return

            # Per-button cooldown: drop rapid repeat taps before the board has updated.
            # Versus callbacks are exempt — they use their own processing flag internally.
            if is_callback and not event.data.startswith("vs_"):
                now = time.time()
                last = _cb_cooldown.get(uid, 0.0)
                if now - last < CB_COOLDOWN_SEC:
                    try:
                        await event.answer("⏳ Slow down a little!", show_alert=False)
                    except Exception:
                        pass
                    return
                _cb_cooldown[uid] = now

        # ==========================================
        # CARD DROP SPAWNER ENGINE (FOR GROUPS - Messages Only)
        # ==========================================
        if is_msg and event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            chat_id = str(event.chat.id)
            ensure_group(chat_id, event.chat.title)
            
            # Read spawn boundaries from database
            db_ref = config.load_db()
            s_min = db_ref["groups"].get(chat_id, {}).get("spawn_min", 100)
            s_max = db_ref["groups"].get(chat_id, {}).get("spawn_max", 110)

            # Passively record chat membership — powers the /leaderboard
            # "This Chat" scope without needing per-command Telegram API calls.
            members = db_ref["groups"][chat_id].setdefault("members", {})
            if str(uid) not in members:
                members[str(uid)] = True
                config.save_db()
            
            # Spawn logic counter increment
            config.group_counters.setdefault(chat_id, {"count": 0, "target": random.randint(s_min, s_max)})
            config.group_counters[chat_id]["count"] += 1
            if config.group_counters[chat_id]["count"] >= config.group_counters[chat_id]["target"]:
                config.group_counters[chat_id] = {"count": 0, "target": random.randint(s_min, s_max)}
                asyncio.create_task(trigger_drop(event.chat.id))

        return await handler(event, data)

# ==========================================
# MAIN EXECUTION ENTRY POINT
# ==========================================
async def main():
    logger.info("Initializing system settings...")

    # Restore database from pinned backup BEFORE anything reads it.
    logger.info("Verifying cloud database backup integrity...")
    await load_from_group()

    load_settings()
    
    # Setup middlewares for BOTH Messages and Callbacks
    dp.message.outer_middleware(GlobalGuardMiddleware())
    dp.callback_query.outer_middleware(GlobalGuardMiddleware())
    
    # Attach unified main routers
    dp.include_router(main_router)
    
    try:
        # Initiate scheduled background microtasks
        logger.info("Starting background persistence cycles...")
        asyncio.create_task(periodic_save())
        asyncio.create_task(backup_to_group())
        
        logger.info("Launching stock market exchange loop...")
        asyncio.create_task(market_engine_loop())

        # Start the /vlog activity-log auto-purge loop (7-day retention)
        logger.info("Starting vault-log auto-purge cycle...")
        asyncio.create_task(vlog_cleanup_loop())

        # Start the Aviator HTTP betting server and engine
        logger.info("Launching Aviator betting server & engine...")
        asyncio.create_task(start_aviator_server())

        # SIGTERM management logic
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(dp.stop_polling()))
            except NotImplementedError:
                pass
        
        logger.info("Anime Nexus is running over high speed aiogram v3 engines...")
        
        # Drop pending update queues to avoid start-up spam bursts
        await bot.delete_webhook(drop_pending_updates=True) 
        
        # Start bot polling loop
        logger.info("Establishing connection with Telegram API...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"Critical error during main event loop: {e}", exc_info=True)
    finally:
        logger.info("Closing active connection sessions...")
        try:
            logger.info("Flushing database to disk before shutdown...")
            await asyncio.to_thread(_flush_db, force=True)
            if vlog._vlogs_dirty:
                await asyncio.to_thread(vlog._flush_vlogs, force=True)
        except Exception as e:
            logger.critical(f"Failed to flush database on shutdown: {e}", exc_info=True)
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot application terminated by administrator.")
