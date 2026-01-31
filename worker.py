# worker.py
import asyncio
import time
import logging
import re
import config
import database
import utils

logger = logging.getLogger(__name__)

async def watchdog_task():
    """
    Background task that checks if a user is stuck for > 5 minutes.
    """
    logger.info("🐕 Watchdog started.")
    while True:
        try:
            # Check every 60 seconds
            await asyncio.sleep(60)

            # Only check if the system is running and someone is active
            if database.is_running and database.current_active_user and database.active_user_start_time:
                
                elapsed = time.time() - database.active_user_start_time
                
                # --- TRIGGER: User active for > 5 Minutes (300 seconds) ---
                if elapsed > 300: 
                    uid = database.current_active_user
                    client = database.clients.get(uid)
                    
                    if not client: continue

                    logger.warning(f"🐕 Watchdog Alert: User {uid} stuck for > 5 mins. Taking action...")

                    # 1. Force Send /slot to check status
                    try:
                        await client.send_message(config.TARGET_BOT, '/slot')
                        await asyncio.sleep(5) # Wait for reply to arrive in history
                        
                        # 2. Read the latest message
                        history = await client.get_messages(config.TARGET_BOT, limit=1)
                        if history:
                            text = history[0].text
                            me = await client.get_me()
                            
                            # 3. DECIDE ACTION BASED ON TEXT
                            if "Remaining Slot Usage: 0" in text:
                                sleep_time = (12 * 3600) + (10 * 60)
                                database.user_data[uid]['next_play_time'] = time.time() + sleep_time
                                logger.info(f"🐕 Watchdog: {me.first_name} finished (0 Slots). Sleeping 12h 10m.")
                                
                            elif "play again in" in text:
                                # Simple parse (fallback to 30m if regex fails)
                                database.user_data[uid]['next_play_time'] = time.time() + 1800 
                                logger.info(f"🐕 Watchdog: {me.first_name} is on cooldown. Sleeping 30m.")
                                
                            else:
                                # Unknown state, just skip them for 5 mins to unblock queue
                                database.user_data[uid]['next_play_time'] = time.time() + 300
                                logger.info(f"🐕 Watchdog: {me.first_name} status unclear. Skipping for 5 mins.")

                    except Exception as e:
                        logger.error(f"🐕 Watchdog Error: {e}")
                        # If we can't send/read, force a skip anyway
                        database.user_data[uid]['next_play_time'] = time.time() + 300

                    # 4. KILL THE STUCK LOOP
                    database.force_abort_flag = True
        
        except Exception as e:
            logger.error(f"🐕 Watchdog Crash: {e}")
            await asyncio.sleep(10)

async def play_user_turn(user_id):
    """
    Plays slots for ONE user. Now includes Abort Flag check.
    """
    client = database.clients.get(user_id)
    if not client: return

    if time.time() < database.user_data[user_id]['next_play_time']:
        return

    me = await client.get_me()
    logger.info(f"[START] Turn started for User {me.first_name} ({user_id})")
    
    # --- WATCHDOG SETUP ---
    database.active_user_start_time = time.time() # Start Clock
    database.force_abort_flag = False             # Reset Flag

    try:
        while True:
            # --- CHECK ABORT FLAG (From Watchdog) ---
            if database.force_abort_flag:
                logger.warning(f"🛑 Aborting {me.first_name} due to Watchdog trigger.")
                break

            async with client.conversation(config.TARGET_BOT, timeout=15) as conv:
                await conv.send_message('/slot')
                response = await conv.get_response()
                text = response.text

                won = utils.parse_extols(text)
                database.user_data[user_id]['extols'] += won

                # Normal Stop Conditions
                if "Remaining Slot Usage: 0" in text:
                    sleep_duration = (12 * 3600) + (10 * 60)
                    database.user_data[user_id]['next_play_time'] = time.time() + sleep_duration
                    logger.info(f"🛑 {me.first_name}: Slots Empty. Sleeping 12h 10m.")
                    break
                
                elif "play again in" in text:
                    h_match = re.search(r'(\d+)\s*h', text, re.IGNORECASE)
                    m_match = re.search(r'(\d+)\s*m', text, re.IGNORECASE)
                    s_match = re.search(r'(\d+)\s*s', text, re.IGNORECASE)

                    h = int(h_match.group(1)) if h_match else 0
                    m = int(m_match.group(1)) if m_match else 0
                    s = int(s_match.group(1)) if s_match else 0
                    
                    total_seconds = (h * 3600) + (m * 60) + s
                    if total_seconds == 0: total_seconds = 1800 # Fallback

                    database.user_data[user_id]['next_play_time'] = time.time() + total_seconds + 30
                    logger.info(f"⏳ {me.first_name}: Cooldown detected. Sleeping {h}h {m}m {s}s.")
                    break

                logger.info(f"🎰 {me.first_name}: Spin successful. Won: {won}")
                await asyncio.sleep(4)

    except Exception as e:
        logger.error(f"[ERROR] User {user_id}: {e}")
        database.user_data[user_id]['next_play_time'] = time.time() + 60
    
    finally:
        # Reset Watchdog Timers when done
        database.active_user_start_time = 0
        database.force_abort_flag = False

async def start_relay_race():
    """
    Main Supervisor Loop. Now also starts the Watchdog.
    """
    if database.is_running:
        return
        
    database.is_running = True
    logger.info("🏁 Relay Race Background Task Started!")

    # --- START THE WATCHDOG ---
    asyncio.create_task(watchdog_task())

    try:
        while True:
            if not database.farming_queue:
                logger.info("Queue is empty. Worker stopping.")
                break

            active_run_found = False
            current_time = time.time()
            
            for user_id in list(database.farming_queue):
                if user_id not in database.user_data: continue

                if current_time >= database.user_data[user_id]['next_play_time']:
                    
                    database.current_active_user = user_id 
                    await play_user_turn(user_id)
                    database.current_active_user = None
                    active_run_found = True
            
            if not active_run_found:
                await asyncio.sleep(30)
            else:
                await asyncio.sleep(5)

    except Exception as e:
        logger.error(f"❌ Worker Crashed: {e}")
    finally:
        database.is_running = False
        logger.info("🛑 Worker Stopped.")
