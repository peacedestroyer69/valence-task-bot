import os
import json
import logging
import datetime
import sqlite3
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
import threading

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
logger = logging.getLogger("TaskBot.Database")

# Global clients
db = None
use_sqlite = True
db_lock = threading.RLock()

# --- Column Whitelists (SQL injection prevention) ---
ALLOWED_USER_FIELDS = {"xp", "level", "streak", "last_completed_date", "total_completed", "best_streak", "streak_freezes", "badges", "sprint_goal"}
ALLOWED_TASK_FIELDS = {"title", "description", "due_date", "priority", "category", "is_private", "status", "completed_at", "shared_with", "checklist", "pomodoros_estimated", "pomodoros_completed", "recurrence", "is_habit", "due_warning_sent", "remind_at", "notes"}

# --- Firebase Initialization ---
try:
    cred_json = os.getenv("FIREBASE_CREDENTIALS")
    if cred_json:
        cred_dict = json.loads(cred_json)
        cred = credentials.Certificate(cred_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        use_sqlite = False
        logger.info("Firebase initialized successfully in task_db.py.")
    else:
        logger.warning("FIREBASE_CREDENTIALS not found. Falling back to local SQLite database.")
except Exception as e:
    logger.error(f"Failed to initialize Firebase in task_db.py: {e}. Falling back to SQLite.")

# --- Local SQLite Fallback Setup ---
APPDATA_DIR = r"C:\Users\ROG\.gemini\antigravity"
if not os.path.exists(APPDATA_DIR) or not os.access(APPDATA_DIR, os.W_OK):
    APPDATA_DIR = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity")
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
    except Exception:
        import tempfile
        APPDATA_DIR = tempfile.gettempdir()

SQLITE_DB_PATH = os.path.join(APPDATA_DIR, "tasks_local.db")

def init_local_db():
    """Initializes local SQLite database for fallback mode."""
    if not use_sqlite:
        return
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                streak INTEGER DEFAULT 0,
                last_completed_date TEXT,
                total_completed INTEGER DEFAULT 0,
                best_streak INTEGER DEFAULT 0,
                streak_freezes INTEGER DEFAULT 1,
                badges TEXT DEFAULT '[]',
                sprint_goal INTEGER DEFAULT 7
            )
        """)
        # Tasks table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                due_date TEXT,
                priority TEXT DEFAULT 'Medium',
                category TEXT DEFAULT 'General',
                is_private INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                shared_with TEXT DEFAULT '[]',
                checklist TEXT DEFAULT '[]',
                pomodoros_estimated INTEGER DEFAULT 1,
                pomodoros_completed INTEGER DEFAULT 0,
                recurrence TEXT DEFAULT 'none',
                is_habit INTEGER DEFAULT 0,
                due_warning_sent INTEGER DEFAULT 0,
                remind_at TEXT,
                notes TEXT DEFAULT '[]'
            )
        """)
        # Upgrade existing SQLite DB if columns are missing
        _migrate_columns = [
            ("tasks", "due_warning_sent", "INTEGER DEFAULT 0"),
            ("tasks", "remind_at", "TEXT"),
            ("tasks", "notes", "TEXT DEFAULT '[]'"),
            ("users", "best_streak", "INTEGER DEFAULT 0"),
            ("users", "streak_freezes", "INTEGER DEFAULT 1"),
            ("users", "badges", "TEXT DEFAULT '[]'"),
            ("users", "sprint_goal", "INTEGER DEFAULT 7"),
        ]
        for table, col, col_type in _migrate_columns:
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()

# Initialize if we are running in local SQLite mode
if use_sqlite:
    init_local_db()

# --- Timezone Helpers ---

def get_ist_now() -> datetime.datetime:
    """Returns the current datetime in Asia/Kolkata (IST, UTC+5:30) as a timezone-naive object."""
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)).replace(tzinfo=None)

def get_ist_date_str() -> str:
    """Returns today's date in IST as YYYY-MM-DD."""
    return get_ist_now().strftime("%Y-%m-%d")

# --- Row-to-Dict Helpers ---

