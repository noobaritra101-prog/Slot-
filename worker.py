# worker.py
import asyncio
import time
import logging
import config
import database
import utils

logger = logging.getLogger(__name__)

async def play_user_turn(user_id):
    """
    Plays slots for ONE specific user until they hit a limit.
    """
    client = database.clients.get(user_id)
    if not client: return

    # Check cooldown before starting (Safety check)
    if time.time() < database.user_data[user_id]['next_play_time']:
        return

    logger.info(f"[START] Turn started for User {user_id}")
    
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

                # 2. Check Logic: Stop Conditions
                if "Remaining Slot Usage: 0" in text:
                    logger.info(f"[LIMIT STOP] Daily slot limit reached for {user_id}.")
                    break
                
                if "You have used 12 slots" in text or "play again in" in text:
                    logger.info(f"[LIMIT STOP] Global 12h limit reached for {user_id}.")
                    break
                
                # Log success
                logger.info(f"[HUNT SUCCESS] Game responded for {user_id}. Won: {won}")
                
                # 3. Anti-Flood Delay
                await asyncio.sleep(4)

        # 4. Set Cooldown Timer (12 Hours + 10 Minutes = 43800 Seconds)
        database.user_data[user_id]['next_play_time'] = time.time() + 43800
        logger.info(f"[HUNT ABORT] Turn finished for {user_id}. Cooldown started.")

    except Exception as e:
        logger.error(f"[ERROR] User {user_id}: {e}")
        # If error occurs, we assume turn is over to prevent stalling
        await asyncio.sleep(5)

async def start_relay_race():
    """
    The Supervisor Loop: Picks one user at a time to play.
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
        
        # --- LOOP THROUGH QUEUE ---
        for user_id in database.farming_queue:
            # Only play if their cooldown timer has passed
            if time.time() >= database.user_data[user_id]['next_play_time']:
                
                # Mark as active for stats
                database.current_active_user = user_id 
                
                # Play the turn
                await play_user_turn(user_id)
                
                # Unmark active
                database.current_active_user = None
                active_run_found = True
        
        # --- SLEEP LOGIC ---
        if not active_run_found and database.farming_queue:
            # Everyone is on cooldown. Calculate sleep time.
            wake_times = [database.user_data[uid]['next_play_time'] for uid in database.farming_queue]
            earliest_wake = min(wake_times)
            sleep_duration = earliest_wake - time.time()
            
            if sleep_duration > 0:
                logger.info(f"💤 All accounts resting. Sleeping for {int(sleep_duration)} seconds.")
                await asyncio.sleep(sleep_duration + 5) # +5s buffer
            else:
                await asyncio.sleep(10)
        else:
            # Small buffer between turns
            await asyncio.sleep(5)
