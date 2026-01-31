# worker.py
import asyncio
import time
import logging
import re
import config
import database
import utils

logger = logging.getLogger(__name__)

# Pattern to extract time from: "You can play again in 10h 26m 44s."
# Added \s* to allow spaces like "12 h" or "30 s"
COOLDOWN_PATTERN = re.compile(r"play again in\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", re.IGNORECASE)

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
                    match = COOLDOWN_PATTERN.search(text)
                    if match:
                        h = int(match.group(1) or 0)
                        m = int(match.group(2) or 0)
                        s = int(match.group(3) or 0)
                        
                        total_seconds = (h * 3600) + (m * 60) + s
                        # Add 30s buffer to be safe
                        resume_time = time.time() + total_seconds + 30
                        
                        database.user_data[user_id]['next_play_time'] = resume_time
                        logger.info(f"⏳ {me.first_name}: Cooldown detected. Sleeping {h}h {m}m {s}s.")
                    else:
                        # Fallback if regex fails but message is present
                        logger.warning(f"⚠️ {me.first_name}: Cooldown msg found but parse failed. Defaulting to 30m.")
                        database.user_data[user_id]['next_play_time'] = time.time() + 1800
                        
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
        # We create a copy of the list so we don't crash if someone logs out mid-loop
        for user_id in list(database.farming_queue):
            
            # Skip if user data is missing (logged out)
            if user_id not in database.user_data:
                continue

            # Only play if their personal cooldown timer has passed
            if current_time >= database.user_data[user_id]['next_play_time']:
                
                # Mark as active for stats display
                database.current_active_user = user_id 
                
                # Play the turn (runs until slots empty or cooldown hit)
                await play_user_turn(user_id)
                
                # Unmark active
                database.current_active_user = None
                active_run_found = True
        
        # --- SLEEP LOGIC (Optimization) ---
        # If NO one played this round, it means everyone is sleeping.
        # We should wait until the EARLIEST wake-up time to save CPU.
        if not active_run_found and database.farming_queue:
            
            # Gather all wake-up times
            wake_times = []
            for uid in database.farming_queue:
                if uid in database.user_data:
                    wake_times.append(database.user_data[uid]['next_play_time'])
            
            if wake_times:
                earliest_wake = min(wake_times)
                sleep_duration = earliest_wake - time.time()
                
                if sleep_duration > 0:
                    logger.info(f"💤 All accounts resting. Next wake up in {int(sleep_duration)}s.")
                    # Sleep until the first person is ready (+5s buffer)
                    await asyncio.sleep(sleep_duration + 5)
                else:
                    await asyncio.sleep(5)
            else:
                await asyncio.sleep(5)
        else:
            # Small buffer between queue cycles
            await asyncio.sleep(5)
