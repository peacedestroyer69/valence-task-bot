"""
task_db.py

Helper functions using firebase_admin and google.cloud.firestore to manage
tasks and user profiles in Firebase Firestore under 'task_bot_tasks' and 'task_bot_users'.
All Firestore calls are run asynchronously using asyncio.to_thread to prevent blocking.
"""

import os
import json
import logging
import math
from datetime import datetime, timezone, timedelta
import asyncio
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import firestore as google_firestore

# Setup logging
logger = logging.getLogger("TaskBot.DB")

_db_client = None

def get_db():
    """
    Initializes (if needed) and returns the Firestore client.
    Ensures safe initialization during hot reloads or multiple imports.
    """
    global _db_client
    if _db_client is not None:
        return _db_client

    # Check if Firebase is already initialized by another module
    if firebase_admin._apps:
        try:
            _db_client = firestore.client()
            logger.info("Firestore client retrieved from existing Firebase app.")
            return _db_client
        except Exception as e:
            logger.exception(f"Failed to get Firestore client from existing app: {e}")

    # Initialize from environment variable
    cred_json = os.getenv("FIREBASE_CREDENTIALS")
    if cred_json:
        try:
            cred_dict = json.loads(cred_json)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            _db_client = firestore.client()
            logger.info("Firebase initialized successfully from environment credentials.")
            return _db_client
        except Exception as e:
            logger.exception(f"Failed to initialize Firebase from FIREBASE_CREDENTIALS: {e}")

    # Attempt default initialization
    try:
        firebase_admin.initialize_app()
        _db_client = firestore.client()
        logger.info("Firebase initialized using default credentials.")
        return _db_client
    except Exception as e:
        logger.warning(f"Firebase default initialization failed: {e}")

    logger.error("Firestore database client is not initialized.")
    return None


def calculate_level(xp: int) -> int:
    """
    Calculates user level based on cumulative XP.
    Level = 1 + floor(sqrt(xp / 100))
    Example:
      - 0 to 99 XP: Level 1
      - 100 to 399 XP: Level 2
      - 400 to 899 XP: Level 3
      - 900 to 1599 XP: Level 4
    """
    if xp < 0:
        xp = 0
    return 1 + int(math.sqrt(xp / 100))


def _to_utc_datetime(dt):
    """
    Converts a datetime or firestore Timestamp to timezone-aware UTC datetime.
    """
    if dt is None:
        return None
    if isinstance(dt, google_firestore.Timestamp):
        dt = dt.to_datetime()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ==========================================
# TASK CRUD FUNCTIONS (task_bot_tasks)
# ==========================================

async def create_task(
    user_id: str,
    title: str,
    description: str = "",
    due_at: datetime = None,
    category: str = None,
    difficulty: str = None,
    xp_reward: int = 20
) -> dict:
    """
    Creates a new task in the 'task_bot_tasks' collection.
    Returns the created task document data.
    """
    db = get_db()
    if not db:
        raise RuntimeError("Database not initialized")

    def _create():
        doc_ref = db.collection("task_bot_tasks").document()
        task_data = {
            "task_id": doc_ref.id,
            "user_id": str(user_id),
            "title": title,
            "description": description,
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
            "due_at": _to_utc_datetime(due_at),
            "completed_at": None,
            "xp_reward": xp_reward,
            "category": category,
            "difficulty": difficulty
        }
        doc_ref.set(task_data)
        logger.info(f"Task created: {doc_ref.id} for user {user_id}")
        return task_data

    return await asyncio.to_thread(_create)


async def get_task(task_id: str) -> dict or None:
    """
    Retrieves a single task by its task_id.
    Returns the task data, or None if not found.
    """
    db = get_db()
    if not db:
        return None

    def _get():
        doc = db.collection("task_bot_tasks").document(task_id).get()
        return doc.to_dict() if doc.exists else None

    return await asyncio.to_thread(_get)