def _row_to_task_dict(row) -> dict:
    """Converts a SQLite row tuple to a task dictionary. Eliminates copy-paste."""
    def _safe_json_loads(val):
        if val is None:
            return []
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return []

    return {
        "task_id": row[0],
        "user_id": row[1],
        "title": row[2],
        "description": row[3],
        "due_date": row[4],
        "priority": row[5],
        "category": row[6],
        "is_private": bool(row[7]),
        "status": row[8],
        "created_at": row[9],
        "completed_at": row[10],
        "shared_with": _safe_json_loads(row[11]),
        "checklist": _safe_json_loads(row[12]),
        "pomodoros_estimated": row[13],
        "pomodoros_completed": row[14],
        "recurrence": row[15],
        "is_habit": bool(row[16]),
        "due_warning_sent": bool(row[17]) if len(row) > 17 else False,
        "remind_at": row[18] if len(row) > 18 else None,
        "notes": _safe_json_loads(row[19]) if len(row) > 19 else [],
    }

# --- User Profile Operations ---

def get_user_profile(user_id: str) -> dict:
    """Gets or creates a user profile."""
    user_id = str(user_id)
    default_profile = {
        "user_id": user_id,
        "xp": 0,
        "level": 1,
        "streak": 0,
        "last_completed_date": None,
        "total_completed": 0,
        "best_streak": 0,
        "streak_freezes": 1,
        "badges": "[]",
        "sprint_goal": 7
    }
    
    if not use_sqlite and db:
        try:
            doc_ref = db.collection("task_bot_users").document(user_id)
            doc = doc_ref.get()
            if doc.exists:
                data = doc.to_dict()
                data["user_id"] = user_id
                return data
            else:
                doc_ref.set(default_profile)
                return default_profile
        except Exception as e:
            logger.error(f"Firestore get_user_profile error: {e}")
            raise e
            
    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT xp, level, streak, last_completed_date, total_completed, best_streak, streak_freezes, badges, sprint_goal FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return {
                "user_id": user_id,
                "xp": row[0],
                "level": row[1],
                "streak": row[2],
                "last_completed_date": row[3],
                "total_completed": row[4],
                "best_streak": row[5] if row[5] is not None else 0,
                "streak_freezes": row[6] if row[6] is not None else 1,
                "badges": row[7] if row[7] is not None else "[]",
                "sprint_goal": row[8] if row[8] is not None else 7
            }
        else:
            cursor.execute(
                "INSERT INTO users (user_id, xp, level, streak, last_completed_date, total_completed, best_streak, streak_freezes, badges, sprint_goal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, 0, 1, 0, None, 0, 0, 1, "[]", 7)
            )
            conn.commit()
            return default_profile
    finally:
        conn.close()

def update_user_profile(user_id: str, updates: dict) -> bool:
    """Updates fields on a user's profile."""
    user_id = str(user_id)

    # Validate keys against whitelist
    invalid_keys = set(updates.keys()) - ALLOWED_USER_FIELDS
    if invalid_keys:
        logger.warning(f"update_user_profile: rejected invalid fields: {invalid_keys}")
        updates = {k: v for k, v in updates.items() if k in ALLOWED_USER_FIELDS}
    if not updates:
        return False

    with db_lock:
        if not use_sqlite and db:
            try:
                db.collection("task_bot_users").document(user_id).update(updates)
                return True
            except Exception as e:
                logger.error(f"Firestore update_user_profile error: {e}")
                raise e
                
        # SQLite Fallback
        conn = sqlite3.connect(SQLITE_DB_PATH)
        try:
            cursor = conn.cursor()
            fields = []
            params = []
            for k, v in updates.items():
                fields.append(f"{k} = ?")
                params.append(v)
            params.append(user_id)
            query = f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?"
            cursor.execute(query, params)
            conn.commit()
            return True
        finally:
            conn.close()

def add_xp(user_id: str, amount: int) -> tuple:
    """Awards XP to a user and handles leveling up. Returns (new_xp, new_level, leveled_up: bool)."""
    with db_lock:
        profile = get_user_profile(user_id)
        current_xp = profile.get("xp", 0) + amount
        current_level = profile.get("level", 1)
        leveled_up = False
        
        # Simple leveling formula: Level N requires N * 1000 XP
        while current_xp >= current_level * 1000:
            current_xp -= current_level * 1000
            current_level += 1
            leveled_up = True
            
        update_user_profile(user_id, {
            "xp": current_xp,
            "level": current_level
        })
        return current_xp, current_level, leveled_up

