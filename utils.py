# utils.py
import time
import re
import os
from datetime import datetime, timedelta

START_TIME = time.time()

def get_uptime():
    """Calculates how long the bot has been running."""
    uptime_seconds = int(time.time() - START_TIME)
    td = timedelta(seconds=uptime_seconds)
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"

def parse_extols(text):
    """Finds the currency amount (Є) in the message."""
    # Looks for Є followed by numbers (e.g., Є459)
    match = re.search(r'Є(\d+)', text)
    if match:
        return int(match.group(1))
    return 0

def format_status(user_id, current_active_user):
    """Returns the visual status icon for the stats panel."""
    from database import user_data
    
    # If this user is the one currently sending commands
    if user_id == current_active_user:
        return "【🟢】" # Active / Playing
    
    # If the user is ready to play (time passed)
    if time.time() >= user_data[user_id]['next_play_time']:
        return "【🟡】" # Waiting / Ready
        
    return "【🔴】" # Cooldown / Sleeping

def read_last_logs(file_path, lines_count=15):
    """Reads the last N lines of the log file for display."""
    if not os.path.exists(file_path):
        return "No logs found yet."
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            last_lines = lines[-lines_count:]
            return "".join(last_lines)
    except Exception as e:
        return f"Error reading logs: {e}"

def clear_logs(file_path):
    """Wipes the log file."""
    with open(file_path, "w"):
        pass
