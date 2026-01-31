# worker.py
import asyncio
import time
import logging
import re
import config
import database
import utils

logger = logging.getLogger(__name__)

async def play_user_turn(user_id):
    """
    Plays slots for ONE specific user until they hit a limit or cooldown.
    """
    client = database.clients.get(user_id)
    if not client: return

    # Double-check cooldown before starting (Safety check)
    if time.time() < database.user_data[user_id]['next_play_time']:
        return

    me = await client.get_me()
    logger.info(f"[START] Turn started for User {me.first_name} ({user_id})")
    
    try:
        while True:
            # Conversation with the Target Bot
            async with client.conversation(config.TARGET_BOT, timeout=15) as conv:
                await conv.send_message('/slot')
                response = await conv.get_response()
                text = response.text

                # 1. Update Internal Stats (Parsing Extols won)
                won = utils.parse_extols(text)
                database.user_data[user_id]['extols'] += won

                # --- STOP CONDITION 1: EMPTY SLOTS ---
                if "Remaining Slot Usage: 0" in text:
                    # User requested: 12 Hours + 10 Minutes = 43800 seconds
                    sleep_duration = (12 * 3600) + (10 * 60)
                    resume_time = time.time() + sleep_duration
                    
                    database.user_data[user_id]['next_play_time'] = resume_time
                    logger.info(f"🛑 {me.first_name}: Slots Empty. Sleeping 12h 10m (until {time.ctime(resume_time)}).")
                    break
                
                # --- STOP CONDITION 2: COOLDOWN MESSAGE ---
                elif "play again in" in text:
                    # DEBUG: Print exact text to see what is failing if it happens again
                    # logger.info(f"DEBUG TEXT: {text}") 

                    # Independent Regex Checks (Robust against bolding/formatting)
                    # Looks for digits followed specifically by 'h', 'm', or 's'
                    h_match = re.search(r'(\d+)\s*h', text, re.IGNORECASE)
                    m_match = re.search(r'(\d+)\s*m', text, re.IGNORECASE)
                    s_match = re.search(r'(\d+)\s*s', text, re.IGNORECASE)

                    h = int(h_match.group(1)) if h_match else 0
                    m = int(m_match.group(1)) if m_match else 0
                    s = int(s_match.group(1)) if s_match else 0
                    
                    total_seconds = (h * 3600) + (m * 60) + s
                    
                    if total_seconds == 0:
                        # Fallback: If regex fails entirely but message exists, default to 30m
                        logger.warning(f"⚠️ {me.first_name}: Parse failed (Got 0s). Defaulting to 30 mins.")
                        total_seconds = 1800

                    # Add 30s buffer to be safe
                    resume_time = time.time() + total_seconds + 30
                    
                    database.user_data[user_id]['next_play_time'] = resume_time
                    logger.info(f"⏳ {me.first_name}: Cooldown detected. Sleeping {h}h {m}m {s}s.")
                    break

                # Log successful spin
                logger.info(f"🎰 {me.first_name}: Spin successful. Won: {won}")
                
                # 3. Anti-Flood Delay (Wait before next spin in this turn)
                await asyncio.sleep(4)

    except Exception as e:
        logger.error(f"[ERROR] User {user_id}: {e}")
        # If error occurs (e.g. timeout), wait 60s before retrying to prevent spamming errors
        database.user_data[user_id]['next_play_time'] = time.time() + 60

async def start_relay_race():
    """
    The Supervisor Loop: continuously checks who is ready to play.
    """
    if database.is_running:
        return
    database.is_running = True
    
    logger.info("🏁 Relay Race Background Task Started!")

    while True:
        # If queue is empty, stop the worker
        if not database.farming_queue:
            logger.info("Queue is empty. Worker stopping.")
            database.is_running = False
            break

        active_run_found = False
        current_time = time.time()
        
        # --- LOOP THROUGH QUEUE ---
        for user_id in list(database.farming_queue):
            
            if user_id not in database.user_data:
                continue

            # Only play if their personal cooldown timer has passed
            if current_time >= database.user_data[user_id]['next_play_time']:
                
                database.current_active_user = user_id 
                await play_user_turn(user_id)
                database.current_active_user = None
                active_run_found = True
        
        # --- SLEEP LOGIC ---
        if not active_run_found and database.farming_queue:
            # Calculate sleep time based on the earliest wake-up
            wake_times = []
            for uid in database.farming_queue:
                if uid in database.user_data:
                    wake_times.append(database.user_data[uid]['next_play_time'])
            
            if wake_times:
                earliest_wake = min(wake_times)
                sleep_duration = earliest_wake - time.time()
                
                if sleep_duration > 0:
                    logger.info(f"💤 All accounts resting. Next wake up in {int(sleep_duration)}s.")
                    await asyncio.sleep(sleep_duration + 5)
                else:
                    await asyncio.sleep(5)
            else:
                await asyncio.sleep(5)
        else:
            await asyncio.sleep(5)
