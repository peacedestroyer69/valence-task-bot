import unittest
import os
import sys
import datetime
import sqlite3

# Add the parent directory to sys.path so we can import task_db
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import task_db

class TestTaskDatabase(unittest.TestCase):
    def setUp(self):
        import tempfile
        task_db.use_sqlite = True
        task_db.SQLITE_DB_PATH = os.path.join(tempfile.gettempdir(), "test_tasks_local.db")
        
        # Clear caches
        task_db._TASK_USER_CACHE.clear()
        task_db._TASK_CACHE.clear()
        task_db._DIRTY_TASK_USERS.clear()
        task_db._DIRTY_TASKS.clear()
        task_db._DELETED_TASKS.clear()
        
        # Clean up database if it exists from previous failures
        if os.path.exists(task_db.SQLITE_DB_PATH):
            try:
                os.remove(task_db.SQLITE_DB_PATH)
            except OSError:
                pass
                
        # Initialize test database tables
        task_db.init_local_db()
        
        self.user_valence = "856485470171299891"
        self.user_ujjwal = "1403716456025165864"

    def tearDown(self):
        # Clean up database after each test
        if os.path.exists(task_db.SQLITE_DB_PATH):
            try:
                os.remove(task_db.SQLITE_DB_PATH)
            except OSError:
                pass

    def test_user_profile_creation_and_retrieval(self):
        # Verify get_user_profile creates profile if not present
        profile = task_db.get_user_profile(self.user_valence)
        self.assertIsNotNone(profile)
        self.assertEqual(profile["user_id"], self.user_valence)
        self.assertEqual(profile["xp"], 0)
        self.assertEqual(profile["level"], 1)
        self.assertEqual(profile["streak"], 0)
        self.assertIsNone(profile["last_completed_date"])

        # Fetch again and check it remains the same
        profile2 = task_db.get_user_profile(self.user_valence)
        self.assertEqual(profile2["xp"], 0)

    def test_add_xp_and_level_up(self):
        # Add XP without level up
        xp, lvl, leveled_up = task_db.add_xp(self.user_valence, 500)
        self.assertEqual(xp, 500)
        self.assertEqual(lvl, 1)
        self.assertFalse(leveled_up)

        # Add XP causing level up (Level 1 requires 1000 XP)
        xp, lvl, leveled_up = task_db.add_xp(self.user_valence, 600)
        self.assertEqual(xp, 100) # 1100 total XP -> Level 2, 100 leftover
        self.assertEqual(lvl, 2)
        self.assertTrue(leveled_up)
        
        # Verify in DB
        profile = task_db.get_user_profile(self.user_valence)
        self.assertEqual(profile["xp"], 100)
        self.assertEqual(profile["level"], 2)

    def test_update_streak(self):
        # First completion
        streak = task_db.update_streak(self.user_valence)
        self.assertEqual(streak, 1)
        
        profile = task_db.get_user_profile(self.user_valence)
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        self.assertEqual(profile["last_completed_date"], today_str)
        self.assertEqual(profile["total_completed"], 1)

        # Complete again today (streak should remain 1)
        streak = task_db.update_streak(self.user_valence)
        self.assertEqual(streak, 1)

    def test_add_and_get_tasks(self):
        # Add a task
        task_id = task_db.add_task(
            user_id=self.user_valence,
            title="Study Chemistry",
            description="Organic Chemistry chapters 3 & 4",
            due_date="2026-06-20",
            priority="High",
            category="Study",
            is_private=False
        )
        self.assertIsNotNone(task_id)

        # Retrieve specific task
        task = task_db.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Study Chemistry")
        self.assertEqual(task["description"], "Organic Chemistry chapters 3 & 4")
        self.assertEqual(task["due_date"], "2026-06-20")
        self.assertEqual(task["priority"], "High")
        self.assertEqual(task["category"], "Study")
        self.assertFalse(task["is_private"])
        self.assertEqual(task["status"], "pending")

        # Get list of tasks
        tasks = task_db.get_user_tasks(self.user_valence)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], task_id)

    def test_update_and_delete_task(self):
        task_id = task_db.add_task(
            user_id=self.user_valence,
            title="Buy Groceries",
            is_private=True
        )
        
        # Update title and status
        success = task_db.update_task(task_id, {"title": "Buy Fruits", "status": "completed"})
        self.assertTrue(success)
        
        task = task_db.get_task(task_id)
        self.assertEqual(task["title"], "Buy Fruits")
        self.assertEqual(task["status"], "completed")

        # Delete task
        success = task_db.delete_task(task_id)
        self.assertTrue(success)
        
        # Verify it's gone
        task = task_db.get_task(task_id)
        self.assertIsNone(task)

    def test_leaderboard(self):
        # Create user profiles and award XP
        task_db.add_xp(self.user_valence, 1500) # lvl 2, 500xp (total 1500)
        task_db.add_xp(self.user_ujjwal, 2800) # lvl 3, 800xp (total 2800)
        
        leaderboard = task_db.get_leaderboard()
        self.assertEqual(len(leaderboard), 2)
        
        # Ujjwal should be first since they have level 3, 800xp
        self.assertEqual(leaderboard[0]["user_id"], self.user_ujjwal)
        self.assertEqual(leaderboard[1]["user_id"], self.user_valence)

if __name__ == "__main__":
    unittest.main()
