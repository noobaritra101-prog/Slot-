import sqlite3
import threading
import os

DB_FILE = "runner_panel.db"
lock = threading.Lock()

def _init_db():
    """Initializes the database and creates the users table if it doesn't exist."""
    with lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                active_project TEXT
            )
        ''')
        conn.commit()
        conn.close()

# Run initialization on import
_init_db()

def get_all_users():
    """Returns a list of all user IDs in the database."""
    with lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        rows = c.fetchall()
        conn.close()
        return [row[0] for row in rows]

def get_active_project(user_id):
    """Gets the currently active project for a user."""
    with lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT active_project FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

def set_active_project(user_id, project_name):
    """Sets or updates the active project for a user."""
    with lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if project_name is None:
            # If project_name is None, just set it to NULL
            c.execute("UPDATE users SET active_project = NULL WHERE user_id = ?", (user_id,))
        else:
            # Insert or update the active project
            c.execute("""
                INSERT INTO users (user_id, active_project) 
                VALUES (?, ?) 
                ON CONFLICT(user_id) 
                DO UPDATE SET active_project = excluded.active_project
            """, (user_id, project_name))
        conn.commit()
        conn.close()
