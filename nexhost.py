import os
import sys
import shutil
import asyncio
import zipfile
import re
import time
import json 
import psutil
import platform
import uuid
import html
import ast
import logging
import traceback
import functools
from pyrogram import Client, filters, idle, StopPropagation, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
import db

# --- ERROR LOGGING SETUP ---
# All unhandled errors (command crashes AND silent background-task crashes)
# now get written here, so "errors not showing properly" has a paper trail
# even when Telegram itself can't be reached.
logging.basicConfig(
    filename="bot_errors.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("NexHost")
# Also echo to console so it shows up in `pm2 logs` / systemd journal / screen.
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_console)

# RAM Limiter logic (Only works on Linux/Unix VPS environments)
try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    HAS_RESOURCE = False

# --- CONFIGURATION & STATE ---
API_ID = "26759620"        
API_HASH = "e5c2cfff7011b7fee949ed8293bafde8"    
BOT_TOKEN = "8807408443:AAEUvC6RXX_CPJKsKsqN3jdXEu8hgXCzR28" 
ADMIN_IDS = [5716292610]        

# 🚨 CLOUD BACKUP CHANNEL 🚨
BACKUP_CHANNEL_ID = "@nex_host_backup"

# Initialize Client
app = Client(
    "NexHostBot", 
    api_id=API_ID, 
    api_hash=API_HASH, 
    bot_token=BOT_TOKEN, 
    in_memory=True,
    parse_mode=enums.ParseMode.HTML
)

# SYSTEM & RUN STATE TRACKING
BOT_START_TIME = time.time()
ACTIVE_PROCESSES = {} 
RUNNING_SCRIPTS = {} 
EDIT_STATES = {} 
RENAME_STATES = {} 
VIEW_STATES = {}
PENDING_INVITES = {}
USER_CWD = {} 
BASE_DIR = "./projects"
MAINTENANCE_MODE = False

# --- RESOURCE LIMITS (tune these for your VPS) ---
MAX_RAM_GB = 9          # RAM ceiling per running user script (was 1.2GB)
MAX_FILE_SIZE_MB = 500  # Max single file upload size

psutil.cpu_percent(interval=None)

# --- BACKUP CONFIGURATION ---
BACKUP_INTERVAL_MINS = 40
LAST_BACKUP_TIME = time.time()

if os.path.exists("backup_config.json"):
    try:
        with open("backup_config.json", "r") as f: BACKUP_INTERVAL_MINS = json.load(f).get("interval", 40)
    except Exception: pass


# ==========================================
# 0. SMART INLINE BUTTON WRAPPER
# ==========================================
def create_btn(text, cb=None, url=None):
    """ Standard Inline Button without colors to prevent UI crashes """
    kwargs = {"text": text}
    if url: kwargs["url"] = url
    elif cb: kwargs["callback_data"] = cb
    return InlineKeyboardButton(**kwargs)


# ==========================================
# 1. FAST COLLABORATION & STATE MANAGERS
# ==========================================
def get_collabs():
    if os.path.exists("collabs.json"):
        try:
            with open("collabs.json", "r") as f: return json.load(f)
        except Exception: pass
    return {}

def save_collabs(data):
    with open("collabs.json", "w") as f: json.dump(data, f)

def resolve_project_owner(user_id, active_proj_string):
    if active_proj_string and "@" in active_proj_string:
        owner_id, proj = active_proj_string.split("@", 1)
        return int(owner_id), proj
    return user_id, active_proj_string

def get_user_dir(user_id): return os.path.join(BASE_DIR, f"user_{user_id}")
def get_project_dir(owner_id, project_name): return os.path.join(get_user_dir(owner_id), project_name)

def log_collab_event(owner_id, proj_name, action_user, action_desc):
    log_path = os.path.join(get_project_dir(owner_id, proj_name), "collab_audit.log")
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        role = "Owner" if int(owner_id) == int(action_user) else "Collaborator"
        with open(log_path, "a", encoding="utf-8") as f: 
            f.write(f"[{timestamp}] {role} {action_user}: {action_desc}\n")
    except Exception: pass

def save_running_state():
    state = {str(k): v for k, v in RUNNING_SCRIPTS.items()}
    try:
        with open("active_runs.json", "w") as f: json.dump(state, f)
    except Exception: pass

def load_running_state():
    if os.path.exists("active_runs.json"):
        try:
            with open("active_runs.json", "r") as f: return json.load(f)
        except Exception: pass
    return {}

# ==========================================
# 2. MIDDLEWARE (MAINTENANCE & TEXT EDITOR)
# ==========================================
@app.on_message(group=-2)
async def maintenance_check_msg(client: Client, message: Message):
    global MAINTENANCE_MODE
    if MAINTENANCE_MODE and message.from_user and message.from_user.id not in ADMIN_IDS:
        if (message.text and message.text.startswith("/")) or message.document:
            await message.reply_text("⚠️ <b>Maintenance Mode is active.</b>\nNex Host is currently undergoing updates.")
        raise StopPropagation

@app.on_message(filters.text & filters.private, group=-1)
async def handle_text_states(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in EDIT_STATES:
        if message.text.startswith("/"):
            if message.text.strip().lower() == "/cancel":
                del EDIT_STATES[user_id]
                await message.reply_text("🚫 <b>Edit Canceled.</b>")
                raise StopPropagation
            else:
                del EDIT_STATES[user_id]
                await message.reply_text("🚫 <b>Edit aborted due to command execution.</b>")
                return
        state = EDIT_STATES[user_id]
        try:
            with open(state["path"], "w", encoding="utf-8") as f: f.write(message.text)
            log_collab_event(state["owner_id"], state["proj_name"], user_id, f"Edited code in: {state['rel_path']}")
            await message.reply_text(f"✅ <b>File <code>{state['rel_path']}</code> saved!</b>")
        except Exception as e: await message.reply_text(f"❌ <b>Error:</b>\n<code>{safe_html_log(e)}</code>")
        del EDIT_STATES[user_id]
        raise StopPropagation

    elif user_id in RENAME_STATES:
        if message.text.startswith("/"):
            if message.text.strip().lower() == "/cancel":
                del RENAME_STATES[user_id]
                await message.reply_text("🚫 <b>Rename Canceled.</b>")
                raise StopPropagation
            else:
                del RENAME_STATES[user_id]
                await message.reply_text("🚫 <b>Rename aborted due to command execution.</b>")
                return
        state = RENAME_STATES.pop(user_id)
        old_path = state["path"]
        new_name = message.text.strip()
        if "/" in new_name or "\\" in new_name or ".." in new_name:
            await message.reply_text("❌ <b>Invalid file name.</b>")
            raise StopPropagation
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.exists(new_path):
            await message.reply_text("❌ <b>File already exists!</b>")
            raise StopPropagation
        try:
            os.rename(old_path, new_path)
            log_collab_event(state["owner_id"], state["proj_name"], user_id, f"Renamed {state['rel_path']} to {new_name}")
            await message.reply_text(f"✅ <b>Renamed to <code>{new_name}</code> sucssessfully!</b>")
        except Exception as e: await message.reply_text(f"❌ <b>Error:</b>\n<code>{safe_html_log(e)}</code>")
        raise StopPropagation

@app.on_callback_query(group=-1)
async def maintenance_check_cb(client: Client, callback: CallbackQuery):
    global MAINTENANCE_MODE
    if MAINTENANCE_MODE and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⚠️ Maintenance Mode is active.", show_alert=True)
        raise StopPropagation

# ==========================================
# 3. HELPER FUNCTIONS & SAFE HTML PARSER
# ==========================================
def safe_html_log(text):
    """ Uses standard html escaping to protect elements from breaking. """
    if not text: return "(Empty)"
    return html.escape(str(text))

def safe_handler(func):
    """
    Wraps every command/callback handler so that:
      1. A crash inside a handler is logged (console + bot_errors.log) instead
         of vanishing into pyrogram's internal logger.
      2. The user actually SEES that something failed, with the real error,
         instead of the bot just going quiet on that command.
      3. StopPropagation (pyrogram's internal control-flow signal) is left
         alone so maintenance-mode / text-state middleware keeps working.
    """
    @functools.wraps(func)
    async def wrapper(client, update, *args, **kwargs):
        try:
            return await func(client, update, *args, **kwargs)
        except StopPropagation:
            raise
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"Handler '{func.__name__}' crashed:\n{tb}")

            err_text = (
                f"❌ <b>Something went wrong in</b> <code>{func.__name__}</code>:\n"
                f"<code>{safe_html_log(f'{type(e).__name__}: {e}')}</code>\n\n"
                f"<i>This has been logged. If it keeps happening, check bot_errors.log.</i>"
            )
            try:
                if isinstance(update, CallbackQuery):
                    try: await update.answer("⚠️ Error — see chat for details.", show_alert=True)
                    except Exception: pass
                    if update.message: await update.message.reply_text(err_text)
                else:
                    await update.reply_text(err_text)
            except Exception as notify_err:
                logger.error(f"Also failed to notify user about the error above: {notify_err}")
    return wrapper

def get_running_scripts(owner_id):
    return [RUNNING_SCRIPTS[k] for k, p in ACTIVE_PROCESSES.items() if k.startswith(f"{owner_id}:") and p.returncode is None]

def generate_tree(dir_path, prefix=""):
    tree_str = ""
    try: items = os.listdir(dir_path)
    except PermissionError: return ""
    items = [i for i in items if i not in ['venv', '__pycache__', '.git'] and not i.startswith('.')]
    items.sort()
    for i, item in enumerate(items):
        path = os.path.join(dir_path, item)
        is_last = (i == len(items) - 1)
        connector = "└── " if is_last else "├── "
        tree_str += f"{prefix}{connector}{item}\n"
        if os.path.isdir(path): tree_str += generate_tree(path, prefix + ("    " if is_last else "│   "))
    return tree_str

def get_progress_bar(percentage, length=10):
    filled = int((percentage / 100) * length)
    return "▰" * filled + "▱" * (length - filled)

