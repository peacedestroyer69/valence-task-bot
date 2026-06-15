# Task Bot Implementation Tasks

- `[x]` Setup & Configuration
  - `[x]` Define and spin up the team of 12 specialized subagents
  - `[x]` Update `.env` to support `TASK_BOT_TOKEN`
  - `[x]` Add the `valence-task-bot` service definition to `render.yaml`
- `[x]` Implement Core Bot & Web Keep-Alive
  - `[x]` Create `task_bot.py` with keep-alive server, intents, and logging
  - `[x]` Set up commands.Bot lifecycle and cog loader
- `[x]` Implement Channel Moderation
  - `[x]` Implement message interceptor for `#leaderboard`, `#celebration`, and `#study-logs`
  - `[x]` Implement automatic deletion of non-YPT messages
  - `[x]` Implement DM reports to Valence (`856485470171299891`) and Ujjwal (`1403716456025165864`)
- `[x]` Implement Database Layer (`task_db.py`)
  - `[x]` Set up Firebase Firestore client access
  - `[x]` Implement schema helpers for profiles (XP, streaks) and tasks (due dates, checklist, recurrence, visibility)
- `[ ]` Implement Cog Commands (`task_cogs/tasks.py`)
  - `[ ]` Implement `/task add`, `/task edit`, `/task delete`, and `/task complete` (XP awards)
  - `[ ]` Implement `/task list` (paginated, filtered, ephemeral private tasks)
  - `[ ]` Implement `/task share` (collaborative task sharing)
  - `[ ]` Implement `/task checklist` (subtask management)
  - `[ ]` Implement `/task focus` (Pomodoro focus timer integration)
  - `[ ]` Implement `/task dashboard` and `/task leaderboard`
  - `[ ]` Implement `/task habit` (recurring tasks and auto-reset)
- `[ ]` Verification & QA
  - `[x]` Create `scratch/test_task_bot.py` for database and XP math dry-run
  - `[ ]` Ensure code is clean and error-free