def update_streak(user_id: str) -> int:
    """Updates daily completion streak based on completion calendar in IST. Returns current streak."""
    with db_lock:
        profile = get_user_profile(user_id)
        today_str = get_ist_date_str()
        last_completed = profile.get("last_completed_date")
        current_streak = profile.get("streak", 0)
        best_streak = profile.get("best_streak", 0)
        
        if last_completed == today_str:
            # Already completed a task today, streak is maintained
            pass
        else:
            # Check if last completion was yesterday in IST
            today = get_ist_now().date()
            yesterday_str = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            if last_completed == yesterday_str:
                current_streak += 1
            else:
                current_streak = 1
        
        # Update best_streak if current exceeds it
        if current_streak > best_streak:
            best_streak = current_streak

        # Use atomic SQL for total_completed instead of read-then-write
        if use_sqlite:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET streak = ?, last_completed_date = ?, total_completed = total_completed + 1, best_streak = ? WHERE user_id = ?",
                    (current_streak, today_str, best_streak, str(user_id))
                )
                conn.commit()
            finally:
                conn.close()
        else:
            # For Firebase, we still do the field-based update (no atomic increment available via simple dict)
            update_user_profile(user_id, {
                "streak": current_streak,
                "last_completed_date": today_str,
                "total_completed": profile.get("total_completed", 0) + 1,
                "best_streak": best_streak
            })

        return current_streak

# --- Task Operations ---

def add_task(user_id: str, title: str, description: str = "", due_date: str = None, 
             priority: str = "Medium", category: str = "General", is_private: bool = False, 
             recurrence: str = "none", is_habit: bool = False, pomodoros_estimated: int = 1) -> str:
    """Creates a new task. Returns the task ID."""
    import uuid
    task_id = str(uuid.uuid4())
    user_id = str(user_id)
    
    task_data = {
        "task_id": task_id,
        "user_id": user_id,
        "title": title,
        "description": description,
        "due_date": due_date,
        "priority": priority,
        "category": category,
        "is_private": is_private,
        "status": "pending",
        "created_at": get_ist_now().isoformat(),
        "completed_at": None,
        "shared_with": [],
        "checklist": [],
        "pomodoros_estimated": pomodoros_estimated,
        "pomodoros_completed": 0,
        "recurrence": recurrence,
        "is_habit": is_habit,
        "due_warning_sent": False,
        "remind_at": None,
        "notes": []
    }
    
    with db_lock:
        if not use_sqlite and db:
            try:
                db.collection("task_bot_tasks").document(task_id).set(task_data)
                return task_id
            except Exception as e:
                logger.error(f"Firestore add_task error: {e}")
                raise e
                
        # SQLite Fallback
        conn = sqlite3.connect(SQLITE_DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tasks (
                    task_id, user_id, title, description, due_date, priority, category, 
                    is_private, status, created_at, completed_at, shared_with, checklist, 
                    pomodoros_estimated, pomodoros_completed, recurrence, is_habit, due_warning_sent, remind_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, user_id, title, description, due_date, priority, category,
                1 if is_private else 0, "pending", task_data["created_at"], None,
                json.dumps([]), json.dumps([]), pomodoros_estimated, 0, recurrence, 1 if is_habit else 0,
                0, None, json.dumps([])
            ))
            conn.commit()
            return task_id
        finally:
            conn.close()

def get_task(task_id: str) -> dict:
    """Retrieves a single task by ID."""
    task_id = str(task_id)
    if not use_sqlite and db:
        try:
            doc = db.collection("task_bot_tasks").document(task_id).get()
            if doc.exists:
                return doc.to_dict()
            return None
        except Exception as e:
            logger.error(f"Firestore get_task error: {e}")
            raise e
            
    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        if row:
            return _row_to_task_dict(row)
        return None
    finally:
        conn.close()

