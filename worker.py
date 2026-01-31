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

    # Double-check cooldown before starting
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

                # 1. Update Internal Stats
                won = utils.parse_extols(text)
                database.user_data[user_id]['extols'] += won

                # --- STOP CONDITION 1: EMPTY SLOTS ---
                if "Remaining Slot Usage: 0" in text:
                    # 12 Hours + 10 Minutes
                    sleep_duration = (12 * 3600) + (10 * 60)
                    resume_time = time.time() + sleep_duration
                    
                    database.user_data[user_id]['next_play_time'] = resume_time
                    logger.info(f"🛑 {me.first_name}: Slots Empty. Sleeping 12h 10m.")
                    break
                
                # --- STOP CONDITION 2: COOLDOWN MESSAGE ---
                elif "play again in" in text:
                    # Independent Regex Checks (Robust against formatting)
                    h_match = re.search(r'(\d+)\s*h', text, re.IGNORECASE)
                    m_match = re.search(r'(\d+)\s*m', text, re.IGNORECASE)
                    s_match = re.search(r'(\d+)\s*s', text, re.IGNORECASE)

                    h = int(h_match.group(1)) if h_match else 0
                    m = int(m_match.group(1)) if m_match else 0
                    s = int(s_match.group(1)) if s_match else 0
                    
                    total_seconds = (h * 3600) + (m * 60) + s
                    
                    if total_seconds == 0:
                        # Fallback if parse fails
                        logger.warning(f"⚠️ {me.first_name}: Parse failed (Got 0s). Defaulting to 30 mins.")
                        total_seconds = 1800

                    # Add 30s buffer
                    resume_time = time.time() + total_seconds + 30
                    
                    database.user_data[user_id]['next_play_time'] = resume_time
                    logger.info(f"⏳ {me.first_name}: Cooldown detected. Sleeping {h}h {m}m {s}s.")
                    break

                # Log successful spin
                logger.info(f"🎰 {me.first_name}: Spin successful. Won: {won}")
                
                # Anti-Flood Delay
                await asyncio.sleep(4)

    except Exception as e:
        logger.error(f"[ERROR] User {user_id}: {e}")
        # Wait 60s on error before retrying
        database.user_data[user_id]['next_play_time'] = time.time() + 60

async def start_relay_race():
    """
    The Supervisor Loop: Checks queue constantly.
    """
    if database.is_running:
        logger.warning("⚠️ Worker is already running!")
        return
        
    database.is_running = True
    logger.info("🏁 Relay Race Background Task Started!")

    try:
        while True:
            # If queue is empty, stop the worker
            if not database.farming_queue:
                logger.info("Queue is empty. Worker stopping.")
                break

            active_run_found = False
            current_time = time.time()
            
            # --- LOOP THROUGH QUEUE ---
            for user_id in list(database.farming_queue):
                if user_id not in database.user_data: continue

                # Check if timer has passed
                if current_time >= database.user_data[user_id]['next_play_time']:
                    
                    database.current_active_user = user_id 
                    await play_user_turn(user_id)
                    database.current_active_user = None
                    active_run_found = True
            
            # --- NON-BLOCKING SLEEP ---
            # If everyone is sleeping, wait 30 seconds and check again.
            # Do NOT wait the full 12 hours, or the bot will freeze.
            if not active_run_found:
                await asyncio.sleep(30)
            else:
                await asyncio.sleep(5)

    except Exception as e:
        logger.error(f"❌ Worker Crashed: {e}")
    finally:
        database.is_running = False
        logger.info("🛑 Worker Stopped.")