async def get_user_tasks(user_id: str, status: str = None) -> list:
    """
    Retrieves all tasks for a specific user.
    Optionally filters by status (e.g. 'pending', 'completed').
    Returns a list of task documents, sorted by created_at descending.
    """
    db = get_db()
    if not db:
        return []

    def _get_list():
        query = db.collection("task_bot_tasks").where("user_id", "==", str(user_id))
        if status:
            query = query.where("status", "==", status)
        
        docs = query.stream()
        tasks = [doc.to_dict() for doc in docs]
        
        # Sort in memory to avoid needing composite Firestore indexes
        tasks.sort(key=lambda t: t.get("created_at") or datetime.min, reverse=True)
        return tasks

    return await asyncio.to_thread(_get_list)


async def update_task(task_id: str, updates: dict) -> bool:
    """
    Updates specific fields of an existing task.
    Returns True if update succeeded, False if task does not exist.
    """
    db = get_db()
    if not db:
        return False

    def _update():
        doc_ref = db.collection("task_bot_tasks").document(task_id)
        doc = doc_ref.get()
        if not doc.exists:
            return False
            
        # Standardize any datetime values in updates
        cleaned_updates = {}
        for k, v in updates.items():
            if isinstance(v, datetime):
                cleaned_updates[k] = _to_utc_datetime(v)
            else:
                cleaned_updates[k] = v
                
        # If completing, ensure completed_at is set
        if cleaned_updates.get("status") == "completed" and doc.to_dict().get("status") != "completed":
            if "completed_at" not in cleaned_updates:
                cleaned_updates["completed_at"] = datetime.now(timezone.utc)
                
        doc_ref.update(cleaned_updates)
        logger.info(f"Task updated: {task_id}")
        return True

    return await asyncio.to_thread(_update)


async def delete_task(task_id: str) -> bool:
    """
    Deletes a task by its task_id.
    Returns True if deletion succeeded, False if task does not exist.
    """
    db = get_db()
    if not db:
        return False

    def _delete():
        doc_ref = db.collection("task_bot_tasks").document(task_id)
        if not doc_ref.get().exists:
            return False
        doc_ref.delete()
        logger.info(f"Task deleted: {task_id}")
        return True

    return await asyncio.to_thread(_delete)


async def complete_task(task_id: str) -> dict or None:
    """
    Marks a task as completed and automatically awards XP/updates streaks for the user.
    Returns the updated task document, or None if task not found or already completed.
    """
    db = get_db()
    if not db:
        return None

    def _complete():
        doc_ref = db.collection("task_bot_tasks").document(task_id)
        doc = doc_ref.get()
        if not doc.exists:
            return None
        task = doc.to_dict()
        if task.get("status") == "completed":
            return None

        completed_at = datetime.now(timezone.utc)
        updates = {
            "status": "completed",
            "completed_at": completed_at
        }
        doc_ref.update(updates)
        task.update(updates)
        logger.info(f"Task completed successfully in Firestore: {task_id}")
        return task

    completed_task = await asyncio.to_thread(_complete)
    if completed_task:
        user_id = completed_task["user_id"]
        xp_reward = completed_task.get("xp_reward", 20)
        # Update user profile with XP and streak
        await update_user_xp_and_streak(user_id, xp_reward, completed_task=True)

    return completed_task


# ==========================================
# USER PROFILE FUNCTIONS (task_bot_users)
# ==========================================

