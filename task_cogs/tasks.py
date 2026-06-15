import os
import io
import re
import datetime
import asyncio
import logging
import time
import discord
from discord.ext import commands, tasks
from discord import app_commands
import matplotlib
matplotlib.use('Agg')
from matplotlib.figure import Figure
import task_db

logger = logging.getLogger("TaskBot.tasks")

# --- UI Helpers & Modals ---

class AddSubtaskModal(discord.ui.Modal, title="➕ Add Subtask Checklist Item"):
    subtask_name = discord.ui.TextInput(
        label="Subtask Name",
        placeholder="e.g. Read Chapter 4 and solve exercises",
        required=True,
        max_length=100
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        name = self.subtask_name.value
        checklist = self.parent_view.task.get("checklist") or []
        checklist.append({"item": name, "done": False})
        
        # Save to DB
        success = await asyncio.to_thread(
            task_db.update_task,
            self.parent_view.task_id,
            {"checklist": checklist}
        )
        if success:
            self.parent_view.task["checklist"] = checklist
            await self.parent_view.refresh_view(interaction)
        else:
            await interaction.response.send_message("❌ Failed to add subtask.", ephemeral=True)


class ToggleSubtaskSelect(discord.ui.Select):
    def __init__(self, checklist, parent_view):
        options = []
        for i, item in enumerate(checklist):
            status_emoji = "✅" if item.get("done") else "⬜"
            options.append(discord.SelectOption(
                label=f"{i+1}. {item.get('item')[:70]}",
                value=str(i),
                emoji=status_emoji
            ))
        super().__init__(
            placeholder="Select a subtask checklist item to toggle...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        checklist = self.parent_view.task.get("checklist") or []
        if 0 <= idx < len(checklist):
            checklist[idx]["done"] = not checklist[idx]["done"]
            
            # Save to DB
            success = await asyncio.to_thread(
                task_db.update_task,
                self.parent_view.task_id,
                {"checklist": checklist}
            )
            if success:
                self.parent_view.task["checklist"] = checklist
                await self.parent_view.refresh_view(interaction)
            else:
                await interaction.response.send_message("❌ Failed to toggle subtask.", ephemeral=True)


class TaskDetailView(discord.ui.View):
    """View managing the interactive detailed task view card."""
    def __init__(self, task, caller_id):
        super().__init__(timeout=120)
        self.task = task
        self.task_id = task.get("task_id")
        self.caller_id = caller_id
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        
        checklist = self.task.get("checklist") or []
        if checklist:
            self.add_item(ToggleSubtaskSelect(checklist, self))

        self.add_item(self.btn_complete)
        self.add_item(self.btn_focus)
        self.add_item(self.btn_add_subtask)
        self.add_item(self.btn_delete)

        if self.task.get("status") == "completed":
            self.btn_complete.disabled = True
            self.btn_focus.disabled = True
            self.btn_add_subtask.disabled = True

    def get_embed(self) -> discord.Embed:
        t = self.task
        status_emoji = "✅ Completed" if t.get("status") == "completed" else "⏳ Pending"
        priority_emoji = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
        private_emoji = "🔒 Private" if t.get("is_private") else "🔓 Public"
        
        color = 0x5865F2 if t.get("user_id") == "856485470171299891" else 0xEB459E

        embed = discord.Embed(
            title=f"📋 Task details: {t.get('title')}",
            description=t.get("description") or "*No description provided.*",
            color=color
        )
        embed.add_field(name="Task ID", value=f"`{t.get('task_id')}`", inline=False)
        embed.add_field(name="Status", value=status_emoji, inline=True)
        embed.add_field(name="Priority", value=f"{priority_emoji} {t.get('priority')}", inline=True)
        embed.add_field(name="Visibility", value=private_emoji, inline=True)
        embed.add_field(name="Category", value=f"📁 {t.get('category', 'General')}", inline=True)
        
        if t.get("due_date"):
            embed.add_field(name="Due Date", value=f"📅 {t.get('due_date')}", inline=True)
        if t.get("is_habit"):
            embed.add_field(name="Habit Recurrence", value=f"🔁 {t.get('recurrence', 'daily').capitalize()}", inline=True)
            
        embed.add_field(name="Pomodoros Completed", value=f"🍅 {t.get('pomodoros_completed', 0)} / {t.get('pomodoros_estimated', 1)} estimated", inline=False)

        checklist = t.get("checklist") or []
        if checklist:
            done_count = sum(1 for x in checklist if x.get("done"))
            lines = []
            for i, x in enumerate(checklist, start=1):
                icon = "✅" if x.get("done") else "⬜"
                lines.append(f"{icon} `{i}.` {x.get('item')}")
            embed.add_field(name=f" Checklist Progress ({done_count}/{len(checklist)})", value="\n".join(lines), inline=False)
            
        return embed

    async def refresh_view(self, interaction: discord.Interaction):
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.caller_id:
            await interaction.response.send_message("❌ You cannot control this task detail card.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Complete", style=discord.ButtonStyle.green, row=1)
    async def btn_complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        success, task, stats = await asyncio.to_thread(task_db.complete_task, str(interaction.user.id), self.task_id)
        if success:
            self.task = task
            self.update_buttons()
            
            xp_msg = f"🛡️ **+{stats['xp_gained']} XP** | 🔥 **Streak: {stats['streak']} days**"
            if stats.get("level_ups", 0) > 0:
                xp_msg += f"\n🎉 **LEVEL UP!** You reached **Level {stats['new_level']}**!"
                
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
            await interaction.followup.send(f"🎉 Task completed successfully!\n{xp_msg}", ephemeral=self.task.get("is_private"))
        else:
            await interaction.response.send_message("❌ Failed to complete task. (Is it already completed?)", ephemeral=True)

    @discord.ui.button(label="Start Focus (25m)", style=discord.ButtonStyle.blurple, row=1)
    async def btn_focus(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Tasks")
        if cog:
            await cog.start_focus_timer(interaction, self.task)
        else:
            await interaction.response.send_message("❌ Task cog is not loaded.", ephemeral=True)

    @discord.ui.button(label="Add Subtask", style=discord.ButtonStyle.gray, row=1)
    async def btn_add_subtask(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddSubtaskModal(self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.red, row=1)
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        confirm_embed = discord.Embed(
            title="⚠️ Delete Task Confirmation",
            description=f"Are you absolutely sure you want to delete the task: **{self.task.get('title')}**?",
            color=discord.Color.red()
        )
        
        class DeleteConfirmView(discord.ui.View):
            def __init__(self, parent_view):
                super().__init__(timeout=30)
                self.parent_view = parent_view
                
            @discord.ui.button(label="Yes, Delete", style=discord.ButtonStyle.danger)
            async def yes_btn(self, sub_interaction: discord.Interaction, btn: discord.ui.Button):
                success = await asyncio.to_thread(task_db.delete_task, self.parent_view.task_id)
                if success:
                    await sub_interaction.response.edit_message(content="🗑️ Task deleted successfully.", embed=None, view=None)
                else:
                    await sub_interaction.response.send_message("❌ Failed to delete task.", ephemeral=True)
                    
            @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.secondary)
            async def no_btn(self, sub_interaction: discord.Interaction, btn: discord.ui.Button):
                await sub_interaction.response.edit_message(content="❌ Deletion cancelled.", embed=None, view=None)
                
        view = DeleteConfirmView(self)
        await interaction.response.send_message(embed=confirm_embed, view=view, ephemeral=self.task.get("is_private"))


class TaskPaginationView(discord.ui.View):
    """Pagination view listing multiple tasks."""
    def __init__(self, tasks_list, target_user, caller_id, per_page=5):
        super().__init__(timeout=60.0)
        self.tasks = tasks_list
        self.target_user = target_user
        self.caller_id = caller_id
        self.per_page = per_page
        self.current_page = 1
        self.total_pages = max(1, (len(tasks_list) + per_page - 1) // per_page)
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.current_page <= 1)
        self.next_button.disabled = (self.current_page >= self.total_pages)

    def get_embed(self) -> discord.Embed:
        color = 0x5865F2 if self.target_user.id == 856485470171299891 else 0xEB459E
        embed = discord.Embed(
            title=f"📋 Task Board for {self.target_user.display_name}",
            color=color
        )
        
        if not self.tasks:
            embed.description = "*No tasks pending.*"
            return embed

        start_idx = (self.current_page - 1) * self.per_page
        end_idx = start_idx + self.per_page
        page_tasks = self.tasks[start_idx:end_idx]

        lines = []
        for i, t in enumerate(page_tasks, start=start_idx + 1):
            status_emoji = "✅" if t.get("status") == "completed" else "⏳"
            prio_emoji = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
            priv_lbl = "🔒 [Private] " if t.get("is_private") else ""
            habit_lbl = "🔁 " if t.get("is_habit") else ""
            due_lbl = f" *(Due: {t.get('due_date')})*" if t.get("due_date") else ""
            
            lines.append(
                f"**{i}. `{t.get('task_id')[:8]}`** | {status_emoji} {prio_emoji} {priv_lbl}{habit_lbl}**{t.get('title')}**{due_lbl}"
            )
            
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Page {self.current_page}/{self.total_pages} | Total: {len(self.tasks)} tasks")
        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.primary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.caller_id:
            await interaction.response.send_message("❌ You cannot control this list view.", ephemeral=True)
            return False
        return True


class DMSnoozeView(discord.ui.View):
    """View sent in DMs allowing the user to complete or snooze a task alert."""
    def __init__(self, task_id, user_id):
        super().__init__(timeout=86400) # long timeout
        self.task_id = task_id
        self.user_id = str(user_id)

    @discord.ui.button(label="Complete", style=discord.ButtonStyle.green)
    async def btn_complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        success, task, stats = await asyncio.to_thread(task_db.complete_task, self.user_id, self.task_id)
        if success:
            xp_msg = f"🛡️ **+{stats['xp_gained']} XP** | 🔥 **Streak: {stats['streak']} days**"
            if stats.get("level_ups", 0) > 0:
                xp_msg += f"\n🎉 **LEVEL UP!** You reached **Level {stats['new_level']}**!"
            
            # Update embed to indicate completion
            embed = interaction.message.embeds[0]
            embed.title = f"🎉 Task Completed: {task.get('title')}"
            embed.color = discord.Color.gold()
            embed.description = f"Completed successfully!\n\n{xp_msg}"
            
            # Disable all buttons
            for item in self.children:
                item.disabled = True
            
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message("❌ Failed to complete task. (Is it already completed?)", ephemeral=True)

    @discord.ui.button(label="Snooze 1 Hour", style=discord.ButtonStyle.blurple)
    async def btn_snooze_1h(self, interaction: discord.Interaction, button: discord.ui.Button):
        task = await asyncio.to_thread(task_db.get_task, self.task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        now = task_db.get_ist_now()
        new_due = now + datetime.timedelta(hours=1)
        new_due_str = new_due.strftime("%Y-%m-%d %H:%M")
        
        success = await asyncio.to_thread(
            task_db.update_task,
            self.task_id,
            {"due_date": new_due_str, "due_warning_sent": False}
        )
        if success:
            embed = interaction.message.embeds[0]
            embed.title = f"⏳ Task Snoozed: {task.get('title')}"
            embed.description = f"Snoozed for 1 hour. New due date: `{new_due_str} IST`"
            embed.color = discord.Color.blue()
            
            for item in self.children:
                item.disabled = True
                
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message("❌ Failed to snooze task.", ephemeral=True)

    @discord.ui.button(label="Snooze 1 Day", style=discord.ButtonStyle.secondary)
    async def btn_snooze_1d(self, interaction: discord.Interaction, button: discord.ui.Button):
        task = await asyncio.to_thread(task_db.get_task, self.task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        now = task_db.get_ist_now()
        new_due = now + datetime.timedelta(days=1)
        new_due_str = new_due.strftime("%Y-%m-%d %H:%M")
        
        success = await asyncio.to_thread(
            task_db.update_task,
            self.task_id,
            {"due_date": new_due_str, "due_warning_sent": False}
        )
        if success:
            embed = interaction.message.embeds[0]
            embed.title = f"⏳ Task Snoozed: {task.get('title')}"
            embed.description = f"Snoozed for 1 day. New due date: `{new_due_str} IST`"
            embed.color = discord.Color.blue()
            
            for item in self.children:
                item.disabled = True
                
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message("❌ Failed to snooze task.", ephemeral=True)


class DMPendingSelect(discord.ui.Select):
    def __init__(self, tasks_list, caller_id):
        options = []
        for t in tasks_list[:25]:
            prio_emoji = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
            options.append(discord.SelectOption(
                label=t.get("title")[:100],
                value=t.get("task_id"),
                description=f"Priority: {t.get('priority')} | Category: {t.get('category')}",
                emoji=prio_emoji
            ))
        super().__init__(
            placeholder="Select a task to view details...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.caller_id = caller_id

    async def callback(self, interaction: discord.Interaction):
        task_id = self.values[0]
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        view = TaskDetailView(task, self.caller_id)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)


class DMFocusSelect(discord.ui.Select):
    def __init__(self, tasks_list, cog):
        options = []
        for t in tasks_list[:25]:
            options.append(discord.SelectOption(
                label=t.get("title")[:100],
                value=t.get("task_id"),
                description=f"Estimated Poms: {t.get('pomodoros_estimated', 1)}"
            ))
        super().__init__(
            placeholder="Select a task to focus on...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        task_id = self.values[0]
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        await self.cog.start_focus_timer(interaction, task)


class DMQuickAddModal(discord.ui.Modal, title="➕ Quick Add Task"):
    task_title = discord.ui.TextInput(
        label="Task Title",
        placeholder="What do you need to do?",
        required=True,
        max_length=100
    )
    task_desc = discord.ui.TextInput(
        label="Description",
        placeholder="Add details (optional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500
    )
    task_due = discord.ui.TextInput(
        label="Due Date",
        placeholder="e.g. '2h', 'tomorrow', 'YYYY-MM-DD HH:MM' (optional)",
        required=False,
        max_length=50
    )
    task_prio = discord.ui.TextInput(
        label="Priority (Low / Medium / High)",
        placeholder="Medium",
        required=False,
        max_length=10
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        title = self.task_title.value
        desc = self.task_desc.value or ""
        due = self.task_due.value or None
        priority = self.task_prio.value or "Medium"
        
        priority = priority.strip().capitalize()
        if priority not in ["Low", "Medium", "High"]:
            priority = "Medium"
            
        due_str = due
        if due:
            parsed_dt = self.cog.parse_due_date_input(due)
            if not parsed_dt:
                await interaction.response.send_message("❌ Invalid due date format. Use '2h', 'tomorrow', or 'YYYY-MM-DD HH:MM'.", ephemeral=True)
                return
            due_str = parsed_dt.strftime("%Y-%m-%d %H:%M")
            
        task_id = await asyncio.to_thread(
            task_db.add_task,
            user_id=str(interaction.user.id),
            title=title,
            description=desc,
            due_date=due_str,
            priority=priority,
            category="General",
            is_private=True,
            recurrence="none",
            is_habit=False,
            pomodoros_estimated=1
        )
        
        embed = discord.Embed(
            title="🔒 Task Added from DM",
            description=f"**Title:** {title}\n**ID:** `{task_id}`",
            color=discord.Color.purple()
        )
        embed.add_field(name="Priority", value=priority, inline=True)
        if due_str:
            embed.add_field(name="Due Date", value=f"📅 `{due_str}`", inline=True)
            
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DMHomeView(discord.ui.View):
    """Interactive home panel inside user DMs."""
    def __init__(self, user: discord.User, cog):
        super().__init__(timeout=300)
        self.user = user
        self.cog = cog
        
    @discord.ui.button(label="📋 View Pending", style=discord.ButtonStyle.primary, emoji="📋")
    async def btn_pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(self.user.id)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str, "pending")
        if not tasks_list:
            await interaction.response.send_message("📭 You have no pending tasks.", ephemeral=True)
            return
            
        embed = discord.Embed(
            title="📋 Select a Pending Task",
            description="Choose a task from the dropdown below to view details and manage it.",
            color=discord.Color.blurple()
        )
        view = discord.ui.View(timeout=120)
        view.add_item(DMPendingSelect(tasks_list, self.user.id))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="➕ Quick Add", style=discord.ButtonStyle.success, emoji="➕")
    async def btn_quick_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = DMQuickAddModal(self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="📈 My Graph", style=discord.ButtonStyle.secondary, emoji="📈")
    async def btn_graph(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id_str = str(self.user.id)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        completed = [t for t in tasks_list if t.get("status") == "completed"]
        file = self.cog.generate_productivity_chart(completed)
        
        color = 0x5865F2 if user_id_str == "856485470171299891" else 0xEB459E
        embed = discord.Embed(
            title="📈 Weekly Productivity Report",
            description="Your daily completed task trends over the last 7 days.",
            color=color
        )
        embed.set_image(url="attachment://productivity_graph.png")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    @discord.ui.button(label="🏆 Leaderboard", style=discord.ButtonStyle.secondary, emoji="🏆")
    async def btn_leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        leaderboard = await asyncio.to_thread(task_db.get_leaderboard)
        if not leaderboard:
            await interaction.response.send_message("📭 Leaderboard is empty.", ephemeral=True)
            return
            
        embed = discord.Embed(
            title="🏆 Server Productivity Leaderboard",
            description="Ranked by total productivity level and accumulated XP.",
            color=discord.Color.gold()
        )
        
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for idx, entry in enumerate(leaderboard):
            medal = medals[idx] if idx < 3 else f"`#{idx+1}`"
            user = self.cog.bot.get_user(int(entry["user_id"]))
            username = user.display_name if user else f"User {entry['user_id']}"
            lines.append(
                f"{medal} **{username}** — Level {entry['level']} | {entry['xp']} XP | 🔥 {entry['streak']}d streak"
            )
            
        embed.description = "\n\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🍅 Focus Pomodoro", style=discord.ButtonStyle.secondary, emoji="🍅")
    async def btn_focus(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(self.user.id)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str, "pending")
        if not tasks_list:
            await interaction.response.send_message("📭 You have no pending tasks to focus on.", ephemeral=True)
            return
            
        embed = discord.Embed(
            title="🍅 Start a Focus Session",
            description="Choose a task from the dropdown below to start a 25-minute Pomodoro focus timer.",
            color=discord.Color.orange()
        )
        view = discord.ui.View(timeout=120)
        view.add_item(DMFocusSelect(tasks_list, self.cog))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# --- Tasks Cog Class ---

class TasksCog(commands.Cog, name="Tasks"):
    """Cog managing all Task commands, checklists, focus, habits, and weekly graphs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_focus_sessions = {}  # user_id -> TaskFocusTimer object
        self.habit_reset_loop.start()
        self.reminder_task.start()
        self.planner_alerts_task.start()

    def cog_unload(self):
        self.habit_reset_loop.cancel()
        self.reminder_task.cancel()
        self.planner_alerts_task.cancel()

    # --- Parent Task Command Group ---
    task_group = app_commands.Group(name="task", description="Valence Task Bot Commands")

    # --- Background Loops ---

    @tasks.loop(minutes=5)
    async def habit_reset_loop(self):
        """Dynamic completed-habit reset checks in IST."""
        now = task_db.get_ist_now()
        if now.hour == 0 and now.minute < 6:
            logger.info("[HABITS] Checking for habits to reset...")
            completed_habits = await asyncio.to_thread(task_db.fetch_completed_habits)
            for h in completed_habits:
                rec = h.get("recurrence", "daily")
                should_reset = False
                if rec == "daily":
                    should_reset = True
                elif rec == "weekly" and now.weekday() == 0:
                    should_reset = True
                    
                if should_reset:
                    await asyncio.to_thread(task_db.reset_habit, h["task_id"])
                    logger.info(f"[HABITS] Reset habit task '{h.get('title')}' (ID: {h['task_id']})")

    @tasks.loop(minutes=1)
    async def reminder_task(self):
        """Background pending task due warnings (due in < 1h) and custom reminders."""
        try:
            pending_tasks = await asyncio.to_thread(task_db.get_all_pending_tasks)
            now = task_db.get_ist_now()
            for task in pending_tasks:
                user_id_str = task.get("user_id")
                task_id = task.get("task_id")
                
                # 1. Custom Reminders (remind_at)
                remind_at_str = task.get("remind_at")
                if remind_at_str:
                    parsed_remind = None
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                        try:
                            parsed_remind = datetime.datetime.strptime(remind_at_str.strip(), fmt)
                            break
                        except ValueError:
                            continue
                    if parsed_remind and parsed_remind <= now:
                        # Clear reminder first so it doesn't fire again
                        await asyncio.to_thread(task_db.update_task, task_id, {"remind_at": None})
                        try:
                            user = await self.bot.fetch_user(int(user_id_str))
                            if user:
                                embed = discord.Embed(
                                    title="🔔 Custom Task Reminder",
                                    description=f"This is your scheduled reminder for: **{task.get('title')}**",
                                    color=discord.Color.gold()
                                )
                                if task.get("description"):
                                    embed.add_field(name="Description", value=task.get("description"), inline=False)
                                if task.get("due_date"):
                                    embed.add_field(name="Due Date (IST)", value=f"`{task.get('due_date')}`", inline=True)
                                
                                view = DMSnoozeView(task_id, user_id_str)
                                await user.send(embed=embed, view=view)
                                logger.info(f"[REMINDER] Sent custom reminder to {user_id_str} for task '{task.get('title')}'")
                        except Exception as e:
                            logger.error(f"[REMINDER] Failed to send custom reminder to {user_id_str}: {e}")
                
                # 2. Hourly Due Warnings (due in < 1h)
                due_date_str = task.get("due_date")
                if due_date_str and not task.get("due_warning_sent"):
                    parsed_dt = None
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                        try:
                            parsed_dt = datetime.datetime.strptime(due_date_str.strip(), fmt)
                            break
                        except ValueError:
                            continue
                    if parsed_dt:
                        diff = parsed_dt - now
                        if datetime.timedelta(seconds=0) < diff <= datetime.timedelta(hours=1):
                            # Set due_warning_sent to True
                            await asyncio.to_thread(task_db.update_task, task_id, {"due_warning_sent": True})
                            try:
                                user = await self.bot.fetch_user(int(user_id_str))
                                if user:
                                    embed = discord.Embed(
                                        title="⚠️ Task Due Alert",
                                        description=f"Your task **{task.get('title')}** is due in less than 1 hour!",
                                        color=discord.Color.orange()
                                    )
                                    embed.add_field(name="Due Date (IST)", value=f"`{due_date_str}`")
                                    
                                    view = DMSnoozeView(task_id, user_id_str)
                                    await user.send(embed=embed, view=view)
                                    logger.info(f"[REMINDER] Sent due warning to {user_id_str} for task '{task.get('title')}'")
                            except Exception as e:
                                logger.error(f"[REMINDER] Failed to warn user {user_id_str}: {e}")
        except Exception as e:
            logger.error(f"[REMINDER] Error in loop: {e}")

    @tasks.loop(hours=1)
    async def planner_alerts_task(self):
        """Hourly background task managing morning agendas, streak nudges, and weekly digests in IST."""
        now = task_db.get_ist_now()
        logger.info(f"[PLANNER] Running hourly planner checks (IST Hour: {now.hour}, Day: {now.weekday()})...")
        
        user_ids = ["856485470171299891", "1403716456025165864"]
        
        # 1. Daily Morning Agenda DM (8:00 AM IST)
        if now.hour == 8:
            for uid_str in user_ids:
                try:
                    user_id = int(uid_str)
                    user = await self.bot.fetch_user(user_id)
                    if not user:
                        continue
                        
                    profile = await asyncio.to_thread(task_db.get_user_profile, uid_str)
                    tasks_list = await asyncio.to_thread(task_db.get_user_tasks, uid_str)
                    
                    pending = [t for t in tasks_list if t.get("status") == "pending"]
                    habits = [t for t in tasks_list if t.get("is_habit") and t.get("status") == "pending"]
                    sorted_pending = sorted(pending, key=lambda x: {"High": 3, "Medium": 2, "Low": 1}.get(x.get("priority", "Medium"), 2), reverse=True)
                    
                    color = 0x5865F2 if uid_str == "856485470171299891" else 0xEB459E
                    embed = discord.Embed(
                        title=f"☀️ Good Morning, {user.display_name}! Here is your Agenda",
                        description=f"Start your day strong! Current streak: 🔥 **{profile.get('streak', 0)} days**",
                        color=color
                    )
                    
                    if habits:
                        h_lines = [f"- **{h.get('title')}** (🔁 {h.get('recurrence', 'daily')})" for h in habits[:4]]
                        embed.add_field(name="🔁 Daily Habits to Complete", value="\n".join(h_lines), inline=False)
                    else:
                        embed.add_field(name="🔁 Daily Habits to Complete", value="*No habits registered.*", inline=False)
                        
                    if sorted_pending:
                        t_lines = []
                        for t in sorted_pending[:5]:
                            due = f" *(Due: {t['due_date']})*" if t.get("due_date") else ""
                            priv = "🔒 " if t.get("is_private") else ""
                            t_lines.append(f"- `[{t.get('priority')}]` {priv}**{t.get('title')}**{due}")
                        embed.add_field(name="📋 Pending Tasks", value="\n".join(t_lines), inline=False)
                    else:
                        embed.add_field(name="📋 Pending Tasks", value="🎉 *All caught up! No pending tasks.*", inline=False)
                        
                    embed.set_footer(text="Manage tasks using /task board in the server.")
                    await user.send(embed=embed)
                    logger.info(f"[PLANNER] Morning agenda sent to user {uid_str}")
                except Exception as e:
                    logger.error(f"[PLANNER] Failed to send morning agenda to user {uid_str}: {e}")
                    
        # 2. Daily Evening Streak Nudge DM (9:00 PM / 21:00 IST)
        if now.hour == 21:
            for uid_str in user_ids:
                try:
                    profile = await asyncio.to_thread(task_db.get_user_profile, uid_str)
                    streak = profile.get("streak", 0)
                    last_completed = profile.get("last_completed_date")
                    today_str = task_db.get_ist_date_str()
                    
                    if streak > 0 and last_completed != today_str:
                        user_id = int(uid_str)
                        user = await self.bot.fetch_user(user_id)
                        if user:
                            embed = discord.Embed(
                                title="🔥 Streak Saving Alert!",
                                description=(
                                    f"Hey {user.display_name}! You haven't checked off any tasks or habits today.\n\n"
                                    f"Complete at least one task before midnight to keep your **{streak}-day streak** alive! You have 3 hours left! ⏳"
                                ),
                                color=discord.Color.red()
                            )
                            await user.send(embed=embed)
                            logger.info(f"[PLANNER] Streak nudge sent to user {uid_str}")
                except Exception as e:
                    logger.error(f"[PLANNER] Failed to check/send streak nudge to user {uid_str}: {e}")

        # 3. Weekly Sunday Digest DM (Sundays 8:00 PM / 20:00 IST)
        if now.hour == 20 and now.weekday() == 6:
            for uid_str in user_ids:
                try:
                    user_id = int(uid_str)
                    user = await self.bot.fetch_user(user_id)
                    if not user:
                        continue
                        
                    tasks_list = await asyncio.to_thread(task_db.get_user_tasks, uid_str)
                    completed = [t for t in tasks_list if t.get("status") == "completed"]
                    
                    file = self.generate_productivity_chart(completed)
                    
                    color = 0x5865F2 if uid_str == "856485470171299891" else 0xEB459E
                    embed = discord.Embed(
                        title="📈 Your Weekly Productivity Digest",
                        description=f"Congratulations on a focused week! Here is your weekly completions graph.",
                        color=color
                    )
                    embed.set_image(url="attachment://productivity_graph.png")
                    await user.send(embed=embed, file=file)
                    logger.info(f"[PLANNER] Weekly digest DM sent to user {uid_str}")
                except Exception as e:
                    logger.error(f"[PLANNER] Failed to send weekly digest to user {uid_str}: {e}")

    # --- Subcommands ---

    # 1. ADD TASK
    @task_group.command(name="add", description="Add a new public or private task")
    @app_commands.describe(
        title="Title of the task",
        description="Detailed description",
        due_date="Due date (e.g. '2h', 'tomorrow', 'YYYY-MM-DD HH:MM')",
        priority="Task priority",
        category="Category of the task",
        private="Set to True if you want ONLY you to see this task",
        is_habit="Is this a recurring habit?",
        recurrence="Recurrence frequency for habits",
        pomodoros="Estimated Pomodoros"
    )
    @app_commands.choices(
        priority=[
            app_commands.Choice(name="Low", value="Low"),
            app_commands.Choice(name="Medium", value="Medium"),
            app_commands.Choice(name="High", value="High")
        ],
        recurrence=[
            app_commands.Choice(name="None", value="none"),
            app_commands.Choice(name="Daily", value="daily"),
            app_commands.Choice(name="Weekly", value="weekly")
        ]
    )
    async def task_add(self, interaction: discord.Interaction, title: str, description: str = "", 
                       due_date: str = None, priority: str = "Medium", category: str = "General", 
                       private: bool = False, is_habit: bool = False, recurrence: str = "none", pomodoros: int = 1):
        
        due_str = due_date
        if due_date:
            parsed_dt = self.parse_due_date_input(due_date)
            if not parsed_dt:
                await interaction.response.send_message("❌ Invalid due date format. Use formats like '2h', 'tomorrow', or 'YYYY-MM-DD HH:MM'.", ephemeral=True)
                return
            due_str = parsed_dt.strftime("%Y-%m-%d %H:%M")

        task_id = await asyncio.to_thread(
            task_db.add_task,
            user_id=str(interaction.user.id),
            title=title,
            description=description,
            due_date=due_str,
            priority=priority,
            category=category,
            is_private=private,
            recurrence=recurrence,
            is_habit=is_habit,
            pomodoros_estimated=pomodoros
        )

        embed = discord.Embed(
            title="✅ Task Added Successfully!",
            description=f"**Title:** {title}\n**ID:** `{task_id}`",
            color=discord.Color.green() if not private else discord.Color.purple()
        )
        embed.add_field(name="Priority", value=priority, inline=True)
        embed.add_field(name="Category", value=category, inline=True)
        embed.add_field(name="Private", value="🔒 Yes" if private else "🔓 No", inline=True)
        if due_str:
            embed.add_field(name="Due Date", value=f"📅 `{due_str}`", inline=True)
        if is_habit:
            embed.add_field(name="Habit", value=f"🔁 {recurrence.capitalize()}", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=private)

    # 2. VIEW TASK DETAILS
    @task_group.command(name="view", description="View and manage task details interactively")
    @app_commands.describe(task_id="The UUID of the task to view")
    async def task_view(self, interaction: discord.Interaction, task_id: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return

        user_id_str = str(interaction.user.id)
        is_owner = task.get("user_id") == user_id_str
        is_shared = user_id_str in task.get("shared_with", [])
        
        if task.get("is_private") and not is_owner:
            await interaction.response.send_message("❌ This task is private and cannot be viewed by others.", ephemeral=True)
            return
            
        if not is_owner and not is_shared:
            await interaction.response.send_message("❌ You do not have permission to view this task.", ephemeral=True)
            return

        view = TaskDetailView(task, interaction.user.id)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=task.get("is_private"))

    # 3. EDIT TASK
    @task_group.command(name="edit", description="Modify details of an existing task")
    @app_commands.describe(
        task_id="The UUID of the task",
        title="New title",
        description="New description",
        due_date="New due date ('2h', 'tomorrow', 'YYYY-MM-DD HH:MM')",
        priority="New priority",
        category="New category",
        private="Set private visibility"
    )
    @app_commands.choices(
        priority=[
            app_commands.Choice(name="Low", value="Low"),
            app_commands.Choice(name="Medium", value="Medium"),
            app_commands.Choice(name="High", value="High")
        ]
    )
    async def task_edit(self, interaction: discord.Interaction, task_id: str, title: str = None, 
                        description: str = None, due_date: str = None, priority: str = None, 
                        category: str = None, private: bool = None):
        
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return

        updates = {}
        if title is not None: updates["title"] = title
        if description is not None: updates["description"] = description
        if priority is not None: updates["priority"] = priority
        if category is not None: updates["category"] = category
        if private is not None: updates["is_private"] = private
        
        if due_date is not None:
            parsed_dt = self.parse_due_date_input(due_date)
            if not parsed_dt:
                await interaction.response.send_message("❌ Invalid due date format. Use '2h', 'tomorrow', or 'YYYY-MM-DD HH:MM'.", ephemeral=True)
                return
            updates["due_date"] = parsed_dt.strftime("%Y-%m-%d %H:%M")

        if not updates:
            await interaction.response.send_message("⚠️ No update fields were provided.", ephemeral=True)
            return

        await asyncio.to_thread(task_db.update_task, task_id, updates)
        
        embed = discord.Embed(
            title="✏️ Task Updated Successfully",
            description=f"Task `{task_id[:8]}...` has been modified.",
            color=discord.Color.blue()
        )
        for k, v in updates.items():
            embed.add_field(name=k.replace('_', ' ').capitalize(), value=str(v), inline=True)
            
        await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))

    # 4. DELETE TASK
    @task_group.command(name="delete", description="Delete a task")
    @app_commands.describe(task_id="The UUID of the task")
    async def task_delete(self, interaction: discord.Interaction, task_id: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return
            
        await asyncio.to_thread(task_db.delete_task, task_id)
        await interaction.response.send_message(f"🗑️ Task `{task_id}` deleted.", ephemeral=task.get("is_private"))

    # 5. COMPLETE TASK
    @task_group.command(name="complete", description="Complete a task and earn XP")
    @app_commands.describe(task_id="The UUID of the task")
    async def task_complete(self, interaction: discord.Interaction, task_id: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        user_id_str = str(interaction.user.id)
        if task.get("user_id") != user_id_str and user_id_str not in task.get("shared_with", []):
            await interaction.response.send_message("❌ You do not have permission to complete this task.", ephemeral=True)
            return
            
        success, completed_task, stats = await asyncio.to_thread(task_db.complete_task, user_id_str, task_id)
        if not success:
            await interaction.response.send_message("❌ Failed to complete task. Is it already completed?", ephemeral=True)
            return
            
        embed = discord.Embed(
            title="🎉 Task Completed!",
            description=f"**{completed_task['title']}**",
            color=discord.Color.gold()
        )
        embed.add_field(name="XP Gained", value=f"✨ +{stats['xp_gained']} XP")
        embed.add_field(name="Daily Streak", value=f"🔥 {stats['streak']} days")
        embed.add_field(name="New Level", value=f"📈 Level {stats['new_level']}")
        
        await interaction.response.send_message(embed=embed, ephemeral=completed_task.get("is_private"))

    # 6. LIST TASKS
    @task_group.command(name="list", description="List your tasks with pagination")
    @app_commands.describe(
        status="Filter tasks by status",
        category="Filter tasks by category",
        priority="Filter tasks by priority",
        show_private="Show your private tasks (Forces ephemeral view)"
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Pending", value="pending"),
            app_commands.Choice(name="Completed", value="completed")
        ],
        priority=[
            app_commands.Choice(name="Low", value="Low"),
            app_commands.Choice(name="Medium", value="Medium"),
            app_commands.Choice(name="High", value="High")
        ]
    )
    async def task_list(self, interaction: discord.Interaction, status: str = None, 
                        category: str = None, priority: str = None, show_private: bool = False):
        
        user_id_str = str(interaction.user.id)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str, status, category, priority)
        
        filtered = []
        for t in tasks_list:
            is_owner = t.get("user_id") == user_id_str
            if t.get("is_private"):
                if show_private and is_owner:
                    filtered.append(t)
            else:
                filtered.append(t)
                
        if not filtered:
            await interaction.response.send_message("📝 No tasks found matching your filters.", ephemeral=show_private)
            return
            
        view = TaskPaginationView(filtered, interaction.user, interaction.user.id)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=show_private)

    # 7. SHARE TASK
    @task_group.command(name="share", description="Share a public task with another server user")
    @app_commands.describe(task_id="The UUID of the task", user="The user to collaborate with")
    async def task_share(self, interaction: discord.Interaction, task_id: str, user: discord.Member):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return
            
        if task.get("is_private"):
            await interaction.response.send_message("❌ You cannot share a private task. Toggle it to public first.", ephemeral=True)
            return
            
        shared_list = task.get("shared_with") or []
        user_id_str = str(user.id)
        if user_id_str in shared_list:
            await interaction.response.send_message(f"⚠️ Task is already shared with {user.display_name}.", ephemeral=True)
            return
            
        shared_list.append(user_id_str)
        await asyncio.to_thread(task_db.update_task, task_id, {"shared_with": shared_list})
        
        await interaction.response.send_message(f"🤝 Task **{task['title']}** is now shared with {user.mention}!")

    # 8. FOCUS TIMERS GROUP
    focus_group = app_commands.Group(name="focus", description="Focus timer control commands")

    @focus_group.command(name="start", description="Start a focus session (Pomodoro) tied to a task")
    @app_commands.describe(task_id="The UUID of the task to focus on", duration="Duration in minutes (default 25)")
    async def focus_start(self, interaction: discord.Interaction, task_id: str, duration: int = 25):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        await self.start_focus_timer(interaction, task, duration)

    @focus_group.command(name="cancel", description="Cancel your active focus session")
    async def focus_cancel(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id not in self.active_focus_sessions:
            await interaction.response.send_message("❌ You do not have an active focus session.", ephemeral=True)
            return
            
        loop_task = self.active_focus_sessions.pop(user_id)
        loop_task.cancel()
        
        await interaction.response.send_message("🛑 Focus session cancelled successfully. No XP awarded.", ephemeral=True)

    # 9. DASHBOARD
    @task_group.command(name="dashboard", description="View your productivity level, streaks, and agenda")
    async def task_dashboard(self, interaction: discord.Interaction):
        user_id_str = str(interaction.user.id)
        profile = await asyncio.to_thread(task_db.get_user_profile, user_id_str)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        
        pending = [t for t in tasks_list if t.get("status") == "pending" and t.get("user_id") == user_id_str]
        
        xp = profile.get("xp", 0)
        level = profile.get("level", 1)
        xp_needed = level * 1000
        
        bar_len = 10
        filled = int((xp / xp_needed) * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        
        color = 0x5865F2 if user_id_str == "856485470171299891" else 0xEB459E
        embed = discord.Embed(
            title=f"📊 Productivity Dashboard — {interaction.user.display_name}",
            color=color
        )
        embed.add_field(name="Level & XP", value=f"**Level {level}**\n`[{bar}]` {xp}/{xp_needed} XP", inline=True)
        embed.add_field(name="Daily Streak", value=f"🔥 **{profile.get('streak', 0)} days**", inline=True)
        embed.add_field(name="Tasks Completed", value=f"✅ **{profile.get('total_completed', 0)} tasks**", inline=True)
        
        if pending:
            lines = []
            sorted_pending = sorted(pending, key=lambda x: {"High": 3, "Medium": 2, "Low": 1}.get(x.get("priority", "Medium"), 2), reverse=True)
            for t in sorted_pending[:4]:
                due = f" *(Due: {t['due_date']})*" if t.get("due_date") else ""
                lines.append(f"- `[{t['priority']}]` **{t['title']}**{due}")
            embed.add_field(name="📋 Next Up on Your Agenda", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📋 Next Up on Your Agenda", value="🎉 All caught up! No tasks pending.", inline=False)
            
        await interaction.response.send_message(embed=embed)

    # 10. LEADERBOARD
    @task_group.command(name="leaderboard", description="View server productivity rankings")
    async def task_leaderboard(self, interaction: discord.Interaction):
        leaderboard = await asyncio.to_thread(task_db.get_leaderboard)
        if not leaderboard:
            await interaction.response.send_message("📭 Leaderboard is empty.", ephemeral=True)
            return
            
        embed = discord.Embed(
            title="🏆 Server Productivity Leaderboard",
            description="Ranked by total productivity level and accumulated XP.",
            color=discord.Color.gold()
        )
        
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for idx, entry in enumerate(leaderboard):
            medal = medals[idx] if idx < 3 else f"`#{idx+1}`"
            user = self.bot.get_user(int(entry["user_id"]))
            username = user.display_name if user else f"User {entry['user_id']}"
            lines.append(
                f"{medal} **{username}** — Level {entry['level']} | {entry['xp']} XP | 🔥 {entry['streak']}d streak"
            )
            
        embed.description = "\n\n".join(lines)
        await interaction.response.send_message(embed=embed)

    # 11. WEEKLY PRODUCTIVITY GRAPH
    @task_group.command(name="graph", description="Show a visual trend chart of your weekly task completions")
    async def task_graph(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        user_id_str = str(interaction.user.id)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        completed = [t for t in tasks_list if t.get("status") == "completed"]
        
        file = self.generate_productivity_chart(completed)
        
        color = 0x5865F2 if user_id_str == "856485470171299891" else 0xEB459E
        embed = discord.Embed(
            title="📈 Weekly Productivity Report",
            description="Your daily completed task trends over the last 7 days.",
            color=color
        )
        embed.set_image(url="attachment://productivity_graph.png")
        
        await interaction.followup.send(embed=embed, file=file)

    # 12. HABIT CREATION
    @task_group.command(name="habit", description="Create a recurring daily or weekly habit task")
    @app_commands.describe(title="Title of the habit", recurrence="Recurrence frequency")
    @app_commands.choices(
        recurrence=[
            app_commands.Choice(name="Daily", value="daily"),
            app_commands.Choice(name="Weekly", value="weekly")
        ]
    )
    async def task_habit(self, interaction: discord.Interaction, title: str, recurrence: str):
        task_id = await asyncio.to_thread(
            task_db.add_task,
            user_id=str(interaction.user.id),
            title=title,
            priority="Medium",
            category="Habit",
            recurrence=recurrence,
            is_habit=True
        )
        
        embed = discord.Embed(
            title="🔁 Recurring Habit Registered",
            description=f"**Title:** {title}\n**Recurrence:** {recurrence.capitalize()}\n**ID:** `{task_id}`",
            color=discord.Color.teal()
        )
        embed.set_footer(text="Habits reset back to pending automatically at midnight in IST.")
        await interaction.response.send_message(embed=embed)

    # 13. CUSTOM REMINDERS
    @task_group.command(name="remind", description="Schedule a custom DM reminder for a task")
    @app_commands.describe(
        task_id="The UUID of the task",
        time="Time to remind (e.g. '10m', '2h', 'tomorrow', '15:30', or 'YYYY-MM-DD HH:MM')"
    )
    async def task_remind(self, interaction: discord.Interaction, task_id: str, time: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You do not own this task.", ephemeral=True)
            return
            
        parsed_dt = self.parse_due_date_input(time)
        if not parsed_dt:
            # Try absolute time today (e.g. "15:30")
            now = task_db.get_ist_now()
            time_match = re.match(r'^(\d{1,2}):(\d{2})$', time.strip())
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2))
                parsed_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if parsed_dt < now:
                    parsed_dt += datetime.timedelta(days=1)
            else:
                await interaction.response.send_message(
                    "❌ Invalid reminder time format. Use '10m', '2h', 'tomorrow', '15:30', or 'YYYY-MM-DD HH:MM'.", 
                    ephemeral=True
                )
                return
                
        remind_str = parsed_dt.strftime("%Y-%m-%d %H:%M")
        
        success = await asyncio.to_thread(
            task_db.update_task,
            task_id,
            {"remind_at": remind_str}
        )
        if success:
            embed = discord.Embed(
                title="🔔 Reminder Scheduled",
                description=f"You will be reminded of **{task.get('title')}** at `{remind_str} IST`",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))
        else:
            await interaction.response.send_message("❌ Failed to set reminder.", ephemeral=True)

    # --- Helper Logic methods ---

    def parse_due_date_input(self, due_str: str) -> datetime.datetime:
        due_str = due_str.strip().lower()
        now = task_db.get_ist_now()
        
        match = re.match(r'^in\s+(\d+)\s*(h|hour|hours|m|min|mins|d|day|days)$', due_str)
        if not match:
            match = re.match(r'^(\d+)\s*(h|hour|hours|m|min|mins|d|day|days)$', due_str)
            
        if match:
            val = int(match.group(1))
            unit = match.group(2)
            if 'h' in unit:
                return now + datetime.timedelta(hours=val)
            elif 'm' in unit:
                return now + datetime.timedelta(minutes=val)
            elif 'd' in unit:
                return now + datetime.timedelta(days=val)
                
        if due_str == 'tomorrow':
            tomorrow = now + datetime.timedelta(days=1)
            return tomorrow.replace(hour=12, minute=0, second=0, microsecond=0)
            
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(due_str, fmt)
                return dt
            except ValueError:
                continue
        return None

    async def start_focus_timer(self, interaction: discord.Interaction, task, duration_mins=25):
        user_id = interaction.user.id
        if user_id in self.active_focus_sessions:
            await interaction.response.send_message("❌ You already have an active focus timer.", ephemeral=True)
            return
            
        task_id = task.get("task_id")
        title = task.get("title")
        
        async def timer_coro():
            try:
                await asyncio.sleep(duration_mins * 60)
                t = await asyncio.to_thread(task_db.get_task, task_id)
                if t:
                    completed_poms = t.get("pomodoros_completed", 0) + 1
                    await asyncio.to_thread(task_db.update_task, task_id, {"pomodoros_completed": completed_poms})
                    
                new_xp, new_level, leveled_up = await asyncio.to_thread(task_db.add_xp, str(user_id), 50)
                
                try:
                    dm = await interaction.user.create_dm()
                    embed = discord.Embed(
                        title="⏱️ Focus Session Completed!",
                        description=f"Congratulations on completing {duration_mins} minutes focus for **{title}**!",
                        color=discord.Color.orange()
                    )
                    embed.add_field(name="XP Gained", value="✨ +50 XP")
                    await dm.send(embed=embed)
                    if leveled_up:
                        await dm.send(f"🚀 **LEVEL UP!** You reached **Level {new_level}**!")
                except Exception:
                    pass
            except asyncio.CancelledError:
                pass
            finally:
                if user_id in self.active_focus_sessions:
                    del self.active_focus_sessions[user_id]
                    
        loop_task = asyncio.create_task(timer_coro())
        self.active_focus_sessions[user_id] = loop_task
        
        embed = discord.Embed(
            title="🍅 Focus Session Started",
            description=f"Focusing on **{title}** for {duration_mins} minutes.\nEnds at: **{(task_db.get_ist_now() + datetime.timedelta(minutes=duration_mins)).strftime('%H:%M:%S')} IST**",
            color=discord.Color.orange()
        )
        embed.set_footer(text="The bot will DM you when the focus timer is done.")
        await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))

    def generate_productivity_chart(self, completed_tasks) -> discord.File:
        today = task_db.get_ist_now().date()
        dates = [today - datetime.timedelta(days=i) for i in range(6, -1, -1)]
        date_strs = [d.strftime("%Y-%m-%d") for d in dates]
        date_labels = [d.strftime("%a\n%d %b") for d in dates]
        
        counts = {d: 0 for d in date_strs}
        for t in completed_tasks:
            comp_at = t.get("completed_at")
            if comp_at:
                c_date = comp_at.split("T")[0]
                if c_date in counts:
                    counts[c_date] += 1
                    
        y_values = [counts[d] for d in date_strs]
        
        fig = Figure(figsize=(6, 4), facecolor='#1E1F22')
        ax = fig.subplots()
        ax.set_facecolor('#1E1F22')
        
        colors = ['#5865F2' for _ in range(6)] + ['#EB459E']
        
        ax.bar(date_labels, y_values, color=colors, width=0.6)
        ax.set_title("Weekly Task Productivity Trend", fontsize=12, fontweight='bold', color='#FFFFFF', pad=15)
        ax.set_ylabel("Tasks Completed", fontsize=10, fontweight='bold', color='#B5BAC1')
        ax.grid(axis='y', linestyle='--', alpha=0.3)
        
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('#4E5058')
        ax.spines['bottom'].set_color('#4E5058')
        ax.tick_params(colors='#B5BAC1')
        
        fig.tight_layout()
        
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=150)
        buf.seek(0)
        
        return discord.File(fp=buf, filename="productivity_graph.png")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is not None:
            return
            
        user_id_str = str(message.author.id)
        if user_id_str not in ["856485470171299891", "1403716456025165864"]:
            return
            
        # Reply with the interactive DM Home Panel
        profile = await asyncio.to_thread(task_db.get_user_profile, user_id_str)
        color = 0x5865F2 if user_id_str == "856485470171299891" else 0xEB459E
        
        embed = discord.Embed(
            title="🤖 Valence Task Bot DM Assistant",
            description=(
                f"Hello **{message.author.display_name}**! Welcome to your personal productivity dashboard.\n\n"
                f"🔥 **Streak:** {profile.get('streak', 0)} days\n"
                f"🛡️ **Level:** {profile.get('level', 1)} | **XP:** {profile.get('xp', 0)} / {profile.get('level', 1) * 1000} XP\n"
                f"✅ **Total Completed:** {profile.get('total_completed', 0)} tasks\n\n"
                "Use the buttons below to manage your tasks directly in DMs!"
            ),
            color=color
        )
        view = DMHomeView(message.author, self)
        await message.channel.send(embed=embed, view=view)


# --- Cog loader ---
async def setup(bot: commands.Bot):
    cog = TasksCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.task_group)
    logger.info("[TASKS] Loaded Tasks Cog Extension with Slash commands group.")