def get_user_tasks(user_id: str, status: str = None, category: str = None, priority: str = None) -> list:
    """Retrieves tasks owned by or shared with a user."""
    user_id = str(user_id)
    if not use_sqlite and db:
        try:
            tasks_ref = db.collection("task_bot_tasks")
            query = tasks_ref.where("user_id", "==", user_id)
            docs = query.stream()
            tasks = [doc.to_dict() for doc in docs]
            
            shared_query = tasks_ref.where("shared_with", "array_contains", user_id)
            shared_docs = shared_query.stream()
            for doc in shared_docs:
                t = doc.to_dict()
                if t["task_id"] not in [x["task_id"] for x in tasks]:
                    tasks.append(t)
                    
            filtered_tasks = []
            for t in tasks:
                if status and t.get("status") != status:
                    continue
                if category and t.get("category") != category:
                    continue
                if priority and t.get("priority") != priority:
                    continue
                filtered_tasks.append(t)
            return filtered_tasks
        except Exception as e:
            logger.error(f"Firestore get_user_tasks error: {e}")
            raise e
            
    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        query = "SELECT * FROM tasks WHERE user_id = ? OR shared_with LIKE ?"
        params = [user_id, f'%"{user_id}"%']
        if status:
            query += " AND status = ?"
            params.append(status)
        if category:
            query += " AND category = ?"
            params.append(category)
        if priority:
            query += " AND priority = ?"
            params.append(priority)
            
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
    finally:
        conn.close()
    
    return [_row_to_task_dict(row) for row in rows]

def get_all_pending_tasks() -> list:
    """Retrieves all pending tasks across all users (for reminders)."""
    if not use_sqlite and db:
        try:
            docs = db.collection("task_bot_tasks").where("status", "==", "pending").stream()
            return [doc.to_dict() for doc in docs]
        except Exception as e:
            logger.error(f"Firestore get_all_pending_tasks error: {e}")
            return []
            
    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    rows = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE status = 'pending'")
        rows = cursor.fetchall()
    finally:
        conn.close()
    
    return [_row_to_task_dict(row) for row in rows]

def update_task(task_id: str, updates: dict) -> bool:
    """Updates fields on an existing task."""
    task_id = str(task_id)

    # Validate keys against whitelist
    invalid_keys = set(updates.keys()) - ALLOWED_TASK_FIELDS
    if invalid_keys:
        logger.warning(f"update_task: rejected invalid fields: {invalid_keys}")
        updates = {k: v for k, v in updates.items() if k in ALLOWED_TASK_FIELDS}
    if not updates:
        return False

    with db_lock:
        if not use_sqlite and db:
            try:
                db.collection("task_bot_tasks").document(task_id).update(updates)
                return True
            except Exception as e:
                logger.error(f"Firestore update_task error: {e}")
                raise e
                
        # SQLite Fallback
        task = get_task(task_id)
        if not task:
            return False
        
        conn = sqlite3.connect(SQLITE_DB_PATH)
        try:
            cursor = conn.cursor()
            fields = []
            params = []
            for k, v in updates.items():
                fields.append(f"{k} = ?")
                if k in ["shared_with", "checklist", "notes"]:
                    params.append(json.dumps(v))
                elif k in ["is_private", "is_habit", "due_warning_sent"]:
                    params.append(1 if v else 0)
                else:
                    params.append(v)
            params.append(task_id)
            query = f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?"
            cursor.execute(query, params)
            conn.commit()
            return True
        finally:
            conn.close()

def delete_task(task_id: str) -> bool:
    """Deletes a task by ID."""
    task_id = str(task_id)
    with db_lock:
        if not use_sqlite and db:
            try:
                db.collection("task_bot_tasks").document(task_id).delete()
                return True
            except Exception as e:
                logger.error(f"Firestore delete_task error: {e}")
                raise e
                
        # SQLite Fallback
        conn = sqlite3.connect(SQLITE_DB_PATH)
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            rows_affected = cursor.rowcount
            conn.commit()
            return rows_affected > 0
        finally:
            conn.close()

def complete_task(user_id: str, task_id: str) -> tuple:
    """Marks a task completed, awards XP, and updates streaks. Returns (success, task, stats)."""
    with db_lock:
        user_id = str(user_id)
        task_id = str(task_id)
        task = get_task(task_id)
        if not task:
            return False, None, {}
            
        if task.get("status") == "completed":
            return False, task, {}
            
        # Mark task completed
        completed_at = get_ist_now().isoformat()
        update_task(task_id, {"status": "completed", "completed_at": completed_at})
        task["status"] = "completed"
        task["completed_at"] = completed_at
        
        # Calculate XP reward
        priority = task.get("priority", "Medium")
        base_xp = 100
        if priority == "High":
            base_xp = 150
        elif priority == "Low":
            base_xp = 50
            
        # Checklist bonus: +10 per completed item
        checklist = task.get("checklist", [])
        checklist_bonus = 0
        if checklist:
            checklist_bonus = sum(10 for item in checklist if item.get("done") or item.get("completed"))
            
        total_xp = base_xp + checklist_bonus
        
        # Add XP & Level Up
        new_xp, new_level, leveled_up = add_xp(user_id, total_xp)
        
        # Update Streak
        streak = update_streak(user_id)
        
        stats = {
            "xp_gained": total_xp,
            "streak": streak,
            "level_ups": 1 if leveled_up else 0,
            "new_level": new_level
        }
        
        return True, task, stats

