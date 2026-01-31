# database.py
from telethon import TelegramClient

# Dictionary to store connected clients
# Format: {user_id: TelegramClient_Object}
clients = {} 

# Dictionary to store user stats and timers
# Format: {user_id: {'extols': 0, 'next_play_time': 0, 'name': 'Unknown'}}
user_data = {} 

# List of User IDs waiting in the relay race queue
farming_queue = [] 

# Flag to check if the background worker is running
is_running = False

# Tracks who is currently playing (for stats display)
current_active_user = None

# --- WATCHDOG VARIABLES (New) ---
# Tracks the exact time (timestamp) the current user started their turn
active_user_start_time = 0  

# Flag to tell the main worker loop to abort the current user immediately
force_abort_flag = False    

def get_all_sessions():
    """Extracts session strings for export."""
    export_data = {}
    for user_id, client in clients.items():
        try:
            # Save the session string
            export_data[str(user_id)] = client.session.save()
        except Exception:
            pass
    return export_data
    
