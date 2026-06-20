import os
import io
import re
import json
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
        display_list = checklist[:25]  # Discord hard limit: 25 options max
        for i, item in enumerate(display_list):
            status_emoji = "✅" if item.get("done") else "⬜"
            label = f"{i+1}. {item.get('item', '')[:60]}"
            if not label.strip() or label == f"{i+1}. ":
                label = f"{i+1}. (unnamed subtask)"
            options.append(discord.SelectOption(
                label=label,
                value=str(i),
                emoji=status_emoji
            ))
        placeholder = "Select a subtask to toggle..."
        if len(checklist) > 25:
            placeholder = f"Select a subtask to toggle... (+{len(checklist)-25} more)"
        super().__init__(
            placeholder=placeholder,
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
        super().__init__(timeout=180)
        self.task = task
        self.task_id = task.get("task_id")
        self.caller_id = caller_id
        self.message = None
        self.update_buttons()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

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
        status = t.get("status")
        priority = t.get("priority", "Medium")
        
        # 1. Dynamic Color Selection
        if status == "completed":
            color = 0x20BDF4  # Completed Cyan (Neon)
        elif t.get("is_habit"):
            color = 0x1ABC9C  # Habit Turquoise
        else:
            color = {"High": 0xED4245, "Medium": 0xFEE75C, "Low": 0x57F287}.get(priority, 0x5865F2)
            
        status_lbl = "✨ **Completed**" if status == "completed" else "⏳ **Active**"
        priority_lbl = {"High": "🔴 **High**", "Medium": "🟡 **Medium**", "Low": "🟢 **Low**"}.get(priority, "🟡 **Medium**")
        visibility_lbl = "🔒 **Private**" if t.get("is_private") else "🔓 **Public**"
        category = f"📁 `{t.get('category', 'General').upper()}`"
        
        # Truncate description to prevent embed overflow (Discord 1024-char field limit)
        desc = t.get("description") or "*No description provided.*"
        if len(desc) > 1000:
            desc = desc[:997] + "..."
        
        embed = discord.Embed(
            title=f"📋 Task details: {t.get('title', 'Untitled')[:100]}",
            description=desc,
            color=color
        )
        
        # Grid details (Inline fields)
        embed.add_field(name="📌 Status", value=status_lbl, inline=True)
        embed.add_field(name="⚡ Priority", value=priority_lbl, inline=True)
        embed.add_field(name="📂 Category", value=category, inline=True)
        
        embed.add_field(name="🔑 ID", value=f"`{t.get('task_id')[:8]}`", inline=True)
        embed.add_field(name="🔒 Visibility", value=visibility_lbl, inline=True)
        
        if t.get("due_date"):
            embed.add_field(name="📅 Due Date (IST)", value=f"`{t.get('due_date')}`", inline=True)
        elif t.get("is_habit"):
            embed.add_field(name="🔁 Recurrence", value=f"`{t.get('recurrence', 'daily').upper()}`", inline=True)
        else:
            embed.add_field(name="📅 Due Date", value="`No deadline`", inline=True)
    
        # Pomodoro Session Indicator
        pomodoro_estimate = t.get("pomodoros_estimated", 1)
        pomodoro_completed = t.get("pomodoros_completed", 0)
        poms_icons = "🍅" * pomodoro_completed + "⚫" * max(0, pomodoro_estimate - pomodoro_completed)
        embed.add_field(name="🍅 Pomodoro Sessions", value=f"{poms_icons} *({pomodoro_completed}/{pomodoro_estimate} poms)*", inline=False)
    
        # Subtask Checklist with progress bar (truncated to prevent embed overflow)
        checklist = t.get("checklist") or []
        if checklist:
            done_count = sum(1 for x in checklist if x.get("done") or x.get("completed"))
            total_count = len(checklist)
            
            # Draw Progress Bar
            bar_length = 10
            filled = int((done_count / total_count) * bar_length)
            bar = "▰" * filled + "▱" * (bar_length - filled)
            pct = int((done_count / total_count) * 100)
            
            lines = []
            display_items = checklist[:15]  # Show max 15 to prevent overflow
            for i, x in enumerate(display_items, start=1):
                is_done = x.get("done") or x.get("completed")
                item_text = (x.get('item') or '(unnamed)')[:60]
                if is_done:
                    lines.append(f"☑️ ~~`{i}.` {item_text}~~")
                else:
                    lines.append(f"⬜ `{i}.` {item_text}")
            
            if len(checklist) > 15:
                lines.append(f"*... and {len(checklist) - 15} more items*")
                    
            checklist_val = f"`{bar}` **{pct}%** ({done_count}/{total_count})\n\n" + "\n".join(lines)
            # Final safety truncation for Discord 1024-char field limit
            if len(checklist_val) > 1020:
                checklist_val = checklist_val[:1017] + "..."
            embed.add_field(name="📝 Checklist Progress", value=checklist_val, inline=False)

        # Notes section
        notes_raw = t.get("notes", "[]")
        try:
            notes = json.loads(notes_raw) if isinstance(notes_raw, str) else (notes_raw or [])
        except (json.JSONDecodeError, TypeError):
            notes = []
        if notes:
            recent_notes = notes[-3:]  # Show last 3 notes
            n_lines = [f"• {n[:80]}" for n in recent_notes]
            notes_val = "\n".join(n_lines)
            if len(notes) > 3:
                notes_val += f"\n*+{len(notes)-3} older notes*"
            embed.add_field(name=f"📝 Notes ({len(notes)})", value=notes_val, inline=False)

        # Shared with
        shared = t.get("shared_with") or []
        if shared:
            embed.add_field(name="👥 Shared With", value=f"{len(shared)} user(s)", inline=True)

        # Timestamps footer
        created = (t.get("created_at") or "")[:16]
        completed = (t.get("completed_at") or "")[:16]
        footer_parts = []
        if created:
            footer_parts.append(f"Created: {created}")
        if completed:
            footer_parts.append(f"Completed: {completed}")
        if footer_parts:
            embed.set_footer(text=" • ".join(footer_parts))
            
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
        await interaction.response.defer()
        success, task, stats = await asyncio.to_thread(task_db.complete_task, str(interaction.user.id), self.task_id)
        if success:
            self.task = task
            self.update_buttons()
            
            xp_msg = f"🛡️ **+{stats['xp_gained']} XP** | 🔥 **Streak: {stats['streak']} days**"
            if stats.get("level_ups", 0) > 0:
                xp_msg += f"\n🎉 **LEVEL UP!** You reached **Level {stats['new_level']}**!"
                
            await interaction.edit_original_response(embed=self.get_embed(), view=self)
            await interaction.followup.send(f"🎉 Task completed successfully!\n{xp_msg}", ephemeral=self.task.get("is_private"))
        else:
            await interaction.followup.send("❌ Failed to complete task. (Is it already completed?)", ephemeral=True)

    @discord.ui.button(label="Start Focus (25m)", style=discord.ButtonStyle.blurple, row=1)
    async def btn_focus(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Tasks")
        if cog:
            await cog.start_focus_timer(interaction, self.task)
        else:
            await interaction.response.send_message("❌ Task cog is not loaded.", ephemeral=True)

    @discord.ui.button(label="Add Subtask", style=discord.ButtonStyle.gray, row=2)
    async def btn_add_subtask(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddSubtaskModal(self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.red, row=2)
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
                
            async def interaction_check(self, sub_interaction: discord.Interaction) -> bool:
                if sub_interaction.user.id != self.parent_view.caller_id:
                    await sub_interaction.response.send_message("❌ You cannot confirm deletion for this task.", ephemeral=True)
                    return False
                return True
                
            @discord.ui.button(label="Yes, Delete", style=discord.ButtonStyle.danger)
            async def yes_btn(self, sub_interaction: discord.Interaction, btn: discord.ui.Button):
                await sub_interaction.response.defer()
                success = await asyncio.to_thread(task_db.delete_task, self.parent_view.task_id)
                if success:
                    await sub_interaction.edit_original_response(content="🗑️ Task deleted successfully.", embed=None, view=None)
                else:
                    await sub_interaction.followup.send("❌ Failed to delete task.", ephemeral=True)
                    
            @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.secondary)
            async def no_btn(self, sub_interaction: discord.Interaction, btn: discord.ui.Button):
                await sub_interaction.response.edit_message(content="❌ Deletion cancelled.", embed=None, view=None)
                
        view = DeleteConfirmView(self)
        await interaction.response.send_message(embed=confirm_embed, view=view, ephemeral=self.task.get("is_private"))


class TaskPaginationView(discord.ui.View):
    """Pagination view listing multiple tasks."""
    def __init__(self, tasks_list, target_user, caller_id, per_page=5):
        super().__init__(timeout=120.0)
        self.tasks = tasks_list
        self.target_user = target_user
        self.caller_id = caller_id
        self.per_page = per_page
        self.current_page = 1
        self.total_pages = max(1, (len(tasks_list) + per_page - 1) // per_page)
        self.message = None
        self.update_buttons()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

    def update_buttons(self):
        self.prev_button.disabled = (self.current_page <= 1)
        self.next_button.disabled = (self.current_page >= self.total_pages)

    def get_embed(self) -> discord.Embed:
        # Configurable color logic
        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        
        color = 0x5865F2 if self.target_user.id in report_users else 0xEB459E
        
        embed = discord.Embed(
            title=f"📋 Task Board for {self.target_user.display_name}",
            color=color
        )
        
        if not self.tasks:
            embed.description = "*✨ You're completely caught up! No tasks left.*"
            return embed
            
        start_idx = (self.current_page - 1) * self.per_page
        end_idx = start_idx + self.per_page
        page_tasks = self.tasks[start_idx:end_idx]
        
        lines = []
        for i, t in enumerate(page_tasks, start=start_idx + 1):
            status_emoji = "✅" if t.get("status") == "completed" else "⏳"
            prio_emoji = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
            
            due_lbl = f" 📅 `{t.get('due_date')}`" if t.get("due_date") else " 📅 `No due date`"
            category_lbl = f" 📁 `{t.get('category', 'General')}`"
            habit_lbl = " 🔁 `Habit`" if t.get("is_habit") else ""
            private_lbl = " 🔒 `Private`" if t.get("is_private") else ""
            
            # Format task block
            task_block = (
                f"**{i}. {t.get('title')}**\n"
                f"> `{t.get('task_id')[:8]}` | {status_emoji} Status | {prio_emoji} Priority | {category_lbl}{due_lbl}{habit_lbl}{private_lbl}\n"
            )
            lines.append(task_block)
            
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Page {self.current_page}/{self.total_pages} | 📊 Total: {len(self.tasks)} Tasks")
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
        super().__init__(timeout=3600)  # 1 hour timeout (was 24h — memory leak risk)
        self.task_id = task_id
        self.user_id = str(user_id)
        self.message = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

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
        self.message = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass
        
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
        file = await asyncio.to_thread(self.cog.generate_productivity_chart, completed)
        
        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        color = 0x5865F2 if int(user_id_str) in report_users else 0xEB459E
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


# --- Subclass Groups for nested commands ---

class ChecklistGroup(app_commands.Group):
    def __init__(self, cog):
        super().__init__(name="checklist", description="Subtask checklist management")
        self.cog = cog

    async def checklist_task_id_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self.cog.task_id_autocomplete(interaction, current)

    @app_commands.command(name="show", description="Show a task's subtask checklist interactively")
    @app_commands.autocomplete(task_id=checklist_task_id_autocomplete)
    async def checklist_show(self, interaction: discord.Interaction, task_id: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        user_id_str = str(interaction.user.id)
        is_owner = task.get("user_id") == user_id_str
        is_shared = user_id_str in (task.get("shared_with") or [])
        
        if task.get("is_private") and not is_owner:
            await interaction.response.send_message("❌ This task is private and cannot be viewed.", ephemeral=True)
            return
            
        if not is_owner and not is_shared:
            await interaction.response.send_message("❌ You do not have permission to view this task.", ephemeral=True)
            return
            
        view = TaskDetailView(task, interaction.user.id)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=task.get("is_private"))

    @app_commands.command(name="add", description="Add an item to a task's subtask checklist")
    @app_commands.autocomplete(task_id=checklist_task_id_autocomplete)
    async def checklist_add(self, interaction: discord.Interaction, task_id: str, item: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return
            
        checklist = task.get("checklist") or []
        checklist.append({"item": item, "done": False})
        
        success = await asyncio.to_thread(task_db.update_task, task_id, {"checklist": checklist})
        if success:
            embed = discord.Embed(
                title="➕ Subtask Added",
                description=f"Added `\"{item}\"` to **{task.get('title')}** checklist.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))
        else:
            await interaction.response.send_message("❌ Failed to update task checklist.", ephemeral=True)

    @app_commands.command(name="toggle", description="Toggle a subtask checklist item status by number")
    @app_commands.autocomplete(task_id=checklist_task_id_autocomplete)
    async def checklist_toggle(self, interaction: discord.Interaction, task_id: str, number: int):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        user_id_str = str(interaction.user.id)
        is_owner = task.get("user_id") == user_id_str
        is_shared = user_id_str in (task.get("shared_with") or [])
        if not is_owner and not is_shared:
            await interaction.response.send_message("❌ You do not have permission to modify this task.", ephemeral=True)
            return
            
        checklist = task.get("checklist") or []
        idx = number - 1
        if idx < 0 or idx >= len(checklist):
            await interaction.response.send_message(f"❌ Invalid subtask number. Valid range is 1 to {len(checklist)}.", ephemeral=True)
            return
            
        checklist[idx]["done"] = not checklist[idx]["done"]
        success = await asyncio.to_thread(task_db.update_task, task_id, {"checklist": checklist})
        if success:
            status = "Completed" if checklist[idx]["done"] else "Pending"
            embed = discord.Embed(
                title="🔄 Subtask Toggled",
                description=f"Set subtask `{number}. \"{checklist[idx]['item']}\"` of **{task.get('title')}** to **{status}**.",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))
        else:
            await interaction.response.send_message("❌ Failed to update task checklist.", ephemeral=True)

    @app_commands.command(name="remove", description="Remove a subtask checklist item by number")
    @app_commands.autocomplete(task_id=checklist_task_id_autocomplete)
    async def checklist_remove(self, interaction: discord.Interaction, task_id: str, number: int):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return
            
        checklist = task.get("checklist") or []
        idx = number - 1
        if idx < 0 or idx >= len(checklist):
            await interaction.response.send_message(f"❌ Invalid subtask number. Valid range is 1 to {len(checklist)}.", ephemeral=True)
            return
            
        removed_item = checklist.pop(idx)
        success = await asyncio.to_thread(task_db.update_task, task_id, {"checklist": checklist})
        if success:
            embed = discord.Embed(
                title="🗑️ Subtask Removed",
                description=f"Removed subtask `{number}. \"{removed_item['item']}\"` from **{task.get('title')}**.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))
        else:
            await interaction.response.send_message("❌ Failed to update task checklist.", ephemeral=True)


class FocusGroup(app_commands.Group):
    def __init__(self, cog):
        super().__init__(name="focus", description="Focus timer control commands")
        self.cog = cog

    async def focus_task_id_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self.cog.task_id_autocomplete(interaction, current)

    @app_commands.command(name="start", description="Start a focus session (Pomodoro) tied to a task")
    @app_commands.autocomplete(task_id=focus_task_id_autocomplete)
    async def focus_start(self, interaction: discord.Interaction, task_id: str, duration: int = 25):
        if duration <= 0:
            await interaction.response.send_message("❌ Duration must be a positive integer.", ephemeral=True)
            return
        if duration > 480:
            await interaction.response.send_message("❌ Maximum focus duration is 480 minutes (8 hours).", ephemeral=True)
            return

        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        user_id_str = str(interaction.user.id)
        is_owner = task.get("user_id") == user_id_str
        is_shared = user_id_str in (task.get("shared_with") or [])
        
        if task.get("is_private") and not is_owner:
            await interaction.response.send_message("❌ This task is private.", ephemeral=True)
            return
            
        if not is_owner and not is_shared:
            await interaction.response.send_message("❌ You do not have permission to focus on this task.", ephemeral=True)
            return
            
        await self.cog.start_focus_timer(interaction, task, duration)

    @app_commands.command(name="cancel", description="Cancel your active focus session (partial XP if >5min)")
    async def focus_cancel(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        if user_id not in self.cog.active_focus_sessions:
            await interaction.response.send_message("❌ You do not have an active focus session.", ephemeral=True)
            return
        
        session = self.cog.active_focus_sessions.pop(user_id)
        loop_task = session["task"]
        start_time = session["start_time"]
        duration = session["duration"]
        task_title = session.get("title", "Unknown")
        loop_task.cancel()

        # Calculate partial XP based on elapsed time
        elapsed_mins = (task_db.get_ist_now() - start_time).total_seconds() / 60
        if elapsed_mins >= 5:
            # Award partial XP: proportional to time spent (max 50 XP for full session)
            partial_xp = int(min(50, (elapsed_mins / duration) * 50))
            new_xp, new_level, leveled_up = await asyncio.to_thread(task_db.add_xp, str(user_id), partial_xp)
            embed = discord.Embed(
                title="⏹️ Focus Session Ended Early",
                description=f"Focused on **{task_title}** for **{int(elapsed_mins)}** of {duration} minutes.",
                color=discord.Color.orange()
            )
            embed.add_field(name="Partial XP", value=f"✨ +{partial_xp} XP (proportional)")
            if leveled_up:
                embed.add_field(name="🚀 LEVEL UP!", value=f"You reached **Level {new_level}**!")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(
                f"🛑 Focus session cancelled ({int(elapsed_mins)}min < 5min minimum). No XP awarded.", 
                ephemeral=True
            )


# --- Tasks Cog Class ---

class TasksCog(commands.Cog, name="Tasks"):

    board_group = app_commands.Group(name="board", description="Board, sprint, and matrix views")
    stats_group = app_commands.Group(name="stats", description="Task statistics and insights")
    share_group = app_commands.Group(name="share", description="Share tasks with other users")
    manage_group = app_commands.Group(name="manage", description="Task management utilities")
    """Cog managing all Task commands, checklists, focus, habits, and weekly graphs."""
    __cog_app_commands_guilds__ = [int(os.getenv("GUILD_ID", "1514186381348306964"))]

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_focus_sessions = {}  # user_id -> TaskFocusTimer object
        self.last_habit_reset_date = None
        self.habit_reset_loop.start()
        self.reminder_task.start()
        self.planner_alerts_task.start()

        # Reload-safe dynamic nesting
        existing_names = [cmd.name for cmd in self.task_group.commands]
        if "checklist" not in existing_names:
            self.task_group.add_command(ChecklistGroup(self))
        if "focus" not in existing_names:
            self.task_group.add_command(FocusGroup(self))
        if "board" not in existing_names:
            self.task_group.add_command(self.board_group)
        if "stats" not in existing_names:
            self.task_group.add_command(self.stats_group)
        if "share" not in existing_names:
            self.task_group.add_command(self.share_group)
        if "manage" not in existing_names:
            self.task_group.add_command(self.manage_group)

    def cog_unload(self):
        self.habit_reset_loop.cancel()
        self.reminder_task.cancel()
        self.planner_alerts_task.cancel()
        
        # Cancel all active focus timer tasks
        for session in list(self.active_focus_sessions.values()):
            try:
                if isinstance(session, dict):
                    session["task"].cancel()
                else:
                    session.cancel()
            except Exception as e:
                logger.error(f"[TASKS] Error cancelling focus session task: {e}")

    # --- Parent Task Command Group ---
    task_group = app_commands.Group(name="task", description="Valence Task Bot Commands")

    # Autocomplete handler
    async def task_id_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        user_id_str = str(interaction.user.id)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        
        current = current.lower()
        # Sort: pending first (by priority High>Med>Low), then completed
        prio_order = {"High": 0, "Medium": 1, "Low": 2}
        tasks_list.sort(key=lambda t: (
            0 if t.get("status") == "pending" else 1,
            prio_order.get(t.get("priority"), 1)
        ))
        
        choices = []
        for t in tasks_list:
            title = t.get("title", "")
            task_id = t.get("task_id", "")
            
            if not current or current in title.lower() or current in task_id.lower():
                status_char = "✅" if t.get("status") == "completed" else "⏳"
                prio_char = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
                label = f"{status_char}{prio_char} {title}"
                if len(label) > 100:
                    label = label[:97] + "..."
                choices.append(app_commands.Choice(name=label, value=task_id))
                
        return choices[:25]

    # --- Background Loops ---

    @tasks.loop(minutes=5)
    async def habit_reset_loop(self):
        """Dynamic completed-habit reset checks in IST."""
        now = task_db.get_ist_now()
        today_str = now.strftime("%Y-%m-%d")
        
        # Persistent habit reset check
        try:
            last_reset = await asyncio.to_thread(task_db.get_last_habit_reset_date)
        except Exception as e:
            logger.error(f"[HABITS] Error reading last reset date: {e}")
            return
            
        if last_reset == today_str:
            self.last_habit_reset_date = today_str
            return
            
        logger.info("[HABITS] Checking for habits to reset...")
        try:
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
                    
            await asyncio.to_thread(task_db.set_last_habit_reset_date, today_str)
            self.last_habit_reset_date = today_str
            logger.info(f"[HABITS] Successfully completed habit resets for {today_str}")
        except Exception as e:
            logger.error(f"[HABITS] Error during habit resets: {e}")

    @habit_reset_loop.before_loop
    async def before_habit_reset_loop(self):
        await self.bot.wait_until_ready()

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

    @reminder_task.before_loop
    async def before_reminder_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def planner_alerts_task(self):
        """Hourly background task managing morning agendas, streak nudges, and weekly digests in IST."""
        now = task_db.get_ist_now()
        logger.info(f"[PLANNER] Running hourly planner checks (IST Hour: {now.hour}, Day: {now.weekday()})...")
        
        # Dynamically fetch all user IDs from the database (no more hardcoded list)
        try:
            all_users = await asyncio.to_thread(task_db.get_leaderboard)
            user_ids = [u["user_id"] for u in all_users] if all_users else []
        except Exception as e:
            logger.error(f"[PLANNER] Error fetching user list: {e}")
            user_ids = []
        
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
                    
                    report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
                    report_users = [u.strip() for u in report_users_str.split(",") if u.strip()]
                    color = 0x5865F2 if uid_str in report_users else 0xEB459E
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
                    
                    file = await asyncio.to_thread(self.generate_productivity_chart, completed)
                    
                    report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
                    report_users = [u.strip() for u in report_users_str.split(",") if u.strip()]
                    color = 0x5865F2 if uid_str in report_users else 0xEB459E
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

    @planner_alerts_task.before_loop
    async def before_planner_alerts_task(self):
        await self.bot.wait_until_ready()

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
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    async def task_view(self, interaction: discord.Interaction, task_id: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return

        user_id_str = str(interaction.user.id)
        is_owner = task.get("user_id") == user_id_str
        is_shared = user_id_str in (task.get("shared_with") or [])
        
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
    @app_commands.autocomplete(task_id=task_id_autocomplete)
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
            
        await interaction.response.send_message(embed=embed, ephemeral=updates.get("is_private", task.get("is_private")))

    # 4. DELETE TASK
    @task_group.command(name="delete", description="Delete a task")
    @app_commands.describe(task_id="The UUID of the task")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
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
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    async def task_complete(self, interaction: discord.Interaction, task_id: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
            
        user_id_str = str(interaction.user.id)
        if task.get("user_id") != user_id_str and user_id_str not in (task.get("shared_with") or []):
            await interaction.response.send_message("❌ You do not have permission to complete this task.", ephemeral=True)
            return
            
        success, completed_task, stats = await asyncio.to_thread(task_db.complete_task, user_id_str, task_id)
        if not success:
            await interaction.response.send_message("❌ Failed to complete task. Is it already completed?", ephemeral=True)
            return

        # --- Auto-Award Badges ---
        badge_msgs = []
        try:
            profile = await asyncio.to_thread(task_db.get_user_profile, user_id_str)
            tc = profile.get("total_completed", 0)
            streak = profile.get("streak", 0)
            level = profile.get("level", 1)

            badge_map = [
                (tc >= 1, "🌱 First Task"),
                (tc >= 10, "⚡ 10 Tasks"),
                (tc >= 25, "🎯 25 Tasks"),
                (tc >= 50, "💎 50 Tasks"),
                (tc >= 100, "💯 Century Club"),
                (streak >= 3, "🔥 3-Day Streak"),
                (streak >= 7, "🔥 7-Day Streak"),
                (streak >= 14, "🔥 14-Day Streak"),
                (streak >= 30, "🏆 30-Day Streak"),
                (level >= 5, "🛡️ Level 5"),
                (level >= 10, "⚔️ Level 10"),
                (level >= 20, "👑 Level 20"),
            ]
            for condition, badge in badge_map:
                if condition:
                    awarded = await asyncio.to_thread(task_db.add_badge, user_id_str, badge)
                    if awarded:
                        badge_msgs.append(badge)
        except Exception as e:
            logger.error(f"[BADGES] Error awarding badges: {e}")

        embed = discord.Embed(
            title="🎉 Task Completed!",
            description=f"**{completed_task['title']}**",
            color=discord.Color.gold()
        )
        embed.add_field(name="XP Gained", value=f"✨ +{stats['xp_gained']} XP")
        embed.add_field(name="Daily Streak", value=f"🔥 {stats['streak']} days")
        embed.add_field(name="New Level", value=f"📈 Level {stats['new_level']}")

        if badge_msgs:
            embed.add_field(name="🏅 New Badges Unlocked!", value="\n".join(badge_msgs), inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=completed_task.get("is_private"))

    # 6. LIST TASKS
    @task_group.command(name="list", description="List your tasks with pagination")
    @app_commands.describe(
        status="Filter tasks by status",
        category="Filter tasks by category",
        priority="Filter tasks by priority",
        show_private="Show your private tasks (Forces ephemeral view)",
        sort_by="Sort tasks by this field"
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
        ],
        sort_by=[
            app_commands.Choice(name="Priority (High→Low)", value="priority"),
            app_commands.Choice(name="Due Date (Earliest)", value="due_date"),
            app_commands.Choice(name="Created (Newest)", value="created_at")
        ]
    )
    async def task_list(self, interaction: discord.Interaction, status: str = None, 
                        category: str = None, priority: str = None, show_private: bool = False,
                        sort_by: str = None):
        
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

        # Apply sorting
        if sort_by == "priority":
            prio_map = {"High": 3, "Medium": 2, "Low": 1}
            filtered.sort(key=lambda x: prio_map.get(x.get("priority", "Medium"), 2), reverse=True)
        elif sort_by == "due_date":
            filtered.sort(key=lambda x: x.get("due_date") or "9999-99-99")
        elif sort_by == "created_at":
            filtered.sort(key=lambda x: x.get("created_at") or "", reverse=True)
                
        if not filtered:
            await interaction.response.send_message("📝 No tasks found matching your filters.", ephemeral=show_private)
            return
            
        view = TaskPaginationView(filtered, interaction.user, interaction.user.id)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=show_private)

    # 7. SHARE TASK
    @share_group.command(name="add", description="Share a public task with another server user")
    @app_commands.describe(task_id="The UUID of the task", user="The user to collaborate with")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
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

    # Old Focus commands removed (now nested under /task focus start/cancel)

    # 9. DASHBOARD
    @stats_group.command(name="dashboard", description="View your productivity level, streaks, and agenda")
    async def task_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        profile = await asyncio.to_thread(task_db.get_user_profile, user_id_str)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        
        pending = [t for t in tasks_list if t.get("status") == "pending" and t.get("user_id") == user_id_str]
        
        xp = profile.get("xp", 0)
        level = profile.get("level", 1)
        xp_needed = level * 1000
        
        bar_len = 10
        filled = int((xp / xp_needed) * bar_len)
        bar = "▰" * filled + "▱" * (bar_len - filled)
        
        # Configurable color logic
        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        
        color = 0x5865F2 if int(user_id_str) in report_users else 0xEB459E
        
        embed = discord.Embed(
            title=f"🛡️ Character Profile — {interaction.user.display_name}",
            color=color
        )
        embed.add_field(name="👑 Rank / Level", value=f"**Level {level}**\n`{bar}` *{xp}/{xp_needed} XP*", inline=True)
        embed.add_field(name="🔥 Daily Streak", value=f"**{profile.get('streak', 0)} Days**\n*Keep it up!*", inline=True)
        embed.add_field(name="🏆 Completed Tasks", value=f"**{profile.get('total_completed', 0)} Tasks**\n*Total Earned*", inline=True)
        
        if pending:
            lines = []
            sorted_pending = sorted(pending, key=lambda x: {"High": 3, "Medium": 2, "Low": 1}.get(x.get("priority", "Medium"), 2), reverse=True)
            for t in sorted_pending[:4]:
                due = f" *(Due: {t['due_date']})*" if t.get("due_date") else ""
                lines.append(f"- `[{t['priority']}]` **{t['title']}**{due}")
            embed.add_field(name="📋 Next Up on Your Agenda", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📋 Next Up on Your Agenda", value="🎉 All caught up! No tasks pending.", inline=False)
            
        await interaction.followup.send(embed=embed)

    # 10. WEEKLY TASK BOARD (task-focused, NOT XP-based like YPT)
    @board_group.command(name="show", description="Weekly task completion board — who shipped the most this week")
    async def task_board(self, interaction: discord.Interaction):
        await interaction.response.defer()
        leaderboard = await asyncio.to_thread(task_db.get_leaderboard)
        if not leaderboard:
            await interaction.response.send_message("📭 No users found.", ephemeral=True)
            return

        now = task_db.get_ist_now()
        week_start = now - datetime.timedelta(days=now.weekday())
        week_start_str = week_start.strftime("%Y-%m-%d")

        # Build weekly completion counts per user
        user_weekly = []
        for entry in leaderboard:
            uid = entry["user_id"]
            all_tasks = await asyncio.to_thread(task_db.get_user_tasks, uid)
            weekly_done = len([
                t for t in all_tasks 
                if t.get("status") == "completed" and (t.get("completed_at") or "")[:10] >= week_start_str
            ])
            pending = len([t for t in all_tasks if t.get("status") == "pending"])
            user_weekly.append({"user_id": uid, "weekly": weekly_done, "pending": pending, "streak": entry.get("streak", 0)})

        user_weekly.sort(key=lambda x: x["weekly"], reverse=True)

        embed = discord.Embed(
            title="📊 Weekly Task Board",
            description=f"Who completed the most tasks this week (since {week_start_str})?",
            color=discord.Color.teal()
        )

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for idx, entry in enumerate(user_weekly[:10]):
            medal = medals[idx] if idx < 3 else f"`#{idx+1}`"
            user = self.bot.get_user(int(entry["user_id"]))
            username = user.display_name if user else f"User {entry['user_id']}"
            bar_len = min(entry["weekly"], 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)
            lines.append(
                f"{medal} **{username}** `{bar}` **{entry['weekly']}** done | {entry['pending']} pending | 🔥{entry['streak']}d"
            )

        embed.description += "\n\n" + "\n".join(lines)
        embed.set_footer(text="Ranked by tasks COMPLETED this week, not XP")
        await interaction.followup.send(embed=embed)

    # 11. WEEKLY PRODUCTIVITY GRAPH
    @stats_group.command(name="graph", description="Show a visual trend chart of your weekly task completions")
    async def task_graph(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        user_id_str = str(interaction.user.id)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        completed = [t for t in tasks_list if t.get("status") == "completed"]
        
        file = await asyncio.to_thread(self.generate_productivity_chart, completed)
        
        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        color = 0x5865F2 if int(user_id_str) in report_users else 0xEB459E
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
    @manage_group.command(name="remind", description="Schedule a custom DM reminder for a task")
    @app_commands.describe(
        task_id="The UUID of the task",
        time="Time to remind (e.g. '10m', '2h', 'tomorrow', '15:30', or 'YYYY-MM-DD HH:MM')"
    )
    @app_commands.autocomplete(task_id=task_id_autocomplete)
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

    # 14. REOPEN TASK
    @task_group.command(name="reopen", description="Reopen a completed task back to pending")
    @app_commands.describe(task_id="The UUID of the completed task")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    async def task_reopen(self, interaction: discord.Interaction, task_id: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return
        if task.get("status") != "completed":
            await interaction.response.send_message("⚠️ This task is already pending.", ephemeral=True)
            return

        success = await asyncio.to_thread(task_db.reopen_task, task_id)
        if success:
            embed = discord.Embed(
                title="🔄 Task Reopened",
                description=f"**{task.get('title')}** has been set back to pending.",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))
        else:
            await interaction.response.send_message("❌ Failed to reopen task.", ephemeral=True)

    # 15. TODAY VIEW
    @task_group.command(name="today", description="See today's tasks, habits, and overdue items")
    async def task_today(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        today_tasks = await asyncio.to_thread(task_db.get_today_tasks, user_id_str)

        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        color = 0x5865F2 if interaction.user.id in report_users else 0xEB459E

        embed = discord.Embed(
            title=f"📅 Today's Focus — {interaction.user.display_name}",
            color=color
        )

        if not today_tasks:
            embed.description = "✨ You're all caught up! No tasks due today."
            await interaction.followup.send(embed=embed)
            return

        now = task_db.get_ist_now()
        overdue = []
        due_today = []
        habits = []

        for t in today_tasks:
            if t.get("is_habit"):
                habits.append(t)
            elif t.get("due_date"):
                parsed = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        parsed = datetime.datetime.strptime(t["due_date"].strip(), fmt)
                        break
                    except ValueError:
                        continue
                if parsed and parsed < now:
                    overdue.append(t)
                else:
                    due_today.append(t)
            else:
                due_today.append(t)

        if overdue:
            lines = [f"🔴 **{t['title']}** — Due: `{t.get('due_date')}`" for t in overdue[:5]]
            embed.add_field(name=f"⚠️ Overdue ({len(overdue)})", value="\n".join(lines), inline=False)

        if habits:
            lines = [f"🔁 **{t['title']}** ({t.get('recurrence', 'daily')})" for t in habits[:5]]
            embed.add_field(name=f"🔁 Today's Habits ({len(habits)})", value="\n".join(lines), inline=False)

        if due_today:
            lines = []
            for t in due_today[:5]:
                prio = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
                due = f" — Due: `{t.get('due_date')}`" if t.get("due_date") else ""
                lines.append(f"{prio} **{t['title']}**{due}")
            embed.add_field(name=f"📋 Due Today ({len(due_today)})", value="\n".join(lines), inline=False)

        if not overdue and not habits and not due_today:
            embed.description = "✨ Nothing specific for today!"

        embed.set_footer(text=f"Total: {len(today_tasks)} items • Use /task view to manage")
        await interaction.followup.send(embed=embed)

    # 17. SEARCH
    @task_group.command(name="search", description="Search your tasks by keyword")
    @app_commands.describe(query="Search keyword to match against task titles and descriptions")
    async def task_search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)
        user_id_str = str(interaction.user.id)
        results = await asyncio.to_thread(task_db.search_tasks, user_id_str, query)

        if not results:
            await interaction.followup.send(f"🔍 No tasks found matching `{query}`.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔍 Search Results for \"{query}\"",
            color=discord.Color.blurple()
        )
        lines = []
        for t in results[:15]:
            status = "✅" if t.get("status") == "completed" else "⏳"
            prio = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
            lines.append(f"{status}{prio} **{t['title'][:60]}** | `{t['task_id'][:8]}`")

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Found {len(results)} result(s)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # 18. UNSHARE
    @share_group.command(name="remove", description="Remove a user from a shared task")
    @app_commands.describe(task_id="The UUID of the task", user="The user to remove")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    async def task_unshare(self, interaction: discord.Interaction, task_id: str, user: discord.Member):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return

        shared_list = task.get("shared_with") or []
        uid_str = str(user.id)
        if uid_str not in shared_list:
            await interaction.response.send_message(f"⚠️ {user.display_name} is not on this task's share list.", ephemeral=True)
            return

        shared_list.remove(uid_str)
        await asyncio.to_thread(task_db.update_task, task_id, {"shared_with": shared_list})
        await interaction.response.send_message(f"🔓 Removed {user.mention} from **{task['title']}**.")

    # 19. DUPLICATE
    @task_group.command(name="duplicate", description="Clone a task with all its details")
    @app_commands.describe(task_id="The UUID of the task to clone")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    async def task_duplicate(self, interaction: discord.Interaction, task_id: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return

        # Reset checklist items to uncompleted
        checklist = task.get("checklist") or []
        reset_checklist = [{"item": c.get("item", ""), "done": False} for c in checklist]

        new_id = await asyncio.to_thread(
            task_db.add_task,
            user_id=str(interaction.user.id),
            title=f"{task.get('title', '')} (Copy)",
            description=task.get("description", ""),
            due_date=task.get("due_date"),
            priority=task.get("priority", "Medium"),
            category=task.get("category", "General"),
            is_private=task.get("is_private", False),
            recurrence=task.get("recurrence", "none"),
            is_habit=task.get("is_habit", False),
            pomodoros_estimated=task.get("pomodoros_estimated", 1)
        )
        # Copy checklist to the new task
        if reset_checklist:
            await asyncio.to_thread(task_db.update_task, new_id, {"checklist": reset_checklist})

        embed = discord.Embed(
            title="📋 Task Duplicated",
            description=f"Cloned **{task.get('title')}** → New ID: `{new_id[:8]}`",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))

    # 20. TASK NOTES
    @task_group.command(name="note", description="Add a timestamped progress note to a task")
    @app_commands.describe(task_id="The UUID of the task", text="Your note text")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    async def task_note(self, interaction: discord.Interaction, task_id: str, text: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return

        user_id_str = str(interaction.user.id)
        is_owner = task.get("user_id") == user_id_str
        is_shared = user_id_str in (task.get("shared_with") or [])
        if not is_owner and not is_shared:
            await interaction.response.send_message("❌ You don't have access to this task.", ephemeral=True)
            return

        success = await asyncio.to_thread(task_db.add_task_note, task_id, text)
        if success:
            embed = discord.Embed(
                title="📝 Note Added",
                description=f"Added note to **{task.get('title')}**:\n> {text[:200]}",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=task.get("is_private"))
        else:
            await interaction.response.send_message("❌ Failed to add note.", ephemeral=True)

    # 21. STATS
    @stats_group.command(name="show", description="View your detailed productivity statistics")
    async def task_stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        profile = await asyncio.to_thread(task_db.get_user_profile, user_id_str)
        all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)

        total = len(all_tasks)
        completed = [t for t in all_tasks if t.get("status") == "completed"]
        pending = [t for t in all_tasks if t.get("status") == "pending"]
        completion_rate = (len(completed) / total * 100) if total > 0 else 0

        # Category breakdown
        categories = {}
        for t in all_tasks:
            cat = t.get("category", "General")
            categories[cat] = categories.get(cat, 0) + 1

        cat_lines = [f"• `{cat}`: **{count}** tasks" for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True)[:6]]

        # Most productive day of week
        day_counts = {i: 0 for i in range(7)}
        for t in completed:
            comp = t.get("completed_at", "")
            if comp:
                try:
                    dt = datetime.datetime.fromisoformat(comp)
                    day_counts[dt.weekday()] += 1
                except (ValueError, TypeError):
                    pass
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        best_day_idx = max(day_counts, key=day_counts.get)
        best_day = day_names[best_day_idx] if day_counts[best_day_idx] > 0 else "N/A"

        # Badges
        try:
            badges = await asyncio.to_thread(task_db.get_user_badges, user_id_str)
        except Exception:
            badges = []

        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        color = 0x5865F2 if interaction.user.id in report_users else 0xEB459E

        embed = discord.Embed(
            title=f"📊 Productivity Stats — {interaction.user.display_name}",
            color=color
        )
        embed.add_field(name="📋 Total Tasks", value=f"**{total}**", inline=True)
        embed.add_field(name="✅ Completed", value=f"**{len(completed)}**", inline=True)
        embed.add_field(name="⏳ Pending", value=f"**{len(pending)}**", inline=True)
        embed.add_field(name="📈 Completion Rate", value=f"**{completion_rate:.1f}%**", inline=True)
        embed.add_field(name="🔥 Current Streak", value=f"**{profile.get('streak', 0)} days**", inline=True)
        embed.add_field(name="🏆 Best Streak", value=f"**{profile.get('best_streak', 0)} days**", inline=True)
        embed.add_field(name="⭐ Best Day", value=f"**{best_day}** ({day_counts[best_day_idx]} tasks)", inline=True)
        embed.add_field(name="🛡️ Level", value=f"**{profile.get('level', 1)}**", inline=True)
        embed.add_field(name="❄️ Streak Freezes", value=f"**{profile.get('streak_freezes', 0)}**", inline=True)

        if cat_lines:
            embed.add_field(name="📂 Categories", value="\n".join(cat_lines), inline=False)

        if badges:
            embed.add_field(name="🏅 Badges", value=" ".join(badges[:10]), inline=False)

        await interaction.followup.send(embed=embed)

    # 22. STREAK FREEZE
    @manage_group.command(name="freeze", description="Use a streak freeze to protect your streak for today")
    async def task_freeze(self, interaction: discord.Interaction):
        user_id_str = str(interaction.user.id)
        profile = await asyncio.to_thread(task_db.get_user_profile, user_id_str)
        freezes = profile.get("streak_freezes", 0)

        if freezes <= 0:
            embed = discord.Embed(
                title="❄️ No Streak Freezes Available",
                description="You don't have any streak freezes left.\nEarn more by reaching level milestones!",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        success = await asyncio.to_thread(task_db.use_streak_freeze, user_id_str)
        if success:
            embed = discord.Embed(
                title="❄️ Streak Freeze Activated!",
                description=f"Your **{profile.get('streak', 0)}-day streak** is protected for today.\n"
                           f"Remaining freezes: **{freezes - 1}**",
                color=discord.Color.blue()
            )
            embed.set_footer(text="Streak freezes replenish at level milestones (5, 10, 15...)")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("❌ Failed to use streak freeze.", ephemeral=True)

    # 23. WHO'S FOCUSING
    @board_group.command(name="whofocusing", description="See who's currently in a focus session")
    async def task_whofocusing(self, interaction: discord.Interaction):
        sessions = self.active_focus_sessions
        if not sessions:
            await interaction.response.send_message("😴 Nobody is focusing right now. Be the first!", ephemeral=True)
            return

        embed = discord.Embed(
            title="🍅 Live Focus Sessions",
            description="These users are currently locked in and studying:",
            color=discord.Color.orange()
        )

        now = task_db.get_ist_now()
        lines = []
        for uid, session in sessions.items():
            user = self.bot.get_user(uid)
            username = user.display_name if user else f"User {uid}"
            start = session.get("start_time", now)
            duration = session.get("duration", 25)
            elapsed = int((now - start).total_seconds() / 60)
            remaining = max(0, duration - elapsed)
            title = session.get("title", "Unknown task")

            # Progress bar
            pct = min(1.0, elapsed / duration)
            filled = int(pct * 8)
            bar = "▰" * filled + "▱" * (8 - filled)

            lines.append(
                f"**{username}** — `{bar}` {int(pct*100)}%\n"
                f"  📋 {title[:40]} • ⏱️ {remaining}min left"
            )

        embed.description = "\n\n".join(lines)
        embed.set_footer(text=f"{len(sessions)} active session(s)")
        await interaction.response.send_message(embed=embed)

    # 24. KANBAN BOARD VIEW
    @board_group.command(name="kanban", description="Visual Kanban board — see all tasks by status")
    async def task_kanban(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)

        pending = [t for t in all_tasks if t.get("status") == "pending" and not t.get("is_habit")]
        habits = [t for t in all_tasks if t.get("status") == "pending" and t.get("is_habit")]
        completed_recent = [
            t for t in all_tasks if t.get("status") == "completed"
        ]
        # Only show last 5 completed
        completed_recent.sort(key=lambda x: x.get("completed_at") or "", reverse=True)
        completed_recent = completed_recent[:5]

        # Split pending by priority
        high = [t for t in pending if t.get("priority") == "High"]
        medium = [t for t in pending if t.get("priority") == "Medium"]
        low = [t for t in pending if t.get("priority") == "Low"]

        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        color = 0x5865F2 if interaction.user.id in report_users else 0xEB459E

        embed = discord.Embed(
            title=f"📋 Kanban Board — {interaction.user.display_name}",
            color=color
        )

        # High priority column
        if high:
            lines = [f"🔴 {t['title'][:45]}" for t in high[:5]]
            if len(high) > 5:
                lines.append(f"*+{len(high)-5} more*")
            embed.add_field(name=f"🔴 HIGH ({len(high)})", value="\n".join(lines), inline=True)
        else:
            embed.add_field(name="🔴 HIGH (0)", value="*Empty*", inline=True)

        # Medium priority column
        if medium:
            lines = [f"🟡 {t['title'][:45]}" for t in medium[:5]]
            if len(medium) > 5:
                lines.append(f"*+{len(medium)-5} more*")
            embed.add_field(name=f"🟡 MEDIUM ({len(medium)})", value="\n".join(lines), inline=True)
        else:
            embed.add_field(name="🟡 MEDIUM (0)", value="*Empty*", inline=True)

        # Low priority column
        if low:
            lines = [f"🟢 {t['title'][:45]}" for t in low[:5]]
            if len(low) > 5:
                lines.append(f"*+{len(low)-5} more*")
            embed.add_field(name=f"🟢 LOW ({len(low)})", value="\n".join(lines), inline=True)
        else:
            embed.add_field(name="🟢 LOW (0)", value="*Empty*", inline=True)

        # Habits row
        if habits:
            h_lines = [f"🔁 {t['title'][:40]} ({t.get('recurrence', 'daily')})" for t in habits[:4]]
            embed.add_field(name=f"🔁 HABITS ({len(habits)})", value="\n".join(h_lines), inline=False)

        # Recently completed
        if completed_recent:
            c_lines = [f"✅ ~~{t['title'][:40]}~~ — {(t.get('completed_at') or '')[:10]}" for t in completed_recent]
            embed.add_field(name=f"✅ RECENTLY DONE ({len([t for t in all_tasks if t.get('status')=='completed'])} total)", value="\n".join(c_lines), inline=False)

        embed.set_footer(text=f"Total: {len(all_tasks)} tasks • {len(pending)} pending • {len(habits)} habits")
        await interaction.followup.send(embed=embed)

    # 25-A. SPRINT GOAL
    @board_group.command(name="sprint", description="Set or view your weekly task completion goal")
    @app_commands.describe(target="Set a weekly goal (e.g. 10 tasks). Omit to view current progress.")
    async def task_sprint(self, interaction: discord.Interaction, target: int = None):
        user_id_str = str(interaction.user.id)
        profile = await asyncio.to_thread(task_db.get_user_profile, user_id_str)

        if target is not None:
            if target < 1 or target > 100:
                await interaction.response.send_message("❌ Sprint goal must be between 1 and 100.", ephemeral=True)
                return
            # Store sprint goal in user profile (using badges field temporarily as JSON won't conflict)
            await asyncio.to_thread(task_db.update_user_profile, user_id_str, {"sprint_goal": target})
            embed = discord.Embed(
                title="🏃 Sprint Goal Set!",
                description=f"Your weekly target: **{target} tasks**\nGet it done by Sunday!",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed)
            return

        # View progress
        now = task_db.get_ist_now()
        week_start = now - datetime.timedelta(days=now.weekday())
        week_start_str = week_start.strftime("%Y-%m-%d")
        
        all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        weekly_done = len([
            t for t in all_tasks
            if t.get("status") == "completed" and (t.get("completed_at") or "")[:10] >= week_start_str
        ])

        goal = profile.get("sprint_goal", 7)  # Default 7 tasks/week
        pct = min(1.0, weekly_done / goal) if goal > 0 else 0
        filled = int(pct * 15)
        bar = "█" * filled + "░" * (15 - filled)
        days_left = 6 - now.weekday()

        if pct >= 1.0:
            status = "🎉 **SPRINT COMPLETE!** You crushed it!"
        elif pct >= 0.7:
            status = "🔥 Almost there! Keep pushing!"
        elif pct >= 0.3:
            status = f"⏳ {days_left} days left. You got this!"
        else:
            status = f"🚀 Time to grind! {days_left} days remaining."

        embed = discord.Embed(
            title=f"🏃 Weekly Sprint — {interaction.user.display_name}",
            color=discord.Color.green() if pct >= 1.0 else discord.Color.orange()
        )
        embed.add_field(
            name="Progress",
            value=f"`{bar}` **{weekly_done}/{goal}** ({int(pct*100)}%)",
            inline=False
        )
        embed.add_field(name="Status", value=status, inline=False)
        embed.set_footer(text=f"Week of {week_start_str} • Use /task sprint <number> to set goal")
        await interaction.response.send_message(embed=embed)

    # 25-B. EISENHOWER PRIORITY MATRIX
    @board_group.command(name="matrix", description="Eisenhower matrix — see tasks by urgency vs importance")
    async def task_matrix(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        pending = [t for t in all_tasks if t.get("status") == "pending" and not t.get("is_habit")]

        now = task_db.get_ist_now()
        urgent_threshold = now + datetime.timedelta(days=2)  # Due within 2 days = urgent

        # Classify each task
        q1_urgent_important = []  # High priority + due soon → DO FIRST
        q2_not_urgent_important = []  # High priority + not due soon → SCHEDULE
        q3_urgent_not_important = []  # Low/Med priority + due soon → DELEGATE
        q4_neither = []  # Low/Med priority + not due soon → BACKLOG

        for t in pending:
            is_high = t.get("priority") == "High"
            is_urgent = False
            if t.get("due_date"):
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        due = datetime.datetime.strptime(t["due_date"].strip(), fmt)
                        is_urgent = due <= urgent_threshold
                        break
                    except ValueError:
                        continue

            if is_high and is_urgent:
                q1_urgent_important.append(t)
            elif is_high and not is_urgent:
                q2_not_urgent_important.append(t)
            elif not is_high and is_urgent:
                q3_urgent_not_important.append(t)
            else:
                q4_neither.append(t)

        embed = discord.Embed(
            title="🧠 Eisenhower Priority Matrix",
            description="Tasks sorted by urgency (due ≤ 2 days) × importance (High priority)",
            color=0xFF6B6B
        )

        def fmt_quadrant(tasks, limit=4):
            if not tasks:
                return "*Empty — nice!*"
            lines = [f"• {t['title'][:40]}" + (f" ⏰`{t.get('due_date','')[:10]}`" if t.get('due_date') else "") for t in tasks[:limit]]
            if len(tasks) > limit:
                lines.append(f"*+{len(tasks)-limit} more*")
            return "\n".join(lines)

        embed.add_field(name=f"🔴 DO FIRST ({len(q1_urgent_important)})", value=fmt_quadrant(q1_urgent_important), inline=True)
        embed.add_field(name=f"🟡 SCHEDULE ({len(q2_not_urgent_important)})", value=fmt_quadrant(q2_not_urgent_important), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=False)  # Spacer row
        embed.add_field(name=f"🟠 QUICK WINS ({len(q3_urgent_not_important)})", value=fmt_quadrant(q3_urgent_not_important), inline=True)
        embed.add_field(name=f"⚪ BACKLOG ({len(q4_neither)})", value=fmt_quadrant(q4_neither), inline=True)

        embed.set_footer(text=f"{len(pending)} pending tasks analyzed")
        await interaction.followup.send(embed=embed)

    # 25. EXPORT
    @manage_group.command(name="export", description="Export your tasks as a CSV file")
    @app_commands.describe(status="Filter by status (or export all)")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="All Tasks", value="all"),
            app_commands.Choice(name="Pending Only", value="pending"),
            app_commands.Choice(name="Completed Only", value="completed")
        ]
    )
    async def task_export(self, interaction: discord.Interaction, status: str = "all"):
        await interaction.response.defer(ephemeral=True)
        user_id_str = str(interaction.user.id)
        tasks_list = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)

        if status != "all":
            tasks_list = [t for t in tasks_list if t.get("status") == status]

        if not tasks_list:
            await interaction.followup.send("📭 No tasks to export.", ephemeral=True)
            return

        # Build CSV
        csv_lines = ["Title,Status,Priority,Category,Due Date,Created,Completed"]
        for t in tasks_list:
            title = t.get("title", "").replace(",", ";")
            csv_lines.append(
                f"{title},{t.get('status','')},{t.get('priority','')},{t.get('category','')},{t.get('due_date','')},{t.get('created_at','')[:10]},{(t.get('completed_at') or '')[:10]}"
            )

        csv_content = "\n".join(csv_lines)
        buf = io.BytesIO(csv_content.encode("utf-8"))
        file = discord.File(fp=buf, filename=f"tasks_export_{task_db.get_ist_now().strftime('%Y%m%d')}.csv")

        embed = discord.Embed(
            title="📤 Tasks Exported",
            description=f"Exported **{len(tasks_list)}** tasks ({status}) as CSV.",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    # 26. BADGES VIEWER
    @stats_group.command(name="badges", description="View your earned achievement badges")
    async def task_badges(self, interaction: discord.Interaction):
        user_id_str = str(interaction.user.id)
        try:
            badges = await asyncio.to_thread(task_db.get_user_badges, user_id_str)
        except Exception:
            badges = []

        all_badges = [
            ("🌱 First Task", "Complete your first task"),
            ("⚡ 10 Tasks", "Complete 10 tasks"),
            ("🎯 25 Tasks", "Complete 25 tasks"),
            ("💎 50 Tasks", "Complete 50 tasks"),
            ("💯 Century Club", "Complete 100 tasks"),
            ("🔥 3-Day Streak", "Maintain a 3-day streak"),
            ("🔥 7-Day Streak", "Maintain a 7-day streak"),
            ("🔥 14-Day Streak", "Maintain a 14-day streak"),
            ("🏆 30-Day Streak", "Maintain a 30-day streak"),
            ("🛡️ Level 5", "Reach Level 5"),
            ("⚔️ Level 10", "Reach Level 10"),
            ("👑 Level 20", "Reach Level 20"),
        ]

        embed = discord.Embed(
            title=f"🏅 Badge Collection — {interaction.user.display_name}",
            color=discord.Color.gold()
        )

        lines = []
        earned_count = 0
        for badge_name, desc in all_badges:
            if badge_name in badges:
                lines.append(f"✅ **{badge_name}** — _{desc}_")
                earned_count += 1
            else:
                lines.append(f"🔒 ~~{badge_name}~~ — _{desc}_")

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Earned: {earned_count}/{len(all_badges)} badges")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # 27. QUICK PRIORITY CHANGE
    @task_group.command(name="priority", description="Quickly change a task's priority")
    @app_commands.describe(task_id="The UUID of the task", priority="New priority level")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    @app_commands.choices(priority=[
        app_commands.Choice(name="🔴 High", value="High"),
        app_commands.Choice(name="🟡 Medium", value="Medium"),
        app_commands.Choice(name="🟢 Low", value="Low")
    ])
    async def task_priority(self, interaction: discord.Interaction, task_id: str, priority: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return

        await asyncio.to_thread(task_db.update_task, task_id, {"priority": priority})
        prio_emoji = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(priority, "🟡")
        await interaction.response.send_message(
            f"{prio_emoji} **{task['title']}** priority changed to **{priority}**.",
            ephemeral=task.get("is_private")
        )

    # 28. BULK COMPLETE
    @task_group.command(name="bulkcomplete", description="Complete multiple tasks at once (comma-separated IDs)")
    @app_commands.describe(task_ids="Comma-separated task IDs to complete (e.g. 'abc123,def456')")
    async def task_bulk_complete(self, interaction: discord.Interaction, task_ids: str):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        ids = [tid.strip() for tid in task_ids.split(",") if tid.strip()]

        if not ids:
            await interaction.followup.send("❌ No task IDs provided.", ephemeral=True)
            return
        if len(ids) > 20:
            await interaction.followup.send("❌ Maximum 20 tasks per bulk operation.", ephemeral=True)
            return

        completed = []
        failed = []
        total_xp = 0

        for tid in ids:
            try:
                # Support partial IDs — find matching task
                all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
                matched = [t for t in all_tasks if t.get("task_id", "").startswith(tid)]
                if not matched:
                    failed.append(tid[:8])
                    continue
                full_id = matched[0]["task_id"]
                success, task, stats = await asyncio.to_thread(task_db.complete_task, user_id_str, full_id)
                if success:
                    completed.append(task.get("title", "?")[:30])
                    total_xp += stats.get("xp_gained", 0)
                else:
                    failed.append(tid[:8])
            except Exception:
                failed.append(tid[:8])

        embed = discord.Embed(
            title=f"✅ Bulk Complete — {len(completed)}/{len(ids)} Done",
            color=discord.Color.green() if completed else discord.Color.red()
        )
        if completed:
            embed.add_field(name="Completed", value="\n".join(f"• {t}" for t in completed[:10]), inline=False)
            embed.add_field(name="Total XP", value=f"✨ +{total_xp} XP", inline=True)
        if failed:
            embed.add_field(name="Failed", value=", ".join(f"`{f}`" for f in failed[:10]), inline=False)

        await interaction.followup.send(embed=embed)

    # 29. QUICK RESCHEDULE
    @task_group.command(name="reschedule", description="Quickly change a task's due date")
    @app_commands.describe(task_id="The UUID of the task", due_date="New due date (e.g. 'tomorrow', '3pm', 'next friday')")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    async def task_reschedule(self, interaction: discord.Interaction, task_id: str, due_date: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return

        parsed = self.parse_due_date_input(due_date)
        if not parsed:
            await interaction.response.send_message("❌ Invalid date format. Try '2h', 'tomorrow', '3pm', 'next monday', or 'YYYY-MM-DD HH:MM'.", ephemeral=True)
            return

        new_due = parsed.strftime("%Y-%m-%d %H:%M")
        await asyncio.to_thread(task_db.update_task, task_id, {"due_date": new_due, "due_warning_sent": False})
        await interaction.response.send_message(
            f"📅 **{task['title']}** rescheduled to `{new_due}`",
            ephemeral=task.get("is_private")
        )

    # 30. DAILY PLAN — auto-generate a prioritized daily agenda
    @task_group.command(name="plan", description="Auto-generate your daily plan sorted by urgency")
    async def task_plan(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)

        pending = [t for t in all_tasks if t.get("status") == "pending"]
        if not pending:
            await interaction.followup.send("✨ No pending tasks. Enjoy the break!", ephemeral=True)
            return

        now = task_db.get_ist_now()

        # Score each task: higher = do it first
        scored = []
        for t in pending:
            score = 0.0
            # Priority weight
            prio_weight = {"High": 30, "Medium": 15, "Low": 5}.get(t.get("priority"), 15)
            score += prio_weight

            # Urgency weight (closer due date = higher score)
            if t.get("due_date"):
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        due = datetime.datetime.strptime(t["due_date"].strip(), fmt)
                        hours_left = (due - now).total_seconds() / 3600
                        if hours_left < 0:
                            score += 50  # Overdue = maximum urgency
                        elif hours_left < 24:
                            score += 40
                        elif hours_left < 48:
                            score += 25
                        elif hours_left < 168:  # 1 week
                            score += 10
                        break
                    except ValueError:
                        continue

            # Habit bonus (daily habits should be done first)
            if t.get("is_habit"):
                score += 20

            # Checklist progress bonus (nearly done = finish it)
            cl = t.get("checklist") or []
            if cl:
                done_pct = sum(1 for c in cl if c.get("done")) / len(cl)
                if done_pct >= 0.7:
                    score += 15

            scored.append((score, t))

        scored.sort(key=lambda x: x[0], reverse=True)

        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        color = 0x5865F2 if interaction.user.id in report_users else 0xEB459E

        embed = discord.Embed(
            title=f"📋 Today's Plan — {interaction.user.display_name}",
            description=f"**{len(pending)} tasks** prioritized by urgency × importance:",
            color=color
        )

        lines = []
        for i, (score, t) in enumerate(scored[:12], 1):
            prio = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
            due_text = ""
            if t.get("due_date"):
                due_text = f" ⏰`{t['due_date'][:10]}`"
            habit = " 🔁" if t.get("is_habit") else ""
            urgency = ""
            if score >= 70:
                urgency = " ‼️"
            elif score >= 50:
                urgency = " ❗"

            lines.append(f"`{i:02d}.` {prio} **{t['title'][:45]}**{due_text}{habit}{urgency}")

        embed.description += "\n\n" + "\n".join(lines)

        if len(scored) > 12:
            embed.set_footer(text=f"Showing top 12 of {len(scored)} tasks • Focus on the top 3-5 for best results")
        else:
            embed.set_footer(text="Focus on the top 3-5 items for best results")

        await interaction.followup.send(embed=embed)

    # 31. MOVE CATEGORY
    @task_group.command(name="move", description="Move a task to a different category")
    @app_commands.describe(task_id="The UUID of the task", category="New category name")
    @app_commands.autocomplete(task_id=task_id_autocomplete)
    async def task_move(self, interaction: discord.Interaction, task_id: str, category: str):
        task = await asyncio.to_thread(task_db.get_task, task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return
        if task.get("user_id") != str(interaction.user.id):
            await interaction.response.send_message("❌ You are not the owner of this task.", ephemeral=True)
            return

        old_cat = task.get("category", "General")
        await asyncio.to_thread(task_db.update_task, task_id, {"category": category})
        await interaction.response.send_message(
            f"📂 **{task['title']}** moved from `{old_cat}` → `{category}`",
            ephemeral=task.get("is_private")
        )

    # 32. WORKLOAD
    @stats_group.command(name="workload", description="Visualize your task distribution by category and priority")
    async def task_workload(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        pending = [t for t in all_tasks if t.get("status") == "pending"]

        if not pending:
            await interaction.followup.send("✨ No pending tasks to analyze!", ephemeral=True)
            return

        # Category distribution
        cat_counts = {}
        for t in pending:
            cat = t.get("category", "General")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

        # Priority distribution
        prio_counts = {"High": 0, "Medium": 0, "Low": 0}
        for t in pending:
            p = t.get("priority", "Medium")
            prio_counts[p] = prio_counts.get(p, 0) + 1

        # Overdue count
        now = task_db.get_ist_now()
        overdue_count = 0
        for t in pending:
            if t.get("due_date"):
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        due = datetime.datetime.strptime(t["due_date"].strip(), fmt)
                        if due < now:
                            overdue_count += 1
                        break
                    except ValueError:
                        continue

        embed = discord.Embed(
            title=f"📊 Workload Overview — {interaction.user.display_name}",
            color=discord.Color.dark_purple()
        )

        # Priority gauge
        total = len(pending)
        high_pct = int(prio_counts["High"] / total * 100) if total else 0
        med_pct = int(prio_counts["Medium"] / total * 100) if total else 0
        low_pct = int(prio_counts["Low"] / total * 100) if total else 0

        prio_bar = f"🔴 High: **{prio_counts['High']}** ({high_pct}%)\n🟡 Medium: **{prio_counts['Medium']}** ({med_pct}%)\n🟢 Low: **{prio_counts['Low']}** ({low_pct}%)"
        embed.add_field(name="⚡ Priority Split", value=prio_bar, inline=True)

        # Category breakdown
        sorted_cats = sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)[:6]
        cat_lines = []
        for cat, count in sorted_cats:
            bar_len = min(count, 8)
            bar = "█" * bar_len + "░" * (8 - bar_len)
            cat_lines.append(f"`{bar}` **{count}** {cat}")
        embed.add_field(name="📂 By Category", value="\n".join(cat_lines), inline=True)

        # Summary
        habits = len([t for t in pending if t.get("is_habit")])
        embed.add_field(
            name="📋 Summary",
            value=f"**{total}** total pending\n**{overdue_count}** overdue ⚠️\n**{habits}** habits\n**{len(cat_counts)}** categories",
            inline=False
        )

        # Workload assessment
        if overdue_count > 5:
            status = "🚨 **Heavy backlog!** Consider using `/task matrix` to prioritize."
        elif total > 20:
            status = "📈 **High workload.** Focus on High priority first."
        elif total > 10:
            status = "⚡ **Moderate.** You're managing well."
        else:
            status = "✅ **Light workload.** Great balance!"
        embed.add_field(name="🔍 Assessment", value=status, inline=False)

        await interaction.followup.send(embed=embed)

    # 34. OVERDUE TASKS
    @task_group.command(name="overdue", description="List all your overdue tasks")
    async def task_overdue(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id_str = str(interaction.user.id)
        all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)
        pending = [t for t in all_tasks if t.get("status") == "pending" and t.get("due_date")]

        now = task_db.get_ist_now()
        overdue = []
        for t in pending:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    due = datetime.datetime.strptime(t["due_date"].strip(), fmt)
                    if due < now:
                        hours_late = (now - due).total_seconds() / 3600
                        overdue.append((hours_late, t))
                    break
                except ValueError:
                    continue

        if not overdue:
            embed = discord.Embed(
                title="✨ No Overdue Tasks!",
                description="You're all caught up. Nothing is past due.",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        overdue.sort(key=lambda x: x[0], reverse=True)  # Most overdue first

        embed = discord.Embed(
            title=f"🚨 Overdue Tasks — {len(overdue)} items",
            description="These tasks are past their deadline:",
            color=discord.Color.red()
        )

        lines = []
        for hours_late, t in overdue[:15]:
            prio = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(t.get("priority"), "🟡")
            if hours_late >= 48:
                late_str = f"**{int(hours_late/24)}d** late"
            else:
                late_str = f"**{int(hours_late)}h** late"
            lines.append(f"{prio} **{t['title'][:40]}** — {late_str} ⏰`{t['due_date'][:10]}`")

        if len(overdue) > 15:
            lines.append(f"*...and {len(overdue) - 15} more*")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Use /task reschedule to fix deadlines, or /task bulkcomplete to clear them")
        await interaction.followup.send(embed=embed)

    # 35. CLEANUP OLD COMPLETED TASKS
    @manage_group.command(name="cleanup", description="Delete all completed tasks older than N days")
    @app_commands.describe(days="Delete completed tasks older than this many days (default: 30)")
    async def task_cleanup(self, interaction: discord.Interaction, days: int = 30):
        if days < 1:
            await interaction.response.send_message("❌ Days must be at least 1.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        user_id_str = str(interaction.user.id)
        all_tasks = await asyncio.to_thread(task_db.get_user_tasks, user_id_str)

        now = task_db.get_ist_now()
        cutoff = now - datetime.timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        to_delete = [
            t for t in all_tasks
            if t.get("status") == "completed"
            and (t.get("completed_at") or "9999")[:10] < cutoff_str
        ]

        if not to_delete:
            await interaction.followup.send(f"✨ No completed tasks older than {days} days found.", ephemeral=True)
            return

        # Delete them
        deleted_count = 0
        for t in to_delete:
            try:
                await asyncio.to_thread(task_db.delete_task, str(interaction.user.id), t["task_id"])
                deleted_count += 1
            except Exception:
                pass

        embed = discord.Embed(
            title="🧹 Cleanup Complete",
            description=f"Deleted **{deleted_count}** completed tasks older than **{days} days**.",
            color=discord.Color.green()
        )
        embed.set_footer(text=f"Cutoff date: {cutoff_str}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- Helper Logic methods ---

    def parse_due_date_input(self, due_str: str) -> datetime.datetime:
        original = due_str.strip()
        due_str = original.lower()
        now = task_db.get_ist_now()
        
        # Relative time: "2h", "10m", "3d", "in 2 hours"
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

        # AM/PM support: "3pm", "3:00pm", "3:00 PM", "15:30"
        am_pm_match = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', due_str)
        if am_pm_match:
            hour = int(am_pm_match.group(1))
            minute = int(am_pm_match.group(2) or 0)
            period = am_pm_match.group(3)
            if period == 'pm' and hour != 12:
                hour += 12
            elif period == 'am' and hour == 12:
                hour = 0
            result = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if result <= now:
                result += datetime.timedelta(days=1)
            return result

        # 24-hour time: "15:30", "9:00"
        time_match = re.match(r'^(\d{1,2}):(\d{2})$', due_str)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                result = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if result <= now:
                    result += datetime.timedelta(days=1)
                return result

        # "next monday", "next tuesday", etc.
        next_day_match = re.match(r'^next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$', due_str)
        if next_day_match:
            day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
            target_day = day_map[next_day_match.group(1)]
            days_ahead = (target_day - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            result = now + datetime.timedelta(days=days_ahead)
            return result.replace(hour=12, minute=0, second=0, microsecond=0)

        # Absolute formats
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
        self.active_focus_sessions[user_id] = {
            "task": loop_task,
            "start_time": task_db.get_ist_now(),
            "duration": duration_mins,
            "title": title,
            "task_id": task_id
        }
        
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
                c_date = comp_at[:10]  # Works for both "2026-06-18T14:30" and "2026-06-18 14:30"
                if c_date in counts:
                    counts[c_date] += 1
                    
        y_values = [counts[d] for d in date_strs]
        
        fig = Figure(figsize=(6, 4), facecolor='#111214')
        ax = fig.subplots()
        ax.set_facecolor('#111214')
        
        # Plot the glowing neon line chart with circular white-bordered nodes
        ax.plot(date_labels, y_values, color='#5865F2', marker='o', linewidth=2.5, 
                markersize=8, markerfacecolor='#FFFFFF', markeredgecolor='#5865F2', markeredgewidth=2)
        
        # Semi-transparent shaded area under the curve
        ax.fill_between(date_labels, y_values, color='#5865F2', alpha=0.15)
        
        # Add data labels directly above nodes to eliminate axis-scanning
        for i, val in enumerate(y_values):
            ax.annotate(str(val), (date_labels[i], val), textcoords="offset points", 
                        xytext=(0, 8), ha='center', fontsize=9, color='#FFFFFF', fontweight='bold')
            
        ax.set_title("Weekly Task Productivity Trend", fontsize=12, fontweight='bold', color='#FFFFFF', pad=15)
        ax.set_ylabel("Tasks Completed", fontsize=10, fontweight='bold', color='#B5BAC1')
        ax.grid(axis='y', linestyle='--', alpha=0.15, color='#4E5058')
        
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('#4E5058')
        ax.spines['bottom'].set_color('#4E5058')
        ax.tick_params(colors='#B5BAC1')
        
        fig.tight_layout()
        
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=150)
        buf.seek(0)
        fig.clear()
        del fig
        
        return discord.File(fp=buf, filename="productivity_graph.png")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is not None:
            return
            
        user_id_str = str(message.author.id)
            
        content = message.content.strip().lower()
        if content not in ["home", "panel", "menu", "help", "tasks", "start", "!home", "!panel"]:
            return
            
        # Reply with the interactive DM Home Panel
        profile = await asyncio.to_thread(task_db.get_user_profile, user_id_str)
        report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
        report_users = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]
        color = 0x5865F2 if int(user_id_str) in report_users else 0xEB459E
        
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
    logger.info("[TASKS] Loaded Tasks Cog Extension.")