# --- Gamification Leaderboard ---

def get_leaderboard() -> list:
    """Returns users ranked by Level and XP descending (fixing the rollover sorting issue)."""
    if not use_sqlite and db:
        try:
            docs = db.collection("task_bot_users").stream()
            users = [doc.to_dict() for doc in docs]
            users.sort(key=lambda u: (u.get("level", 1), u.get("xp", 0)), reverse=True)
            return users[:10]
        except Exception as e:
            logger.error(f"Firestore get_leaderboard error: {e}")
            raise e
            
    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    rows = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, xp, level, streak, total_completed FROM users ORDER BY level DESC, xp DESC LIMIT 10")
        rows = cursor.fetchall()
    finally:
        conn.close()
    
    leaderboard = []
    for r in rows:
        leaderboard.append({
            "user_id": r[0],
            "xp": r[1],
            "level": r[2],
            "streak": r[3],
            "total_completed": r[4]
        })
    return leaderboard

# --- Habits Resets ---

def fetch_completed_habits() -> list:
    """Queries all completed recurring habits globally."""
    if not use_sqlite and db:
        try:
            docs = db.collection("task_bot_tasks") \
                     .where("is_habit", "==", True) \
                     .where("status", "==", "completed") \
                     .stream()
            return [doc.to_dict() for doc in docs]
        except Exception as e:
            logger.error(f"Firestore fetch_completed_habits error: {e}")
            raise e
            
    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    rows = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE is_habit = 1 AND status = 'completed'")
        rows = cursor.fetchall()
    finally:
        conn.close()
    
    return [_row_to_task_dict(row) for row in rows]

def reset_habit(task_id: str):
    """Resets a completed habit status back to pending."""
    update_task(task_id, {"status": "pending", "completed_at": None})

def get_last_habit_reset_date() -> str:
    """Retrieves the last date habit resets were executed."""
    if not use_sqlite and db:
        try:
            doc = db.collection("task_bot_config").document("settings").get()
            if doc.exists:
                return doc.to_dict().get("last_habit_reset_date")
            return None
        except Exception as e:
            logger.error(f"Firestore get_last_habit_reset_date error: {e}", exc_info=True)
            raise
    # SQLite
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        cursor.execute("SELECT value FROM config WHERE key = 'last_habit_reset_date'")
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def set_last_habit_reset_date(date_str: str):
    """Sets the last date habit resets were executed."""
    if not use_sqlite and db:
        try:
            db.collection("task_bot_config").document("settings").set({"last_habit_reset_date": date_str}, merge=True)
            return
        except Exception as e:
            logger.error(f"Firestore set_last_habit_reset_date error: {e}", exc_info=True)
            raise
    # SQLite
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('last_habit_reset_date', ?)", (date_str,))
        conn.commit()
    finally:
        conn.close()

# --- New DB Functions ---

def reopen_task(task_id: str) -> bool:
    """Sets a completed task back to pending, clears completed_at."""
    task_id = str(task_id)
    task = get_task(task_id)
    if not task:
        return False
    if task.get("status") != "completed":
        return False
    return update_task(task_id, {"status": "pending", "completed_at": None})

def get_overdue_tasks(user_id: str) -> list:
    """Gets all pending tasks where due_date < now (IST)."""
    user_id = str(user_id)
    now_str = get_ist_now().isoformat()

    if not use_sqlite and db:
        try:
            tasks_ref = db.collection("task_bot_tasks")
            docs = tasks_ref.where("user_id", "==", user_id) \
                            .where("status", "==", "pending") \
                            .stream()
            overdue = []
            for doc in docs:
                t = doc.to_dict()
                due = t.get("due_date")
                if due and due < now_str:
                    overdue.append(t)
            return overdue
        except Exception as e:
            logger.error(f"Firestore get_overdue_tasks error: {e}")
            return []

    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND status = 'pending' AND due_date IS NOT NULL AND due_date < ?",
            (user_id, now_str)
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [_row_to_task_dict(row) for row in rows]