async def get_user_profile(user_id: str) -> dict:
    """
    Retrieves or creates a user profile in the 'task_bot_users' collection.
    Automatically checks and resets active streaks if a day was missed.
    Returns the user profile dictionary.
    """
    db = get_db()
    if not db:
        raise RuntimeError("Database not initialized")

    def _get_or_create():
        doc_ref = db.collection("task_bot_users").document(str(user_id))
        doc = doc_ref.get()

        if not doc.exists:
            profile = {
                "user_id": str(user_id),
                "xp": 0,
                "level": 1,
                "streak": 0,
                "max_streak": 0,
                "last_completed_at": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
            doc_ref.set(profile)
            logger.info(f"New user profile created for {user_id}")
            return profile

        profile = doc.to_dict()
        
        # Verify and correct streak reset
        last_completed = profile.get("last_completed_at")
        streak = profile.get("streak", 0)

        if streak > 0 and last_completed:
            last_completed_utc = _to_utc_datetime(last_completed)
            today = datetime.now(timezone.utc).date()
            diff = (today - last_completed_utc.date()).days
            
            if diff > 1:
                profile["streak"] = 0
                profile["updated_at"] = datetime.now(timezone.utc)
                doc_ref.update({
                    "streak": 0,
                    "updated_at": profile["updated_at"]
                })
                logger.info(f"User {user_id} streak reset to 0 (last completed {diff} days ago).")

        return profile

    return await asyncio.to_thread(_get_or_create)


async def update_user_xp_and_streak(user_id: str, xp_to_add: int, completed_task: bool = False) -> dict:
    """
    Updates user XP and level, and updates streak if a task was completed.
    Returns the updated user profile dictionary.
    """
    db = get_db()
    if not db:
        raise RuntimeError("Database not initialized")

    def _update():
        doc_ref = db.collection("task_bot_users").document(str(user_id))
        doc = doc_ref.get()
        now = datetime.now(timezone.utc)

        if not doc.exists:
            xp = max(0, xp_to_add)
            level = calculate_level(xp)
            streak = 1 if completed_task else 0
            max_streak = streak
            last_completed = now if completed_task else None

            profile = {
                "user_id": str(user_id),
                "xp": xp,
                "level": level,
                "streak": streak,
                "max_streak": max_streak,
                "last_completed_at": last_completed,
                "created_at": now,
                "updated_at": now
            }
            doc_ref.set(profile)
            logger.info(f"Created user profile on XP update for {user_id}")
            return profile

        profile = doc.to_dict()
        current_xp = profile.get("xp", 0)
        new_xp = max(0, current_xp + xp_to_add)
        new_level = calculate_level(new_xp)

        streak = profile.get("streak", 0)
        max_streak = profile.get("max_streak", 0)
        last_completed = profile.get("last_completed_at")

        updates = {
            "xp": new_xp,
            "level": new_level,
            "updated_at": now
        }

        if completed_task:
            if last_completed:
                last_completed_utc = _to_utc_datetime(last_completed)
                diff = (now.date() - last_completed_utc.date()).days

                if diff == 0:
                    # Already completed a task today, streak remains same
                    pass
                elif diff == 1:
                    # Consecutive day completion
                    streak += 1
                else:
                    # Streak broken, reset to 1
                    streak = 1
            else:
                # First completed task
                streak = 1

            max_streak = max(max_streak, streak)
            updates["streak"] = streak
            updates["max_streak"] = max_streak
            updates["last_completed_at"] = now
        else:
            # If just modifying XP, check if streak has expired
            if streak > 0 and last_completed:
                last_completed_utc = _to_utc_datetime(last_completed)
                diff = (now.date() - last_completed_utc.date()).days
                if diff > 1:
                    streak = 0
                    updates["streak"] = streak

        doc_ref.update(updates)
        profile.update(updates)
        logger.info(f"User profile updated for {user_id}: XP={new_xp}, Streak={streak}")
        return profile

    return await asyncio.to_thread(_update)


# ==========================================
# LEADERBOARD FUNCTIONS
# ==========================================

async def get_top_users(limit: int = 10) -> list:
    """
    Retrieves the top users ordered by cumulative XP.
    """
    db = get_db()
    if not db:
        return []

    def _get_top():
        docs = db.collection("task_bot_users").order_by("xp", direction=google_firestore.Query.DESCENDING).limit(limit).stream()
        return [doc.to_dict() for doc in docs]

    return await asyncio.to_thread(_get_top)


async def get_top_streaks(limit: int = 10) -> list:
    """
    Retrieves the top users ordered by max streak.
    """
    db = get_db()
    if not db:
        return []

    def _get_top():
        docs = db.collection("task_bot_users").order_by("max_streak", direction=google_firestore.Query.DESCENDING).limit(limit).stream()
        return [doc.to_dict() for doc in docs]

    return await asyncio.to_thread(_get_top)