def format_uptime(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def get_file_icon(filename):
    ext = filename.lower().split('.')[-1] if '.' in filename else ""
    icons = {'py': '🐍', 'txt': '📄', 'log': '📜', 'env': '🔑', 'json': '⚙️', 'yaml': '⚙️', 'yml': '⚙️', 'zip': '🗜️'}
    return icons.get(ext, '📄')

def get_maintenance_text():
    status_icon = "Enabled" if MAINTENANCE_MODE else "Disabled"
    return (
        "🔧 <b>Nex Host — Maintenance System</b>\n"
        "<blockquote>"
        f"📡 <b>Status:</b> {status_icon}\n"
        "🔒 <b>Access:</b> Admin Only\n"
        "⚙️ <b>Scripts:</b> Running"
        "</blockquote>"
    )

def get_maintenance_keyboard():
    return InlineKeyboardMarkup([
        [create_btn("Enable", cb="maint_enable"), create_btn("Disable", cb="maint_disable")],
        [create_btn("Close", cb="maint_close")]
    ])

def build_help_text(user_id):
    txt = (
        "🆘 <b>Help &amp; Command Guide</b>\n\n"
        "<b>📁 Projects</b>\n"
        "<blockquote>"
        "<code>/newproject [name]</code> — create a workspace\n"
        "<code>/deleteproject [name]</code> — wipe a workspace\n"
        "<code>/myprojects</code> — list &amp; switch workspaces\n"
        "<code>/myfiles</code> — graphical file manager\n"
        "<code>/tree</code> — folder tree view\n"
        "<code>/status</code> — quick snapshot of your active project"
        "</blockquote>\n"
        "<b>▶️ Running Code</b>\n"
        "<blockquote>"
        "<code>/run [file.py]</code> — execute a file\n"
        "<code>/restart [file.py]</code> — restart a script\n"
        "<code>/stop</code> — stop a running script\n"
        "<code>/input [text]</code> — send data to console\n"
        "<code>/logs</code> — view terminal output"
        "</blockquote>\n"
        "<b>📄 Files</b>\n"
        "<blockquote>"
        "<code>/rename [old] [new]</code> — rename a file\n"
        "<code>/mkdir [name]</code> / <code>/rmdir [name]</code> — folders\n"
        "<code>/deletefile [path]</code> — delete a file\n"
        "<code>/clone [url]</code> — clone a GitHub repo\n"
        "<code>/backup_proj</code> — download workspace as ZIP\n"
        "<code>/import</code> — upload a ZIP into a workspace\n"
        "<code>/installreqs</code> — install requirements.txt"
        "</blockquote>\n"
        "<b>🤝 Collaboration</b>\n"
        "<blockquote>"
        "<code>/addcollab [proj] [uid]</code> — invite a user\n"
        "<code>/collabs</code> — view collaborators on active project\n"
        "<code>/mycollabs</code> — workspaces shared with you\n"
        "<code>/leavecollab</code> — leave a shared workspace\n"
        "<code>/remcollab [proj] [uid]</code> — remove a collaborator"
        "</blockquote>"
    )
    if user_id in ADMIN_IDS:
        txt += (
            "\n\n<b>👑 Admin</b>\n"
            "<blockquote>"
            "<code>/maintenance</code> — toggle maintenance mode\n"
            "<code>/backup</code> — manual cloud backup\n"
            "<code>/setbackup [mins]</code> — auto-backup interval\n"
            "<code>/broadcast [msg]</code> — announcement to all users\n"
            "<code>/dashboard</code> — system vitals\n"
            "<code>/admin_active</code> — every running process, globally\n"
            "<code>/admin_stop [uid] [proj] [file]</code>\n"
            "<code>/admin_deleteproj [uid] [proj]</code>"
            "</blockquote>"
        )
    return txt

async def build_status_text(user_id):
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str:
        return "📊 <b>Your Status</b>\n<blockquote>No active project selected. Use /myprojects to pick one.</blockquote>"

    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    is_collab = "@" in active_proj_str
    running = [rs for rs in get_running_scripts(owner_id) if rs.startswith(f"{proj_name}/")]

    text = "📊 <b>Your Status</b>\n<blockquote>"
    text += f"🗂 <b>Active Project:</b> {proj_name}"
    text += " (🤝 shared)\n" if is_collab else "\n"
    text += f"🟢 <b>Running Scripts:</b> {len(running)}\n"
    text += f"⏱ <b>Bot Uptime:</b> {format_uptime(time.time() - BOT_START_TIME)}"
    text += "</blockquote>"
    return text

async def generate_dashboard_text(client: Client, view="system"):
    header = "🌐 <b>Nex Host Cloud OS • v4.0</b>\n\n"
    content = ""
    
    if view == "files":
        text = "📂 <b>Network File Architecture</b>\n"
        users = db.get_all_users()
        if not users: 
            content = text + "<i>(No active projects)</i>\n\n"
        else:
            body = ""
            for u_id in users:
                user_dir = get_user_dir(u_id)
                if not os.path.exists(user_dir): continue
                projects = [f for f in os.listdir(user_dir) if os.path.isdir(os.path.join(user_dir, f))]
                if not projects: continue
                
                # Try fetching the actual Telegram name of the User
                try:
                    u_obj = await client.get_users(int(u_id))
                    u_name = html.escape(u_obj.first_name or f"User {u_id}")
                except Exception:
                    u_name = f"User {u_id}"
                    
                body += f"👤 <a href='tg://user?id={u_id}'>{u_name}</a>\n"
                
                for proj in projects:
                    body += f" ├── 📂 {proj}/\n"
                    proj_path = os.path.join(user_dir, proj)
                    
                    all_files = []
                    # Recursively map all files in the project directory
                    for root, _, files in os.walk(proj_path):
                        if 'venv' in root or '__pycache__' in root or '.git' in root: continue
                        for f in files:
                            if not f.startswith('.'):
                                rel_path = os.path.relpath(os.path.join(root, f), proj_path)
                                all_files.append(rel_path)
                                
                    all_files.sort()
                    for file in all_files:
                        status = "🟢" if f"{u_id}:{proj}:{file}" in ACTIVE_PROCESSES else get_file_icon(file)
                        body += f" │   └── {status} {file}\n"
            content = text + f"<blockquote expandable>{body}</blockquote>\n"

    elif view == "system":
        active_deployments = len([k for k, p in ACTIVE_PROCESSES.items() if p.returncode is None])
        cpu_percent = psutil.cpu_percent(interval=None) 
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        content += (
            "📊 <b>System Vitals</b>\n"
            "<blockquote>"
            f"⚡ <b>CPU</b>  {get_progress_bar(cpu_percent)} {cpu_percent}%\n"
            f"📟 <b>RAM</b>  {get_progress_bar(ram.percent)} {ram.percent}%\n"
            f"💾 <b>Disk</b> {get_progress_bar(disk.percent)} {disk.percent}%"
            "</blockquote>\n"
            "⚙️ <b>Runtime &amp; Network</b>\n"
            "<blockquote>"
            f"⏱️ <b>Uptime:</b> {format_uptime(time.time() - BOT_START_TIME)}\n"
            f"📦 <b>Active Deployments:</b> {active_deployments:02d}\n"
            f"☁️ <b>Auto-Backup:</b> Every {BACKUP_INTERVAL_MINS} min"
            "</blockquote>"
        )
    return header + content

async def render_view_page(msg_or_cb, user_id):
    state = VIEW_STATES.get(user_id)
    if not state: 
        if isinstance(msg_or_cb, CallbackQuery): await msg_or_cb.answer("Session expired.", show_alert=True)
        return
        
    page = state["page"]
    chunks = state["chunks"]
    rel_path = state["rel_path"]
    
    header_text = f"📖 <b>{rel_path}</b> (Page {page+1}/{len(chunks)})\n"
    code_text = safe_html_log(chunks[page])
    final_text = f"{header_text}<pre><code class='language-python'>{code_text}</code></pre>"
    
    kb = []
    nav_row = []
    if page > 0: nav_row.append(create_btn("⬅️ Prev", cb="view_prev"))
    if page < len(chunks) - 1: nav_row.append(create_btn("Next ➡️", cb="view_next"))
    if nav_row: kb.append(nav_row)
    kb.append([create_btn("❌ Close Viewer", cb="view_close")])
    
    if isinstance(msg_or_cb, CallbackQuery):
        if msg_or_cb.message.text: await msg_or_cb.message.edit_text(final_text, reply_markup=InlineKeyboardMarkup(kb))
        else: await msg_or_cb.message.reply_text(final_text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg_or_cb.reply_text(final_text, reply_markup=InlineKeyboardMarkup(kb))

# ==========================================
# 4. ☁️ TELEGRAM CLOUD RESTORE & BACKUP
# ==========================================
async def restore_system(client):
    try: msg = await client.send_message(ADMIN_IDS[0], "☁️ <b>Checking backups...</b>")
    except: msg = None

    try:
        chat = await client.get_chat(BACKUP_CHANNEL_ID)
        if chat.pinned_message:
            full_msg = await client.get_messages(BACKUP_CHANNEL_ID, chat.pinned_message.id)
            if full_msg.document and full_msg.document.file_name == "system_backup.zip":
                if msg: await msg.edit_text("📥 <b>Restoring Nex Host System...</b>")
                file_path = await client.download_media(full_msg.document)
                
                def _extract():
                    with zipfile.ZipFile(file_path, 'r') as zip_ref: zip_ref.extractall(".")
                    os.remove(file_path)
                await asyncio.to_thread(_extract)
                
                if msg: await msg.edit_text("✅ <b>Restore ComPLete!</b>")
                return
        if msg: await msg.edit_text("ℹ️ <i>No backup found. Starting fresh.</i>")
    except Exception as e:
        if msg: await msg.edit_text(f"⚠️ <b>Restore Failed:</b>\n<code>{safe_html_log(e)}</code>")

async def perform_backup(client, status_msg=None):
    global LAST_BACKUP_TIME
    LAST_BACKUP_TIME = time.time()
    zip_path = "system_backup.zip"
    try:
        def _zip_files():
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for db_file in ["runner_panel.db", "active_runs.json", "collabs.json", "backup_config.json"]:
                    if os.path.exists(db_file): zipf.write(db_file)
                for file in os.listdir("."):
                    if file.endswith((".session", ".session-journal")): zipf.write(file)
                if os.path.exists("projects"):
                    for root, _, files in os.walk("projects"):
                        if 'venv' in root or '__pycache__' in root or '.git' in root: continue
                        for file in files: zipf.write(os.path.join(root, file))
        await asyncio.to_thread(_zip_files)
        
        chat = await client.get_chat(BACKUP_CHANNEL_ID)
        if chat.pinned_message: await client.delete_messages(BACKUP_CHANNEL_ID, chat.pinned_message.id)
        
        sent_msg = await client.send_document(BACKUP_CHANNEL_ID, document=zip_path, caption=f"☁️ <b>Nex Host Backup</b>\n📅 {time.strftime('%Y-%m-%d %H:%M:%S')}")
        os.remove(zip_path)
        await sent_msg.pin(disable_notification=True)
        if status_msg: await status_msg.edit_text("✅ <b>Backup saved to Cloud!</b>")
    except Exception as e:
        if status_msg: await status_msg.edit_text(f"❌ <b>Backup Failed:</b>\n<code>{safe_html_log(e)}</code>")

async def auto_backup_loop(client):
    global LAST_BACKUP_TIME
    while True:
        await asyncio.sleep(60) 
        if time.time() - LAST_BACKUP_TIME >= BACKUP_INTERVAL_MINS * 60:
            await perform_backup(client)

# ==========================================
# 5. 🚨 HTML ANIMATED DEPLOYMENT & AUTO-HEAL
# ==========================================
def get_recent_logs(project_dir):
    log_file_path = os.path.join(project_dir, "run.log")
    if os.path.exists(log_file_path):
        with open(log_file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return "".join(lines[-15:])
    return "Container output initializing..."

async def read_stdout(process, client, chat_id, project_dir, owner_id, rel_path, attempted_modules):
    process_key = f"{owner_id}:{os.path.basename(project_dir)}:{rel_path}"
    log_file_path = os.path.join(project_dir, "run.log")
    pip_bin = os.path.join(os.path.abspath(project_dir), "venv", "bin", "pip")

    while True:
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=1.5)
            if not line: break 
            decoded = line.decode('utf-8', errors='ignore').strip()
            if decoded:
                with open(log_file_path, "a", encoding="utf-8") as f: f.write(decoded + "\n")
                
                match = re.search(r"No module named '([^']+)'", decoded)
                if match:
                    module = match.group(1)
                    if module in attempted_modules: continue
                    attempted_modules.add(module)
                    
                    pkg_map = {"dotenv": "python-dotenv", "bs4": "beautifulsoup4", "cv2": "opencv-python", "yaml": "pyyaml", "PIL": "Pillow", "telebot": "pyTelegramBotAPI"}
                    pkg = pkg_map.get(module, module)
                    
                    anim = f"⚙️ <u><b>AUTO-HEAL INITIATED...</b></u>\n└─ missing module detected: <code>{pkg}</code>\n└─ installing dependencies..."
                    msg = await client.send_message(chat_id, anim)
                    
                    proc = await asyncio.create_subprocess_shell(f"{pip_bin} install {pkg}")
                    await proc.wait()
                    
                    # Persist automatically installed module into requirements.txt
                    try:
                        req_p = os.path.join(project_dir, "requirements.txt")
                        mode = "a" if os.path.exists(req_p) else "w"
                        with open(req_p, mode) as f: f.write(f"\n{pkg}")
                    except Exception: pass
                    
                    anim = f"⚙️ <u><b>AUTO-HEAL INITIATED...</b></u>\n└─ missing module detected: <code>{pkg}</code>\n└─ resolving packages ✓\n\n🔁 <b>Restarting script...</b>"
                    await msg.edit_text(anim)
                    
                    if process.returncode is None:
                        process.terminate()
                        await process.wait() 
                    
                    await asyncio.sleep(0.5)
                    await msg.delete()
                    asyncio.create_task(start_process(client, chat_id, chat_id, rel_path, attempted_modules, action="autoheal"))
                    return 
        except asyncio.TimeoutError:
            if process.returncode is not None: break 
                
    await process.wait()
    if ACTIVE_PROCESSES.get(process_key) == process:
        RUNNING_SCRIPTS.pop(process_key, None)
        ACTIVE_PROCESSES.pop(process_key, None)
        save_running_state() 

async def start_process(client, chat_id, initiator_id, rel_path, attempted_modules=None, action="deploy"):
    if attempted_modules is None: attempted_modules = set()
    
    active_proj_str = db.get_active_project(initiator_id)
    if not active_proj_str: return await client.send_message(chat_id, "❌ <b>No active project.</b>")
    
    owner_id, proj_name = resolve_project_owner(initiator_id, active_proj_str)
    process_key = f"{owner_id}:{proj_name}:{rel_path}"
    abs_project_dir = os.path.abspath(get_project_dir(owner_id, proj_name))
    
    if not os.path.exists(os.path.join(abs_project_dir, rel_path)): 
        return await client.send_message(chat_id, f"❌ <code>{rel_path}</code> <b>not found.</b>")
    
    if process_key in ACTIVE_PROCESSES and ACTIVE_PROCESSES[process_key].returncode is None:
        return await client.send_message(chat_id, f"⚠️ <code>{rel_path}</code> <b>is already running!</b>")

    log_collab_event(owner_id, proj_name, initiator_id, f"Started code execution: {rel_path}")

    if action == "deploy": header = "⚙️ <b>STARTING DEPLOYMENT...</b>"
    elif action == "restart": header = "🔁 <b>RESTARTING DEPLOYMENT...</b>"
    else: header = "🔧 <b>AUTO-HEAL RESTART...</b>"
        
    anim = f"{header}\n\n📦 <b>CREATING VIRTUAL ENVIRONMENT...</b>\n"
    msg = await client.send_message(chat_id, anim)
    
    venv_dir = os.path.join(abs_project_dir, "venv")
    pip_bin = os.path.join(venv_dir, "bin", "pip")
    
    try:
        if not os.path.exists(venv_dir):
            await (await asyncio.create_subprocess_shell(f"python3 -m venv {venv_dir}")).wait()
            
        anim += "└─ venv initialized ✓\n\n"
        await msg.edit_text(anim)
        
        req_path = os.path.join(abs_project_dir, "requirements.txt")
        
        # --- AUTO-GENERATE requirements.txt if missing ---
        if not os.path.exists(req_path) and action != "autoheal":
            anim += "📄 <b>AUTO-GENERATING REQUIREMENTS...</b>\n"
            await msg.edit_text(anim)
            try:
                try: stdlib = set(sys.stdlib_module_names)
                except AttributeError: stdlib = {'os', 'sys', 'time', 'datetime', 'json', 're', 'math', 'random', 'asyncio', 'logging', 'sqlite3', 'threading', 'socket', 'urllib', 'hashlib', 'pathlib', 'collections', 'itertools', 'typing', 'subprocess', 'shutil', 'base64'}
                
                imports = set()
                for root, _, files in os.walk(abs_project_dir):
                    if 'venv' in root or '__pycache__' in root or '.git' in root: continue
                    for file in files:
                        if file.endswith('.py'):
                            try:
                                with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                                    tree = ast.parse(f.read())
                                    for node in ast.walk(tree):
                                        if isinstance(node, ast.Import):
                                            for n in node.names: imports.add(n.name.split('.')[0])
                                        elif isinstance(node, ast.ImportFrom):
                                            if node.module: imports.add(node.module.split('.')[0])
                            except Exception: pass
                
                pkg_map = {"dotenv": "python-dotenv", "bs4": "beautifulsoup4", "cv2": "opencv-python", "yaml": "pyyaml", "PIL": "Pillow", "telebot": "pyTelegramBotAPI"}
                final_reqs = [pkg_map.get(imp, imp) for imp in imports if imp not in stdlib and not imp.startswith('_')]
                
                if final_reqs:
                    with open(req_path, "w") as f: f.write("\n".join(final_reqs))
                    anim += "└─ generated successfully ✓\n\n"
                else:
                    anim += "└─ no external dependencies detected ✓\n\n"
                await msg.edit_text(anim)
            except Exception as e:
                anim += f"└─ ⚠️ Auto-generation failed.\n└─ 💡 <b>Suggest:</b> Please create requirements.txt manually and upload.\n\n"
                await msg.edit_text(anim)
                await asyncio.sleep(0.5)

        # Proceed to install requirements.txt if present
        if os.path.exists(req_path) and action != "autoheal":
            anim += "📡 <b>FETCHING REQUIREMENTS...</b>\n└─ installing dependencies...\n"
            await msg.edit_text(anim)
            
            req_proc = await asyncio.create_subprocess_shell(f"{pip_bin} install -r {req_path}")
            await req_proc.wait()
            
            anim += "└─ resolving packages ✓\n\n"
            await msg.edit_text(anim)

        anim += "🛠️ <b>CREATING CONTAINER...</b>\n"
        await msg.edit_text(anim)

        def enforce_ram_limit():
            if HAS_RESOURCE:
                try:
                    max_bytes = int(MAX_RAM_GB * 1024 * 1024 * 1024)
                    resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
                except Exception: pass

        process = await asyncio.create_subprocess_exec(
            os.path.join(venv_dir, "bin", "python"), "-u", rel_path,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=abs_project_dir,
            preexec_fn=enforce_ram_limit if HAS_RESOURCE else None 
        )
        
        anim += "└─ container boot sequence started ✓\n\n"
        await msg.edit_text(anim)
        
        await asyncio.sleep(0.6) 
        if process.returncode is not None:
            anim += f"❌ <u><b>DEPLOYMENT UNSUCCESSFUL</b></u>\n└─ Process crashed instantly (Code: {process.returncode}). View logs to debug."
            return await msg.edit_text(anim)

        ACTIVE_PROCESSES[process_key] = process
        RUNNING_SCRIPTS[process_key] = f"{proj_name}/{rel_path}"
        save_running_state() 
        asyncio.create_task(read_stdout(process, client, chat_id, abs_project_dir, owner_id, rel_path, attempted_modules))

        keyboard = InlineKeyboardMarkup([
            [create_btn("🔄 Refresh Logs", cb="refresh_log_run.log"), create_btn("🔁 Restart", cb=f"rst_{owner_id}@{proj_name}:{rel_path}"[:64])],
            [create_btn("🛑 Stop Process", cb=f"kill_{owner_id}@{proj_name}:{rel_path}"[:64])]
        ])
        
        recent_log = get_recent_logs(abs_project_dir)
        anim += f"🚀 <u><b>DEPLOYMENT SUCCESSFUL ✓</b></u>\n\n<pre><code class='language-bash'>{safe_html_log(recent_log)}</code></pre>"
        await msg.edit_text(anim, reply_markup=keyboard)

    except Exception as e:
        anim += f"\n❌ <u><b>DEPLOYMENT UNSUCCESSFUL</b></u>\n└─ Error: <code>{safe_html_log(e)}</code>"
        await msg.edit_text(anim)

async def restart_process(client, chat_id, initiator_id, active_proj_str, rel_path):
    owner_id, proj_name = resolve_project_owner(initiator_id, active_proj_str)
    process_key = f"{owner_id}:{proj_name}:{rel_path}"
    
    if process_key in ACTIVE_PROCESSES and ACTIVE_PROCESSES[process_key].returncode is None:
        ACTIVE_PROCESSES[process_key].terminate()
        ACTIVE_PROCESSES.pop(process_key, None)
        RUNNING_SCRIPTS.pop(process_key, None)
    
    log_collab_event(owner_id, proj_name, initiator_id, f"Restarted code execution: {rel_path}")
    
    prev_active = db.get_active_project(initiator_id)
    db.set_active_project(initiator_id, active_proj_str)
    await start_process(client, chat_id, initiator_id, rel_path, action="restart")
    db.set_active_project(initiator_id, prev_active)

# ==========================================
# 6. AUTO START PROCESSES ON BOOT
# ==========================================
async def resume_active_processes(client):
    saved_states = load_running_state()
    if not saved_states: return
    print(f"🔄 Found {len(saved_states)} previously active scripts. Resuming...")
    for process_key, script_path in list(saved_states.items()):
        try:
            owner_id_str, proj_name, rel_path = process_key.split(":", 2)
            owner_id = int(owner_id_str)
            prev_active = db.get_active_project(owner_id)
            db.set_active_project(owner_id, proj_name) 
            
            try: await client.send_message(owner_id, f"🔄 <b>System Rebooted:</b> Auto-resuming <code>{proj_name}/{rel_path}</code>...")
            except Exception: pass
            
            await start_process(client, owner_id, owner_id, rel_path)
            db.set_active_project(owner_id, prev_active)
            await asyncio.sleep(0.5) 
        except Exception as e:
            print(f"⚠️ Failed to resume {script_path}: {e}")

# ==========================================
# 7. UI MENUS / CALLBACK HANDLERS
# ==========================================
async def send_main_menu(msg_or_cb):
    user_id = msg_or_cb.from_user.id
    keyboard = InlineKeyboardMarkup([
        [create_btn("📁 My Projects", cb="menu_projects"), create_btn("➕ New Project", cb="menu_new")],
        [create_btn("📂 File Manager", cb="fm_."), create_btn("🤝 Collabs", cb="collab_info")],
        [create_btn("🛑 Stop Process", cb="menu_stop"), create_btn("📜 View Logs", cb="menu_logs")],
        [create_btn("📊 System Vitals", cb="dash_sys"), create_btn("🆘 Help", cb="menu_help")]
    ])
    
    active = db.get_active_project(user_id)
    if active and "@" in active: active_display = f"🤝 {active.split('@')[1]} (Collab)"
    else: active_display = active if active else "None"
        
    running = get_running_scripts(user_id)
    status = f"🗂 <b>Active:</b> {active_display}"
    if running: status += "\n🟢 <b>Running:</b> " + ", ".join(running)

    text = f"🪼 <b>Nex Host Cloud OS</b>\n<blockquote>{status}</blockquote>\n<i>Select an option below:</i>"
    if isinstance(msg_or_cb, Message): await msg_or_cb.reply_text(text, reply_markup=keyboard)
    else: await msg_or_cb.message.edit_text(text, reply_markup=keyboard)

async def render_projects(client, message, user_id, is_cb=True):
    user_dir = get_user_dir(user_id)
    projects = [f for f in os.listdir(user_dir) if os.path.isdir(os.path.join(user_dir, f))] if os.path.exists(user_dir) else []
    
    collabs = get_collabs()
    shared_with_me = []
    for key, users in collabs.items():
        if user_id in users: shared_with_me.append(key)
            
    if not projects and not shared_with_me:
        return await (message.edit_text if is_cb else message.reply_text)("❌ <b>No projects or collabs found.</b> Use /newproject to create one.")

    active = db.get_active_project(user_id)
    keyboard, row = [], []
    running = get_running_scripts(user_id)

    if projects:
        keyboard.append([create_btn("── Your Projects ──", cb="noop")])
        for proj in projects:
            running_icon = "🟢" if any(rs.startswith(f"{proj}/") for rs in running) else "📁"
            is_active = active == proj
            label = f"⭐ {proj}" if is_active else f"{running_icon} {proj}"
            row.append(create_btn(label, cb=f"set_proj_{proj}"[:64]))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row); row = []

    if shared_with_me:
        keyboard.append([create_btn("── Shared With You ──", cb="noop")])
        for shared in shared_with_me:
            owner_str, proj = shared.split(":")
            is_active = active == f"{owner_str}@{proj}"
            label = f"⭐ {proj}" if is_active else f"🤝 {proj}"
            row.append(create_btn(label, cb=f"set_proj_{owner_str}@{proj}"[:64]))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)

    keyboard.append([create_btn("🔄 Refresh", cb="menu_projects"), create_btn("🔙 Back to Menu", cb="menu_main")])
    text = f"🗂 <b>Workspace Selection</b>\n<blockquote>⭐ Active: <code>{active or 'None'}</code></blockquote>"
    await (message.edit_text if is_cb else message.reply_text)(text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_callback_query()
@safe_handler
async def handle_callbacks(client: Client, callback: CallbackQuery):
    user_id, data = callback.from_user.id, callback.data

    if data == "noop":
        return await callback.answer()

    if data == "maint_enable" and user_id in ADMIN_IDS:
        global MAINTENANCE_MODE; MAINTENANCE_MODE = True
        await callback.answer("Maintenance Enabled!")
        await callback.message.edit_text(get_maintenance_text(), reply_markup=get_maintenance_keyboard())
    elif data == "maint_disable" and user_id in ADMIN_IDS:
        MAINTENANCE_MODE = False
        await callback.answer("Maintenance Disabled!")
        await callback.message.edit_text(get_maintenance_text(), reply_markup=get_maintenance_keyboard())
    elif data == "maint_close" and user_id in ADMIN_IDS:
        await callback.answer("Closed!")
        await callback.message.delete()

    elif data.startswith("collab_acc_") or data.startswith("collab_rej_"):
        invite_id = data.split("_")[2]
        if invite_id not in PENDING_INVITES: return await callback.answer("❌ Invite expired or invalid.", show_alert=True)
            
        invite = PENDING_INVITES[invite_id]
        if invite["collab_id"] != user_id: return await callback.answer("❌ This invite is not for you.", show_alert=True)
            
        owner_id, proj_name = invite["owner_id"], invite["proj_name"]
        
        if data.startswith("collab_acc_"):
            collabs = get_collabs()
            key = f"{owner_id}:{proj_name}"
            if key not in collabs: collabs[key] = []
            if user_id not in collabs[key]: collabs[key].append(user_id)
            save_collabs(collabs)
            await callback.answer("✅ Invite Accepted!")
            await callback.message.edit_text(f"✅ <b>You accepted the invite! You are now a collaborator on <code>{proj_name}</code>.</b>")
            try: await client.send_message(owner_id, f"✅ User <code>{user_id}</code> accepted your invite to <code>{proj_name}</code>.")
            except: pass
        else:
            await callback.answer("❌ Invite Rejected!")
            await callback.message.edit_text(f"❌ <b>You rejected the invite for <code>{proj_name}</code>.</b>")
            try: await client.send_message(owner_id, f"❌ User <code>{user_id}</code> rejected your invite to <code>{proj_name}</code>.")
            except: pass
        del PENDING_INVITES[invite_id]

    elif data == "collab_info":
        active_proj_str = db.get_active_project(user_id)
        if not active_proj_str: return await callback.answer("❌ No active project.", show_alert=True)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        collabs = get_collabs()
        collab_users = collabs.get(f"{owner_id}:{proj_name}", [])
        
        text = f"👥 <b>Collab Info:</b> <code>{proj_name}</code>\n👑 <b>Owner:</b> <code>{owner_id}</code>\n\n<b>Collaborators:</b>\n"
        if collab_users:
            for cu in collab_users: text += f" ├─ <code>{cu}</code>\n"
        else: text += "<i>(None)</i>"
        
        await callback.answer("Viewing Collab Info")
        await callback.message.reply_text(text)

    elif data == "menu_main": 
        await callback.answer("Opening Main Menu...")
        await send_main_menu(callback)
        
    elif data == "menu_projects": 
        await callback.answer("Loading Projects...")
        await render_projects(client, callback.message, user_id, True)
        
    elif data == "menu_new": 
        await callback.answer("Creating New Project...")
        await callback.message.edit_text("➕ Send <code>/newproject [name]</code> or <code>/clone [URL]</code>")

    # FILE EXPLORER
    elif data.startswith("fm_") and "mkdir" not in data:
        rel_path = data[3:]
        active_proj_str = db.get_active_project(user_id)
        if not active_proj_str: return await callback.answer("❌ No active project!", show_alert=True)
        
        USER_CWD[user_id] = rel_path 
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        target_dir = os.path.join(get_project_dir(owner_id, proj_name), rel_path) if rel_path != "." else get_project_dir(owner_id, proj_name)
        if not os.path.exists(target_dir): return await callback.answer("❌ Invalid path.", show_alert=True)

        items = sorted(os.listdir(target_dir))
        keyboard = []
        for item in items:
            if item in ['venv', '__pycache__', '.git'] or (item.startswith('.') and not item.endswith('.session')): continue
            
            if item == "collab_audit.log":
                collabs = get_collabs()
                if owner_id == user_id and not collabs.get(f"{owner_id}:{proj_name}"): continue
            
            item_rel = os.path.join(rel_path, item).replace("\\", "/") if rel_path != "." else item
            is_dir = os.path.isdir(os.path.join(target_dir, item))
            
            icon = "📁" if is_dir else get_file_icon(item)
            if not is_dir and f"{owner_id}:{proj_name}:{item_rel}" in ACTIVE_PROCESSES: 
                icon = "🟢"
                
            keyboard.append([create_btn(f"{icon} {item}", cb=f"{'fm' if is_dir else 'fl'}_{item_rel}"[:64])])
                
        if rel_path == ".": 
            keyboard.append([create_btn("➕ New Folder", cb="fm_mkdir_."), create_btn("👥 Collab Info", cb="collab_info")])
            keyboard.append([create_btn("🔙 Back to Projects", cb="menu_projects")]) 
        else: 
            keyboard.append([create_btn("➕ New Folder", cb=f"fm_mkdir_{rel_path}"[:64])])
            parent = os.path.dirname(rel_path) or "."
            keyboard.append([create_btn("🔙 Back", cb=f"fm_{parent}"[:64]), create_btn("🗑 Delete Folder", cb=f"deldir_{rel_path}"[:64])])
            
        keyboard.append([create_btn("🏠 Main Menu", cb="menu_main")])
        
        collab_tag = "🤝 Collab" if owner_id != user_id else "Personal"
        await callback.answer("Folder loaded!")
        await callback.message.edit_text(f"📂 <b>Nex Host Explorer:</b> <code>{proj_name}/{rel_path}</code> ({collab_tag})", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("fm_mkdir_"):
        rel_path = data.split("fm_mkdir_")[1]
        await callback.answer("Awaiting folder name...")
        await callback.message.edit_text(f"📁 <b>Create Directory:</b>\n\nSend: <code>/mkdir {rel_path if rel_path != '.' else ''}/FolderName</code>")

    elif data.startswith("fl_"):
        rel_path = data[3:]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        process_key = f"{owner_id}:{proj_name}:{rel_path}"
        
        keyboard = []
        if process_key in ACTIVE_PROCESSES:
            keyboard.append([create_btn("🛑 Stop File", cb=f"kill_{owner_id}@{proj_name}:{rel_path}"[:64]), create_btn("🔁 Restart", cb=f"rst_{owner_id}@{proj_name}:{rel_path}"[:64])])
        else:
            keyboard.append([create_btn("▶️ Run File", cb=f"run_file_{rel_path}"[:64])])
            
        if rel_path.endswith("requirements.txt"):
            keyboard.insert(0, [create_btn("📦 Install Deps", cb=f"req_inst_{rel_path}"[:64])])

        if rel_path == "collab_audit.log" and owner_id != user_id:
            keyboard.extend([
                [create_btn("📖 View", cb=f"viewfl_{rel_path}"[:64]), create_btn("📥 Download", cb=f"dlfl_{rel_path}"[:64])],
                [create_btn("🔙 Back", cb=f"fm_{os.path.dirname(rel_path) or '.'}"[:64])]
            ])
        else:
            keyboard.extend([
                [create_btn("📖 View", cb=f"viewfl_{rel_path}"[:64]), create_btn("✏️ Edit", cb=f"editfl_{rel_path}"[:64])],
                [create_btn("🏷 Rename", cb=f"rnfl_{rel_path}"[:64]), create_btn("🗑 Delete", cb=f"delfl_{rel_path}"[:64])], 
                [create_btn("📥 Download", cb=f"dlfl_{rel_path}"[:64]), create_btn("🔙 Back", cb=f"fm_{os.path.dirname(rel_path) or '.'}"[:64])]
            ])
        await callback.answer("File selected!")
        await callback.message.edit_text(f"📄 <b>File:</b> <code>{rel_path}</code>\n\n<i>Choose an action below:</i>", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("req_inst_"):
        rel_path = data.split("req_inst_")[1]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        req_path = os.path.join(get_project_dir(owner_id, proj_name), rel_path)
        venv_pip = os.path.join(get_project_dir(owner_id, proj_name), "venv", "bin", "pip")
        
        await callback.answer("⏳ Installing dependencies...", show_alert=False)
        msg = await callback.message.reply_text("⏳ <b>Installing...</b>")
        await (await asyncio.create_subprocess_shell(f"{venv_pip} install -r {req_path}")).wait()
        await msg.edit_text("✅ <b>Dependencies installed!</b>")

    # FILE VIEWER & EDITOR 
    elif data.startswith("viewfl_"):
        rel_path = data.split("viewfl_")[1]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        filepath = os.path.join(get_project_dir(owner_id, proj_name), rel_path)
        
        if not os.path.exists(filepath): return await callback.answer("File not found.", show_alert=True)
        with open(filepath, "r", encoding="utf-8") as f: content = f.read()
        
        if not content.strip(): content = "(Empty File)"
        chunks = [content[i:i+3500] for i in range(0, len(content), 3500)]
        VIEW_STATES[user_id] = {"rel_path": rel_path, "chunks": chunks, "page": 0}
        await callback.answer("Opening file viewer...")
        await render_view_page(callback, user_id)

    elif data in ["view_prev", "view_next", "view_close"]:
        state = VIEW_STATES.get(user_id)
        if not state: return await callback.answer("Session expired.", show_alert=True)
        if data == "view_close":
            del VIEW_STATES[user_id]
            await callback.answer("Viewer closed.")
            return await callback.message.delete()
        
        if data == "view_prev" and state["page"] > 0: state["page"] -= 1
        elif data == "view_next" and state["page"] < len(state["chunks"]) - 1: state["page"] += 1
            
        await callback.answer("Changing page...")
        await render_view_page(callback, user_id)

    elif data.startswith("editfl_"):
        rel_path = data.split("editfl_")[1]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        
        if rel_path == "collab_audit.log" and owner_id != user_id: return await callback.answer("❌ Cannot edit audit log.", show_alert=True)
            
        filepath = os.path.join(get_project_dir(owner_id, proj_name), rel_path)
        if not os.path.exists(filepath): return await callback.answer("File not found.", show_alert=True)
            
        EDIT_STATES[user_id] = {"path": filepath, "rel_path": rel_path, "owner_id": owner_id, "proj_name": proj_name}
        await callback.answer("Ready for edit.")
        await callback.message.reply_text(
            f"✏️ <b>Editing:</b> <code>{rel_path}</code>\n\n"
            "Send the <b>new code/content</b> for this file here in the chat.\n"
            "<i>(Note: This will overwrite the entire file)</i>\n\n"
            "Send <code>/cancel</code> to abort editing."
        )

    elif data.startswith("rnfl_"):
        rel_path = data.split("rnfl_")[1]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        
        if rel_path == "collab_audit.log" and owner_id != user_id: return await callback.answer("❌ Cannot rename audit log.", show_alert=True)
            
        filepath = os.path.join(get_project_dir(owner_id, proj_name), rel_path)
        RENAME_STATES[user_id] = {"path": filepath, "rel_path": rel_path, "owner_id": owner_id, "proj_name": proj_name}
        await callback.answer("Ready for rename.")
        await callback.message.reply_text(
            f"🏷 <b>Renaming:</b> <code>{rel_path}</code>\n\n"
            "Send the <b>new name</b> for this file here in the chat.\n"
            "Send <code>/cancel</code> to abort."
        )

    elif data.startswith("run_file_"):
        await callback.answer("Starting Process...", show_alert=False)
        await callback.message.delete()
        await start_process(client, callback.message.chat.id, user_id, data.split("run_file_")[1], action="deploy")
        
    elif data.startswith("rst_"):
        parts = data.split("rst_")[1].split(":")
        await callback.answer("Restarting Process...", show_alert=False)
        await callback.message.delete()
        await restart_process(client, callback.message.chat.id, user_id, parts[0], parts[1])

    elif data == "menu_stop":
        active_proj_str = db.get_active_project(user_id)
        if not active_proj_str: return await callback.answer("❌ No active project.", show_alert=True)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        
        running_keys = [k for k, p in ACTIVE_PROCESSES.items() if k.startswith(f"{owner_id}:{proj_name}:") and p.returncode is None]
        if not running_keys: return await callback.answer("⚠️ No processes are running in this workspace.", show_alert=True)
            
        keyboard = []
        for k in running_keys:
            path = k.split(":", 2)[2]
            keyboard.append([create_btn(f"🛑 Stop {path}", cb=f"kill_{owner_id}@{proj_name}:{path}"[:64])])
        keyboard.append([create_btn("🔙 Back to Menu", cb="menu_main")])
        
        await callback.answer("Select process to stop...")
        await callback.message.edit_text("🛑 <b>Select a process to stop:</b>", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("kill_"):
        parts = data.split("_", 1)[1].split(":")
        owner_id, proj_name = resolve_project_owner(user_id, parts[0])
        process_key = f"{owner_id}:{proj_name}:{parts[1]}"
        
        if process_key in ACTIVE_PROCESSES:
            await callback.answer("Killing process...", show_alert=False)
            anim = "🛑 <b>STOPPING CONTAINER...</b>\n"
            await callback.message.edit_text(anim)
            await asyncio.sleep(0.3)
            anim += "└─ sending termination signals...\n"
            await callback.message.edit_text(anim)
            
            ACTIVE_PROCESSES[process_key].terminate()
            ACTIVE_PROCESSES.pop(process_key, None)
            RUNNING_SCRIPTS.pop(process_key, None)
            save_running_state()
            log_collab_event(owner_id, proj_name, user_id, f"Stopped execution: {parts[1]}")
            
            await asyncio.sleep(0.3)
            anim += "└─ process killed ✓\n\n📴 <b>STOPPED SUCCESSFULLY ✓</b>"
            await callback.message.edit_text(anim)
            await asyncio.sleep(1.5)
            
            callback.data = "menu_main"
            await handle_callbacks(client, callback)
        else: await callback.answer("⚠️ Not running.", show_alert=True)

    elif data.startswith("delfl_"):
        rel_path = data.split("delfl_")[1]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        
        if rel_path == "collab_audit.log" and owner_id != user_id: return await callback.answer("❌ Only the owner can delete this.", show_alert=True)
            
        filepath = os.path.join(get_project_dir(owner_id, proj_name), rel_path)
        if os.path.exists(filepath):
            os.remove(filepath)
            log_collab_event(owner_id, proj_name, user_id, f"Deleted file: {rel_path}")
            await callback.answer("✅ Deleted!", show_alert=True)
        else: await callback.answer("❌ Not found.", show_alert=True)
        callback.data = f"fm_{os.path.dirname(rel_path) or '.'}"
        await handle_callbacks(client, callback)

    elif data.startswith("dlfl_"):
        rel_path = data.split("dlfl_")[1]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        filepath = os.path.join(get_project_dir(owner_id, proj_name), rel_path)
        if os.path.exists(filepath):
            await callback.answer("Sending document...")
            await callback.message.reply_document(document=filepath)
        else: await callback.answer("❌ File not found.", show_alert=True)
        
    elif data.startswith("deldir_"):
        rel_path = data.split("deldir_")[1]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        dirpath = os.path.join(get_project_dir(owner_id, proj_name), rel_path)
        if os.path.exists(dirpath):
            shutil.rmtree(dirpath)
            log_collab_event(owner_id, proj_name, user_id, f"Deleted folder: {rel_path}")
            await callback.answer("✅ Folder deleted!", show_alert=True)
        else: await callback.answer("❌ Not found.", show_alert=True)
        callback.data = f"fm_{os.path.dirname(rel_path) or '.'}"
        await handle_callbacks(client, callback)

    # DASHBOARD
    elif data.startswith("dash_"):
        if user_id not in ADMIN_IDS: return await callback.answer("❌ Admin Only", show_alert=True)
        view = "system" if "sys" in data else "files"
        await callback.answer("Loading dashboard...")
        try: await callback.message.edit_text(await generate_dashboard_text(client, view=view), reply_markup=InlineKeyboardMarkup([
            [create_btn("⚙️ System", cb="dash_sys"), create_btn("📂 File Manager", cb="dash_files")],
            [create_btn("🔄 Refresh", cb=f"dash_refresh_{view}")], [create_btn("🔙 Main Menu", cb="menu_main")]
        ]))
        except: pass

    elif data == "menu_logs":
        active_proj_str = db.get_active_project(user_id)
        if not active_proj_str: return await callback.answer("❌ No active project!", show_alert=True)
        await callback.answer("Loading log options...")
        await callback.message.edit_text("📜 <b>Select Log Type:</b>", reply_markup=InlineKeyboardMarkup([
            [create_btn("📜 System Log (run.log)", cb="refresh_log_run.log")],
            [create_btn("🤝 Collab Audit Logs", cb="refresh_log_collab_audit.log")],
            [create_btn("🔙 Menu", cb="menu_main")]
        ]))

    elif data.startswith("refresh_log_"):
        log_filename = data.split("refresh_log_")[1]
        active_proj_str = db.get_active_project(user_id)
        owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
        log_file = os.path.join(get_project_dir(owner_id, proj_name), log_filename)
        
        if not os.path.exists(log_file): return await callback.answer("❌ Log file empty or missing.", show_alert=True)
        with open(log_file, "r", encoding="utf-8") as f: lines = f.readlines()
        tail = "".join(lines[-25:]) or "(Empty)"
        
        try:
            header_text = f"📜 <b>Logs for</b> <code>{log_filename}</code>:\n"
            log_body = safe_html_log(tail[-3800:])
            final_text = f"{header_text}<pre><code class='language-bash'>{log_body}</code></pre>"
            
            await callback.message.edit_text(final_text, reply_markup=InlineKeyboardMarkup([
                [create_btn("🔄 Refresh", cb=f"refresh_log_{log_filename}"), create_btn("🔙 Back", cb="menu_logs")]
            ]))
            await callback.answer("✅ Refreshed!")
        except: await callback.answer("⚠️ No new logs.", show_alert=False)

    elif data == "status_refresh":
        await callback.answer("Refreshed!")
        try:
            await callback.message.edit_text(await build_status_text(user_id), reply_markup=InlineKeyboardMarkup([
                [create_btn("🔄 Refresh", cb="status_refresh"), create_btn("🗂 Switch Project", cb="menu_projects")]
            ]))
        except Exception: pass

    elif data == "menu_help":
        txt = build_help_text(user_id)
        await callback.answer("Loading Help Guide...")
        await callback.message.edit_text(txt, reply_markup=InlineKeyboardMarkup([[create_btn("🔙 Back", cb="menu_main")]]))

    elif data.startswith("set_proj_"):
        db.set_active_project(user_id, data.split("set_proj_")[1])
        await callback.answer(f"Workspace set to {data.split('set_proj_')[1]}")
        await callback.message.edit_text(f"✅ <b>Workspace Set:</b> <code>{data.split('set_proj_')[1]}</code>")

# ==========================================
# 8. ALL BOT COMMANDS
# ==========================================
@app.on_message(filters.command("start"))
@safe_handler
async def start_cmd(client: Client, message: Message): await send_main_menu(message)

@app.on_message(filters.command("help"))
@safe_handler
async def help_cmd(client: Client, message: Message):
    txt = build_help_text(message.from_user.id)
    await message.reply_text(txt, reply_markup=InlineKeyboardMarkup([[create_btn("🔙 Back to Menu", cb="menu_main")]]))

@app.on_message(filters.command("maintenance") & filters.user(ADMIN_IDS))
@safe_handler
async def cmd_maintenance(client: Client, message: Message): await message.reply_text(get_maintenance_text(), reply_markup=get_maintenance_keyboard())

@app.on_message(filters.command("backup") & filters.user(ADMIN_IDS))
@safe_handler
async def manual_backup_cmd(client: Client, message: Message):
    msg = await message.reply_text("⏳ <b>Generating Cloud Backup...</b>")
    await perform_backup(client, msg)

@app.on_message(filters.command("setbackup") & filters.user(ADMIN_IDS))
@safe_handler
async def set_backup_cmd(client: Client, message: Message):
    global BACKUP_INTERVAL_MINS
    if len(message.command) < 2 or not message.command[1].isdigit(): return await message.reply_text("Usage: <code>/setbackup [minutes]</code>")
    BACKUP_INTERVAL_MINS = int(message.command[1])
    with open("backup_config.json", "w") as f: json.dump({"interval": BACKUP_INTERVAL_MINS}, f)
    await message.reply_text(f"✅ <b>Auto-Backup interval set to {BACKUP_INTERVAL_MINS} minutes.</b>")

@app.on_message(filters.command(["dashboard", "stats"]) & filters.user(ADMIN_IDS))
@safe_handler
async def cloud_os_dashboard(client: Client, message: Message):
    msg = await message.reply_text("🔄 Fetching System Vitals...")
    await msg.edit_text(await generate_dashboard_text(client, "system"), reply_markup=InlineKeyboardMarkup([[create_btn("⚙️ System", cb="dash_sys"), create_btn("📂 File Manager", cb="dash_files")], [create_btn("🔄 Refresh", cb="dash_refresh_system")]]))

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_IDS))
@safe_handler
async def admin_broadcast(client: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/broadcast [msg]</code>")
    text = message.text.split(None, 1)[1]
    for u in db.get_all_users():
        try: await client.send_message(u, f"📢 <b>Nex Host Broadcast</b>\n\n{text}")
        except Exception: pass
    await message.reply_text("✅ <b>Broadcast sent.</b>")

@app.on_message(filters.command("admin_active") & filters.user(ADMIN_IDS))
@safe_handler
async def admin_active_cmd(client: Client, message: Message):
    running = [k for k, p in ACTIVE_PROCESSES.items() if p.returncode is None]
    if not running: return await message.reply_text("⚠️ <b>No processes are running globally.</b>")
    text = "🌐 <b>Global Running Processes:</b>\n\n"
    for i, k in enumerate(running):
        uid, proj, path = k.split(":", 2)
        text += f"{i+1}. 👤 <code>{uid}</code> | 📁 <code>{proj}</code> | 📄 <code>{path}</code>\n   🛑 <code>/admin_stop {uid} {proj} {path}</code>\n\n"
    await message.reply_text(text)

@app.on_message(filters.command("admin_stop") & filters.user(ADMIN_IDS))
@safe_handler
async def admin_stop_cmd(client: Client, message: Message):
    if len(message.command) < 4: return await message.reply_text("Usage: <code>/admin_stop [uid] [project] [file]</code>")
    process_key = f"{message.command[1]}:{message.command[2]}:{message.command[3]}"
    if process_key in ACTIVE_PROCESSES and ACTIVE_PROCESSES[process_key].returncode is None:
        ACTIVE_PROCESSES[process_key].terminate()
        ACTIVE_PROCESSES.pop(process_key, None)
        RUNNING_SCRIPTS.pop(process_key, None)
        save_running_state()
        await message.reply_text(f"✅ 🛑 <b>Stopped <code>{message.command[3]}</code> for User <code>{message.command[1]}</code>.</b>")
    else: await message.reply_text("❌ <b>Process not found.</b>")

@app.on_message(filters.command("admin_projects") & filters.user(ADMIN_IDS))
@safe_handler
async def admin_projects_cmd(client: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/admin_projects [user_id]</code>")
    target_uid = message.command[1]
    user_dir = get_user_dir(target_uid)
    projects = [f for f in os.listdir(user_dir) if os.path.isdir(os.path.join(user_dir, f))] if os.path.exists(user_dir) else []
    if not projects: return await message.reply_text("❌ <b>User has no projects.</b>")
    text = f"🗂 <b>Projects for User <code>{target_uid}</code>:</b>\n\n"
    for p in projects: text += f"📁 <code>{p}</code>\n   🗑 <code>/admin_deleteproj {target_uid} {p}</code>\n\n"
    await message.reply_text(text)

@app.on_message(filters.command("admin_deleteproj") & filters.user(ADMIN_IDS))
@safe_handler
async def admin_deleteproj_cmd(client: Client, message: Message):
    if len(message.command) < 3: return await message.reply_text("Usage: <code>/admin_deleteproj [user_id] [project]</code>")
    target_uid, target_proj = message.command[1], message.command[2]
    project_dir = get_project_dir(target_uid, target_proj)
    if os.path.exists(project_dir):
        shutil.rmtree(project_dir)
        await message.reply_text(f"✅ <b>Project <code>{target_proj}</code> of User <code>{target_uid}</code> deleted.</b>")
    else: await message.reply_text("❌ <b>Project not found.</b>")

@app.on_message(filters.command("collabs"))
@safe_handler
async def view_collabs(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    
    collabs = get_collabs()
    collab_users = collabs.get(f"{owner_id}:{proj_name}", [])
    text = f"👥 <b>Collaboration Info:</b> {proj_name}\n<blockquote>👑 <b>Owner:</b> <code>{owner_id}</code>\n"
    if collab_users:
        text += "🤝 <b>Collaborators:</b>\n"
        for cu in collab_users: text += f" ├─ <code>{cu}</code>\n"
    else: text += "<i>(No collaborators added)</i>\n"
    text += "</blockquote>"
    await message.reply_text(text)

@app.on_message(filters.command("addcollab"))
@safe_handler
async def add_collab(client: Client, message: Message):
    if len(message.command) < 3: return await message.reply_text("Usage: <code>/addcollab [project_name] [user_id]</code>")
    proj_name = message.command[1]
    user_id = message.from_user.id
    
    try: collab_id = int(message.command[2])
    except ValueError: return await message.reply_text("❌ <b>Invalid User ID.</b>")

    if collab_id == user_id: return await message.reply_text("❌ <b>You can't invite yourself.</b>")
    if not os.path.exists(get_project_dir(user_id, proj_name)): return await message.reply_text("❌ <b>Project not found.</b>")

    collabs = get_collabs()
    if collab_id in collabs.get(f"{user_id}:{proj_name}", []):
        return await message.reply_text(f"⚠️ <code>{collab_id}</code> <b>is already a collaborator on this project.</b>")
    if any(inv["collab_id"] == collab_id and inv["owner_id"] == user_id and inv["proj_name"] == proj_name for inv in PENDING_INVITES.values()):
        return await message.reply_text(f"⏳ <b>An invite is already pending for <code>{collab_id}</code>.</b>")

    invite_id = uuid.uuid4().hex[:8]
    PENDING_INVITES[invite_id] = {"collab_id": collab_id, "owner_id": user_id, "proj_name": proj_name}
    kb = InlineKeyboardMarkup([
        [create_btn("✅ Accept", cb=f"collab_acc_{invite_id}"), create_btn("❌ Reject", cb=f"collab_rej_{invite_id}")]
    ])
    try:
        await client.send_message(collab_id, f"🤝 <b>Collaboration Invite!</b>\n\nUser <code>{user_id}</code> has invited you to join their workspace: <code>{proj_name}</code>.", reply_markup=kb)
        await message.reply_text(f"⏳ <b>Invitation sent to <code>{collab_id}</code> pending acceptance.</b>")
    except Exception as e: await message.reply_text(f"❌ <b>Could not send invite:</b>\n<code>{safe_html_log(e)}</code>")

@app.on_message(filters.command("remcollab"))
@safe_handler
async def rem_collab(client: Client, message: Message):
    if len(message.command) < 3: return await message.reply_text("Usage: <code>/remcollab [project_name] [user_id]</code>")
    collabs = get_collabs()
    key = f"{message.from_user.id}:{message.command[1]}"
    if key in collabs and int(message.command[2]) in collabs[key]:
        collabs[key].remove(int(message.command[2]))
        save_collabs(collabs)
        await message.reply_text(f"🚫 <b>User <code>{message.command[2]}</code> removed from <code>{message.command[1]}</code>.</b>")
    else: await message.reply_text("❌ <b>User is not a collaborator.</b>")

@app.on_message(filters.command("mycollabs"))
@safe_handler
async def my_collabs_cmd(client: Client, message: Message):
    """ Lists every workspace that's been shared WITH this user (across all owners). """
    user_id = message.from_user.id
    collabs = get_collabs()
    shared_with_me = [key for key, users in collabs.items() if user_id in users]

    if not shared_with_me:
        return await message.reply_text("🤝 <b>No workspaces have been shared with you yet.</b>")

    text = "🤝 <b>Workspaces Shared With You</b>\n<blockquote>"
    keyboard = []
    for key in shared_with_me:
        owner_id, proj = key.split(":", 1)
        text += f"📁 {proj} (owner: <code>{owner_id}</code>)\n"
        keyboard.append([create_btn(f"➡️ Switch to {proj}", cb=f"set_proj_{owner_id}@{proj}"[:64])])
    text += "</blockquote>"
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.command("leavecollab"))
@safe_handler
async def leave_collab_cmd(client: Client, message: Message):
    """ Lets a collaborator remove themselves from the currently active shared workspace. """
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str or "@" not in active_proj_str:
        return await message.reply_text("❌ <b>Your active workspace isn't a shared one.</b> Use <code>/myprojects</code> to switch into one first.")

    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    collabs = get_collabs()
    key = f"{owner_id}:{proj_name}"
    if key in collabs and user_id in collabs[key]:
        collabs[key].remove(user_id)
        save_collabs(collabs)
        db.set_active_project(user_id, None)
        log_collab_event(owner_id, proj_name, user_id, "Left the collaboration")
        await message.reply_text(f"🚪 <b>You left <code>{proj_name}</code>.</b>")
        try: await client.send_message(owner_id, f"🚪 User <code>{user_id}</code> left <code>{proj_name}</code>.")
        except Exception: pass
    else:
        await message.reply_text("❌ <b>You're not a collaborator on this project.</b>")

@app.on_message(filters.command("newproject"))
@safe_handler
async def create_project(client: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/newproject [name]</code>")
    proj = message.command[1]
    dir_path = get_project_dir(message.from_user.id, proj)
    if os.path.exists(dir_path): return await message.reply_text("⚠️ <b>Project exists!</b>")
    os.makedirs(dir_path, exist_ok=True)
    await (await asyncio.create_subprocess_shell(f"python3 -m venv {os.path.join(dir_path, 'venv')}")).wait()
    db.set_active_project(message.from_user.id, proj)
    await message.reply_text(f"✅ <b>Nex Host Project <code>{proj}</code> created!</b>")

@app.on_message(filters.command(["deleteproject", "rmproject"]))
@safe_handler
async def cmd_delete_project(client: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/deleteproject [name]</code>")
    project_name = message.command[1]
    user_id = message.from_user.id
    project_dir = get_project_dir(user_id, project_name)
    if os.path.exists(project_dir) and os.path.isdir(project_dir):
        to_kill = [k for k in ACTIVE_PROCESSES.keys() if k.startswith(f"{user_id}:{project_name}:")]
        for k in to_kill:
            if ACTIVE_PROCESSES[k].returncode is None: ACTIVE_PROCESSES[k].terminate()
            ACTIVE_PROCESSES.pop(k, None)
            RUNNING_SCRIPTS.pop(k, None)
        if to_kill: save_running_state()
        shutil.rmtree(project_dir)
        collabs = get_collabs()
        if f"{user_id}:{project_name}" in collabs:
            del collabs[f"{user_id}:{project_name}"]
            save_collabs(collabs)
        if db.get_active_project(user_id) == project_name: db.set_active_project(user_id, None)
        await message.reply_text(f"✅ <b>Project <code>{project_name}</code> deleted.</b>")
    else: await message.reply_text("❌ <b>Project not found.</b>")

@app.on_message(filters.command(["deletefile", "rmfile"]))
@safe_handler
async def delete_file_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/deletefile [path/to/file]</code>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    rel_path = message.command[1]
    if rel_path == "collab_audit.log" and user_id != owner_id: return await message.reply_text("❌ <b>Only owner can delete audit log.</b>")
    filepath = os.path.join(get_project_dir(owner_id, proj_name), rel_path)
    if os.path.exists(filepath):
        os.remove(filepath)
        log_collab_event(owner_id, proj_name, user_id, f"Deleted file via command: {rel_path}")
        await message.reply_text(f"✅ <b>Deleted <code>{rel_path}</code>.</b>")
    else: await message.reply_text("❌ <b>File not found.</b>")

@app.on_message(filters.command("myprojects"))
@safe_handler
async def my_projects_cmd(client: Client, message: Message):
    await render_projects(client, message, message.from_user.id, is_cb=False)

@app.on_message(filters.command("clone"))
@safe_handler
async def clone_repo(client: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/clone [url]</code>")
    url = message.command[1].split("/tree/")[0]
    name = re.sub(r'[^a-zA-Z0-9\-]', '-', url.rstrip("/").split("/")[-1].replace(".git", ""))
    project_dir = get_project_dir(message.from_user.id, name)
    if os.path.exists(project_dir): return await message.reply_text(f"⚠️ <b>Project <code>{name}</code> exists!</b>")
    msg = await message.reply_text(f"⏳ <b>Cloning <code>{name}</code>...</b>")
    proc = await asyncio.create_subprocess_shell(f"git clone {url} {project_dir}", stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0: return await msg.edit_text(f"❌ <b>Failed.</b>\n<code>{safe_html_log(stderr.decode('utf-8').strip())}</code>")
    await msg.edit_text(f"⏳ <b>Creating virtual environment...</b>")
    await (await asyncio.create_subprocess_shell(f"python3 -m venv {os.path.join(project_dir, 'venv')}")).wait()
    db.set_active_project(message.from_user.id, name)
    await msg.edit_text(f"✅ <b>Cloned <code>{name}</code>!</b>")

@app.on_message(filters.command("run"))
@safe_handler
async def run_project(client: Client, message: Message):
    await start_process(client, message.chat.id, message.from_user.id, message.command[1] if len(message.command) > 1 else "main.py", action="deploy")

@app.on_message(filters.command("restart"))
@safe_handler
async def restart_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/restart [filename.py]</code>")
    await restart_process(client, message.chat.id, user_id, active_proj_str, message.command[1])

@app.on_message(filters.command("stop"))
@safe_handler
async def stop_process_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    
    running_keys = [k for k, p in ACTIVE_PROCESSES.items() if k.startswith(f"{owner_id}:{proj_name}:") and p.returncode is None]
    if not running_keys: return await message.reply_text("⚠️ <b>No processes are running in this workspace.</b>")
        
    if len(message.command) > 1:
        target = message.command[1]
        matches = [k for k in running_keys if k.endswith(f":{target}")]
        if not matches: return await message.reply_text(f"❌ <b>No running process found matching <code>{target}</code>.</b>")
        
        msg = await message.reply_text("🛑 <b>STOPPING CONTAINER...</b>\n")
        await asyncio.sleep(0.3)
        anim = "🛑 <b>STOPPING CONTAINER...</b>\n└─ sending termination signals...\n"
        await msg.edit_text(anim)
        
        for m in matches:
            ACTIVE_PROCESSES[m].terminate()
            ACTIVE_PROCESSES.pop(m, None)
            RUNNING_SCRIPTS.pop(m, None)
        save_running_state()
        log_collab_event(owner_id, proj_name, user_id, f"Stopped execution: {target}")
        
        await asyncio.sleep(0.3)
        anim += "└─ process killed ✓\n\n📴 <b>STOPPED SUCCESSFULLY ✓</b>"
        return await msg.edit_text(anim)
        
    keyboard = []
    for k in running_keys:
        path = k.split(":", 2)[2]
        keyboard.append([create_btn(f"🛑 Stop {path}", cb=f"kill_{owner_id}@{proj_name}:{path}"[:64])])
    keyboard.append([create_btn("🔙 Cancel", cb="menu_main")])
    await message.reply_text("🛑 <b>Select process to sto stop:</b>", reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.command("input"))
@safe_handler
async def send_input(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    
    running = [k for k, p in ACTIVE_PROCESSES.items() if k.startswith(f"{owner_id}:{proj_name}:") and p.returncode is None]
    if not running: return await message.reply_text("❌ <b>No processes are running in this workspace.</b>")
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/input [text]</code> or <code>/input [filename.py] [text]</code>")
    
    target_process = None
    if len(running) == 1:
        target_process = ACTIVE_PROCESSES[running[0]]
        input_text = message.text.split(None, 1)[1]
    else:
        arg1 = message.command[1]
        matches = [k for k in running if k.endswith(f":{arg1}")]
        if matches and len(message.command) > 2:
            target_process = ACTIVE_PROCESSES[matches[0]]
            input_text = message.text.split(None, 2)[2]
        else: return await message.reply_text("⚠️ <b>Multiple processes running. Please specify: <code>/input [filename.py] [text]</code></b>")
    
    target_process.stdin.write((input_text + "\n").encode('utf-8'))
    await target_process.stdin.drain()
    await message.reply_text(f"⌨️ <b>Sent input successfully.</b>")

@app.on_message(filters.command("logs"))
@safe_handler
async def check_logs(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project selected.</b>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)

    log_filename = message.command[1] if len(message.command) > 1 else "run.log"
    log_file = os.path.join(get_project_dir(owner_id, proj_name), log_filename)

    if not os.path.exists(log_file): return await message.reply_text(f"❌ <b>No logs found in <code>{log_filename}</code>.</b>")

    with open(log_file, "r", encoding="utf-8") as f: lines = f.readlines()
    tail = "".join(lines[-25:]) or "(Empty)"
    
    kb = InlineKeyboardMarkup([[create_btn("🔄 Refresh Logs", cb=f"refresh_log_{log_filename}")]])
    header_text = f"📜 <b>Logs for</b> <code>{log_filename}</code>:\n"
    log_body = safe_html_log(tail[-3800:])
    final_text = f"{header_text}<pre><code class='language-bash'>{log_body}</code></pre>"
    await message.reply_text(final_text, reply_markup=kb)

@app.on_message(filters.command(["export", "backup_proj", "download"]))
@safe_handler
async def export_project(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project selected.</b>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    project_dir = get_project_dir(owner_id, proj_name)
    zip_path = f"{proj_name}.zip"
    msg = await message.reply_text("⏳ <b>Creating ZIP...</b>")
    
    def _export_zip():
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(project_dir):
                if 'venv' in root or '__pycache__' in root or '.git' in root: continue
                for file in files: zipf.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), project_dir))
    await asyncio.to_thread(_export_zip)
    await msg.edit_text("📤 <b>Uploading...</b>")
    await message.reply_document(document=zip_path, caption=f"📦 <b>Workspace Backup:</b> <code>{proj_name}</code>")
    os.remove(zip_path)

@app.on_message(filters.command("import"))
@safe_handler
async def import_project(client: Client, message: Message):
    await message.reply_text("📥 <b>How to Import:</b>\n1️⃣ Use <code>/newproject [name]</code>\n2️⃣ Send me the <code>.zip</code> file directly in chat.")

@app.on_message(filters.command("rename"))
@safe_handler
async def rename_item(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    if len(message.command) < 3: return await message.reply_text("Usage: <code>/rename [old] [new]</code>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    old_name, new_name = message.command[1], message.command[2]
    
    if old_name == "collab_audit.log" and owner_id != user_id: return await message.reply_text("❌ <b>Only owner can rename audit log.</b>")
    if ".." in old_name or ".." in new_name: return await message.reply_text("❌ <b>Invalid path.</b>")
    
    project_dir = get_project_dir(owner_id, proj_name)
    old_path = os.path.join(project_dir, old_name)
    if not os.path.exists(old_path): return await message.reply_text("❌ <b>Not found.</b>")
    os.rename(old_path, os.path.join(project_dir, new_name))
    log_collab_event(owner_id, proj_name, user_id, f"Renamed {old_name} to {new_name}")
    await message.reply_text(f"✅ <b>Renamed <code>{old_name}</code> to <code>{new_name}</code>.</b>")

@app.on_message(filters.command("mkdir"))
@safe_handler
async def mkdir_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/mkdir [folder_name]</code>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    os.makedirs(os.path.join(get_project_dir(owner_id, proj_name), message.command[1].lstrip("/")), exist_ok=True)
    log_collab_event(owner_id, proj_name, user_id, f"Created directory: {message.command[1]}")
    await message.reply_text(f"✅ <b>Successfully created <code>{message.command[1]}</code></b>")

@app.on_message(filters.command("rmdir"))
@safe_handler
async def remove_dir(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    if len(message.command) < 2: return await message.reply_text("Usage: <code>/rmdir [folder_name]</code>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    dir_name = message.command[1]
    if dir_name in ["venv", ".git"] or ".." in dir_name: return await message.reply_text("❌ <b>Denied.</b>")
    dir_path = os.path.join(get_project_dir(owner_id, proj_name), dir_name)
    if os.path.exists(dir_path) and os.path.isdir(dir_path):
        shutil.rmtree(dir_path)
        log_collab_event(owner_id, proj_name, user_id, f"Deleted directory: {dir_name}")
        await message.reply_text(f"✅ <b><code>{dir_name}</code> deleted.</b>")
    else: await message.reply_text("❌ <b>Not found.</b>")

@app.on_message(filters.command("installreqs"))
@safe_handler
async def install_requirements(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    req_path = os.path.join(get_project_dir(owner_id, proj_name), "requirements.txt")
    if not os.path.exists(req_path): return await message.reply_text("❌ <b><code>requirements.txt</code> not found.</b>")
    msg = await message.reply_text("⏳ <b>Installing...</b>")
    await (await asyncio.create_subprocess_shell(f"{os.path.join(get_project_dir(owner_id, proj_name), 'venv', 'bin', 'pip')} install -r {req_path}")).wait()
    log_collab_event(owner_id, proj_name, user_id, f"Manually installed requirements.txt")
    await msg.edit_text("✅ <b>Dependencies installed!</b>")

@app.on_message(filters.command("myfiles"))
@safe_handler
async def myfiles_cmd(client: Client, message: Message):
    active_proj_str = db.get_active_project(message.from_user.id)
    if not active_proj_str: return await render_projects(client, message, message.from_user.id, is_cb=False)
    
    bot_msg = await message.reply_text("⏳ <b>Loading Explorer...</b>")
    class MockCallback:
        def __init__(self, msg, usr):
            self.message = msg
            self.data = "fm_."
            self.from_user = usr
        async def answer(self, *args, **kwargs): pass
    await handle_callbacks(client, MockCallback(bot_msg, message.from_user))

@app.on_message(filters.command("tree"))
@safe_handler
async def tree_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>No active project.</b>")
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    tree = await asyncio.to_thread(generate_tree, get_project_dir(owner_id, proj_name))
    header_text = f"🌳 <b>{proj_name}</b>\n"
    tree_body = safe_html_log(tree or "(Empty)")
    final_text = f"{header_text}<pre><code class='language-text'>{tree_body}</code></pre>"
    await message.reply_text(final_text)

@app.on_message(filters.command("status"))
@safe_handler
async def status_cmd(client: Client, message: Message):
    """ A quick, glanceable snapshot of the caller's current workspace. """
    user_id = message.from_user.id
    await message.reply_text(await build_status_text(user_id), reply_markup=InlineKeyboardMarkup([
        [create_btn("🔄 Refresh", cb="status_refresh"), create_btn("🗂 Switch Project", cb="menu_projects")]
    ]))

@app.on_message(filters.command("whoami"))
@safe_handler
async def whoami_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    role = "👑 Admin" if user_id in ADMIN_IDS else "👤 User"
    text = (
        "🪪 <b>Your Info</b>\n"
        "<blockquote>"
        f"ID: <code>{user_id}</code>\n"
        f"Role: {role}"
        "</blockquote>"
    )
    await message.reply_text(text)

# ==========================================
# 9. COLLAB-AWARE FILE UPLOADS
# ==========================================
@app.on_message(filters.document & filters.private)
@safe_handler
async def handle_file_upload(client: Client, message: Message):
    MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
    if message.document.file_size > MAX_FILE_SIZE: return await message.reply_text(f"❌ <b>Upload Denied:</b> File exceeds the {MAX_FILE_SIZE_MB} MB limit.")

    user_id = message.from_user.id
    active_proj_str = db.get_active_project(user_id)
    if not active_proj_str: return await message.reply_text("❌ <b>Select a project or workspace first!</b>")
    
    owner_id, proj_name = resolve_project_owner(user_id, active_proj_str)
    cwd = USER_CWD.get(user_id, ".") 
    target_dir = os.path.join(get_project_dir(owner_id, proj_name), cwd)
    os.makedirs(target_dir, exist_ok=True)
    
    path = os.path.join(target_dir, message.document.file_name)
    msg = await message.reply_text("📥 <b>Downloading...</b>")
    await message.download(path)
    if path.endswith(".zip"):
        def _extract():
            with zipfile.ZipFile(path, 'r') as z: z.extractall(os.path.dirname(path))
            os.remove(path)
        await asyncio.to_thread(_extract)
        
    log_collab_event(owner_id, proj_name, user_id, f"Uploaded file: {message.document.file_name} to {cwd}")
    
    if cwd == ".": await msg.edit_text(f"✅ <b>Saved to workspace root:</b> <code>{proj_name}</code>")
    else: await msg.edit_text(f"✅ <b>Saved to folder:</b> <code>{proj_name}/{cwd}</code>")

# ==========================================
# 10. STARTUP
# ==========================================
async def main_startup():
    await restore_system(app)
    await app.set_bot_commands([
        BotCommand("start", "Nex Host Main"), 
        BotCommand("status", "Quick Status"),
        BotCommand("myfiles", "Explorer/Editor"),
        BotCommand("myprojects", "Workspaces"),
        BotCommand("newproject", "Create Project"), 
        BotCommand("addcollab", "Add Partner"), 
        BotCommand("collabs", "Collab Info"),
        BotCommand("mycollabs", "Shared With Me"),
        BotCommand("run", "Run Script"), 
        BotCommand("restart", "Restart File"),
        BotCommand("stop", "Stop Script"),
        BotCommand("logs", "View Terminal Logs"),
        BotCommand("backup_proj", "Backup/Download"),
        BotCommand("deletefile", "Delete File"),
        BotCommand("help", "Help Guide"),
    ])
    print("✅ Nex Host Cloud OS Active! HTML UI & Animations Running.")
    asyncio.create_task(auto_backup_loop(app))
    await resume_active_processes(app)

def _asyncio_exception_handler(loop, context):
    """
    Catches exceptions from fire-and-forget background tasks
    (asyncio.create_task(...) calls like read_stdout / auto-heal restarts).
    Without this, those crashes were completely silent — the bot would just
    stop reacting to that one deployment with no error anywhere, which is
    almost certainly the "deploy and it stops responding" issue.
    """
    msg = context.get("exception") or context.get("message")
    logger.error(f"Unhandled background task error: {msg}", exc_info=context.get("exception"))

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_asyncio_exception_handler)
    app.start()
    try:
        loop.run_until_complete(main_startup())
        idle()
    except Exception:
        logger.error(f"Fatal error in main loop:\n{traceback.format_exc()}")
        raise
    finally:
        app.stop()