def get_today_tasks(user_id: str) -> list:
    """Gets pending tasks due today + habits due today + overdue tasks."""
    user_id = str(user_id)
    today_str = get_ist_date_str()

    if not use_sqlite and db:
        try:
            tasks_ref = db.collection("task_bot_tasks")
            docs = tasks_ref.where("user_id", "==", user_id) \
                            .where("status", "==", "pending") \
                            .stream()
            result = []
            for doc in docs:
                t = doc.to_dict()
                due = t.get("due_date")
                is_habit = t.get("is_habit", False)
                if is_habit:
                    result.append(t)
                elif due and due[:10] <= today_str:
                    result.append(t)
            return result
        except Exception as e:
            logger.error(f"Firestore get_today_tasks error: {e}")
            return []

    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        # Pending tasks due today or overdue, plus pending habits
        cursor.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND status = 'pending' AND (is_habit = 1 OR (due_date IS NOT NULL AND SUBSTR(due_date, 1, 10) <= ?))",
            (user_id, today_str)
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [_row_to_task_dict(row) for row in rows]

def search_tasks(user_id: str, query: str) -> list:
    """Searches tasks by title/description matching."""
    user_id = str(user_id)
    search_pattern = f"%{query}%"

    if not use_sqlite and db:
        try:
            # Firestore has no LIKE; fetch all and filter in-memory
            tasks_ref = db.collection("task_bot_tasks")
            docs = tasks_ref.where("user_id", "==", user_id).stream()
            result = []
            query_lower = query.lower()
            for doc in docs:
                t = doc.to_dict()
                title = (t.get("title") or "").lower()
                desc = (t.get("description") or "").lower()
                if query_lower in title or query_lower in desc:
                    result.append(t)
            return result
        except Exception as e:
            logger.error(f"Firestore search_tasks error: {e}")
            return []

    # SQLite Fallback
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND (title LIKE ? OR description LIKE ?)",
            (user_id, search_pattern, search_pattern)
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [_row_to_task_dict(row) for row in rows]

def use_streak_freeze(user_id: str) -> bool:
    """Consumes 1 streak freeze to prevent streak reset. Returns True if freeze was available."""
    user_id = str(user_id)
    with db_lock:
        profile = get_user_profile(user_id)
        freezes = profile.get("streak_freezes", 0)
        if freezes <= 0:
            return False

        if use_sqlite:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET streak_freezes = streak_freezes - 1, last_completed_date = ? WHERE user_id = ? AND streak_freezes > 0",
                    (get_ist_date_str(), user_id)
                )
                affected = cursor.rowcount
                conn.commit()
                return affected > 0
            finally:
                conn.close()
        else:
            update_user_profile(user_id, {
                "streak_freezes": freezes - 1,
                "last_completed_date": get_ist_date_str()
            })
            return True

def add_badge(user_id: str, badge: str) -> bool:
    """Adds a badge to user's badge list if not already earned."""
    user_id = str(user_id)
    with db_lock:
        profile = get_user_profile(user_id)
        badges_raw = profile.get("badges", "[]")
        try:
            badges = json.loads(badges_raw) if isinstance(badges_raw, str) else badges_raw
        except (json.JSONDecodeError, TypeError):
            badges = []
        if not isinstance(badges, list):
            badges = []
        if badge in badges:
            return False
        badges.append(badge)
        update_user_profile(user_id, {"badges": json.dumps(badges)})
        return True

def get_user_badges(user_id: str) -> list:
    """Returns list of earned badges."""
    user_id = str(user_id)
    profile = get_user_profile(user_id)
    badges_raw = profile.get("badges", "[]")
    try:
        badges = json.loads(badges_raw) if isinstance(badges_raw, str) else badges_raw
    except (json.JSONDecodeError, TypeError):
        badges = []
    if not isinstance(badges, list):
        badges = []
    return badges

def add_task_note(task_id: str, note_text: str) -> bool:
    """Appends a timestamped note to a task's notes list."""
    task_id = str(task_id)
    task = get_task(task_id)
    if not task:
        return False

    notes = task.get("notes", [])
    if not isinstance(notes, list):
        notes = []

    note_entry = {
        "text": note_text,
        "timestamp": get_ist_now().isoformat()
    }
    notes.append(note_entry)

    return update_task(task_id, {"notes": notes})
