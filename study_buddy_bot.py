"""
Student Study Buddy Bot
=======================
A productivity bot for students: notes, to-dos, habits, study timer, reminders.
Each user's data is stored in a small SQLite file, so it survives restarts.

No ads, no spam, no upsell — just a useful tool.
"""

import os
import logging
import html
import sqlite3
import asyncio
from datetime import datetime, timedelta, time as dtime
from typing import Optional, List, Tuple
from contextlib import contextmanager
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut, TelegramError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8635684444:AAFPbqQk-V2PZGEeEHYJ7r99p8o-52GwKWY")
DB_PATH = os.getenv("DB_PATH", "study_buddy.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.FileHandler("study_buddy.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("study_buddy")

# ---------------------------------------------------------------------------
# Conversation states (stored in user_data)
# ---------------------------------------------------------------------------
STATE_KEY = "awaiting"
STATE_NOTE = "note"
STATE_TODO = "todo"
STATE_HABIT = "habit"
STATE_REMINDER_TEXT = "reminder_text"
STATE_REMINDER_TIME = "reminder_time"

# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def db_init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            joined_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            streak INTEGER NOT NULL DEFAULT 0,
            last_done_date TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            due_at TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            duration_min INTEGER NOT NULL,
            completed_at TEXT NOT NULL
        );
        """)

def ensure_user(user_id: int, first_name: str):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id, first_name, joined_at) VALUES (?, ?, ?)",
            (user_id, first_name, datetime.utcnow().isoformat()),
        )

# ---------------------------------------------------------------------------
# Study tips - rotates daily based on day of year
# ---------------------------------------------------------------------------
STUDY_TIPS = [
    "The Pomodoro Technique works because your brain craves predictable breaks. 25 minutes of focus, 5 minutes off. Try it on your hardest subject first.",
    "Active recall beats re-reading. Close the book and try to write down everything you remember. The struggle is where learning happens.",
    "Sleep is when your brain files memories. Pulling an all-nighter before an exam often hurts more than helps.",
    "Spaced repetition: review notes after 1 day, 3 days, 7 days, 14 days. You'll remember 10x more with the same total time.",
    "Teach what you just learned to an imaginary student. If you can't explain it simply, you don't understand it yet.",
    "Switch subjects every 1-2 hours. Interleaving prevents mental fatigue and improves retention.",
    "Your phone in another room is the single biggest study upgrade you can make. Not silent, not face down — another room.",
    "Hard problems first. When your brain is freshest, attack the thing you've been avoiding.",
    "Take handwritten notes when possible. Slower input forces deeper processing.",
    "Drink water before coffee. Most 'tired' is actually mild dehydration.",
    "If you can't start, set a 2-minute timer and just begin. Starting is the whole battle.",
    "Background music with lyrics hurts focus. Instrumental, lo-fi, or silence works better.",
    "Review your notes within 24 hours of class. After that, you've already forgotten 70%.",
    "Practice tests beat highlighting every time. Quiz yourself, even badly.",
    "Break big assignments into 30-minute chunks. 'Write essay' is scary. 'Outline 3 points' isn't.",
    "Move your body for 10 minutes between sessions. Walk, stretch, anything. Sitting kills focus.",
    "If you study where you sleep, your brain confuses 'study mode' and 'rest mode'. Use a different spot.",
    "Mistakes on practice problems are gold. Don't erase them — write why they were wrong.",
    "Set one goal for each study session. 'Study chapter 4' is vague. 'Solve 5 problems from chapter 4' is real.",
    "Eat protein-rich breakfast on exam days. Sugar crashes mid-paper are real.",
    "Reward yourself after milestones, not before. Earn the break.",
    "Study groups work only if everyone comes prepared. Otherwise it's a hangout.",
    "Use the Feynman technique: write a topic, explain it like you're talking to a 12-year-old, find gaps, fix them.",
    "Your weakest subject deserves the most time, not the least. We avoid what hurts.",
    "Don't 'study'. Decide exactly what success looks like for the next 30 minutes.",
    "Re-reading feels like learning but mostly isn't. If it doesn't make you struggle, it isn't sticking.",
    "A messy desk steals attention every few seconds. Clear it before you sit down.",
    "Hunger and tiredness disguise themselves as boredom. Check both before quitting a session.",
    "Read the question twice before answering. Half of all wrong answers come from misreading.",
    "Consistency beats intensity. 1 hour daily beats 7 hours on Sunday — every single time.",
]

def todays_tip() -> str:
    return STUDY_TIPS[datetime.utcnow().timetuple().tm_yday % len(STUDY_TIPS)]

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Notes", callback_data="m:notes"),
         InlineKeyboardButton("✅ To-Do", callback_data="m:todos")],
        [InlineKeyboardButton("🔥 Habits", callback_data="m:habits"),
         InlineKeyboardButton("⏰ Reminders", callback_data="m:reminders")],
        [InlineKeyboardButton("🍅 Study Timer", callback_data="m:timer"),
         InlineKeyboardButton("💡 Today's Tip", callback_data="m:tip")],
        [InlineKeyboardButton("📊 My Stats", callback_data="m:stats")],
    ])

def back_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Menu", callback_data="m:menu")

def notes_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Note", callback_data="notes:new")],
        [InlineKeyboardButton("📋 View All", callback_data="notes:list")],
        [back_btn()],
    ])

def todos_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Task", callback_data="todos:new")],
        [InlineKeyboardButton("📋 View All", callback_data="todos:list")],
        [back_btn()],
    ])

def habits_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Habit", callback_data="habits:new")],
        [InlineKeyboardButton("📋 View & Check-In", callback_data="habits:list")],
        [back_btn()],
    ])

def reminders_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ New Reminder", callback_data="rem:new")],
        [InlineKeyboardButton("📋 View All", callback_data="rem:list")],
        [back_btn()],
    ])

def timer_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍅 25 min focus", callback_data="timer:25"),
         InlineKeyboardButton("☕ 5 min break", callback_data="timer:5")],
        [InlineKeyboardButton("📚 50 min deep", callback_data="timer:50"),
         InlineKeyboardButton("⏸ 15 min break", callback_data="timer:15")],
        [back_btn()],
    ])

# ---------------------------------------------------------------------------
# /start and /help
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.first_name or "")
    name = html.escape(user.first_name or "there")
    text = (
        f"👋 Hey <b>{name}</b>!\n\n"
        "I'm your <b>Study Buddy</b> — a quiet productivity tool for students.\n\n"
        "<b>What I do:</b>\n"
        "• 📝 Notes — quick thoughts, ideas, formulas\n"
        "• ✅ To-Do — assignments and tasks\n"
        "• 🔥 Habits — track daily streaks (revision, exercise, reading)\n"
        "• ⏰ Reminders — set them in your own words\n"
        "• 🍅 Study Timer — Pomodoro-style focus sessions\n"
        "• 💡 Daily Tip — one practical study insight every day\n\n"
        "Pick something to start:"
    )
    await update.message.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Commands</b>\n\n"
        "/start — open menu\n"
        "/menu — same as /start\n"
        "/note &lt;text&gt; — quick note (e.g. <code>/note physics ch5 done</code>)\n"
        "/todo &lt;text&gt; — quick task\n"
        "/tip — today's study tip\n"
        "/stats — your activity\n"
        "/help — this message\n\n"
        "Or just tap menu buttons — no typing needed."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_tip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"💡 <b>Today's Study Tip</b>\n\n{todays_tip()}",
        parse_mode=ParseMode.HTML,
    )

# ---------------------------------------------------------------------------
# Quick commands
# ---------------------------------------------------------------------------
async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: <code>/note your text here</code>", parse_mode=ParseMode.HTML)
        return
    text = " ".join(context.args).strip()[:1000]
    user = update.effective_user
    ensure_user(user.id, user.first_name or "")
    with db() as c:
        c.execute("INSERT INTO notes(user_id, content, created_at) VALUES (?,?,?)",
                  (user.id, text, datetime.utcnow().isoformat()))
    await update.message.reply_text("📝 Saved.")

async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: <code>/todo task here</code>", parse_mode=ParseMode.HTML)
        return
    text = " ".join(context.args).strip()[:500]
    user = update.effective_user
    ensure_user(user.id, user.first_name or "")
    with db() as c:
        c.execute("INSERT INTO todos(user_id, content, created_at) VALUES (?,?,?)",
                  (user.id, text, datetime.utcnow().isoformat()))
    await update.message.reply_text("✅ Added to your list.")

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_stats(update.effective_user.id, update.message.reply_text)

async def show_stats(user_id: int, reply_fn):
    with db() as c:
        notes_n = c.execute("SELECT COUNT(*) FROM notes WHERE user_id=?", (user_id,)).fetchone()[0]
        todo_total = c.execute("SELECT COUNT(*) FROM todos WHERE user_id=?", (user_id,)).fetchone()[0]
        todo_done = c.execute("SELECT COUNT(*) FROM todos WHERE user_id=? AND done=1", (user_id,)).fetchone()[0]
        habits_n = c.execute("SELECT COUNT(*) FROM habits WHERE user_id=?", (user_id,)).fetchone()[0]
        best_streak = c.execute("SELECT MAX(streak) FROM habits WHERE user_id=?", (user_id,)).fetchone()[0] or 0
        sessions_n = c.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,)).fetchone()[0]
        focus_min = c.execute("SELECT COALESCE(SUM(duration_min),0) FROM sessions WHERE user_id=?", (user_id,)).fetchone()[0]

    text = (
        "📊 <b>Your Stats</b>\n\n"
        f"📝 Notes saved: <b>{notes_n}</b>\n"
        f"✅ Tasks: <b>{todo_done}/{todo_total}</b> done\n"
        f"🔥 Habits tracked: <b>{habits_n}</b>\n"
        f"⭐ Best streak: <b>{best_streak} days</b>\n"
        f"🍅 Focus sessions: <b>{sessions_n}</b>\n"
        f"⏱ Total focus time: <b>{focus_min} min</b>"
    )
    await reply_fn(text, reply_markup=InlineKeyboardMarkup([[back_btn()]]), parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
async def show_notes_list(user_id: int, edit_fn):
    with db() as c:
        rows = c.execute(
            "SELECT id, content, created_at FROM notes WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user_id,),
        ).fetchall()
    if not rows:
        await edit_fn("📝 No notes yet. Tap ➕ New Note to add one.",
                      reply_markup=notes_menu(), parse_mode=ParseMode.HTML)
        return
    lines = ["📝 <b>Your Notes</b> (latest 20)\n"]
    buttons = []
    for r in rows:
        snippet = html.escape(r["content"][:60]) + ("…" if len(r["content"]) > 60 else "")
        lines.append(f"• {snippet}")
        buttons.append([InlineKeyboardButton(f"🗑 Delete: {r['content'][:25]}", callback_data=f"notes:del:{r['id']}")])
    buttons.append([back_btn()])
    await edit_fn("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# To-Dos
# ---------------------------------------------------------------------------
async def show_todos_list(user_id: int, edit_fn):
    with db() as c:
        rows = c.execute(
            "SELECT id, content, done FROM todos WHERE user_id=? ORDER BY done, id DESC LIMIT 20",
            (user_id,),
        ).fetchall()
    if not rows:
        await edit_fn("✅ No tasks yet. Tap ➕ New Task.",
                      reply_markup=todos_menu(), parse_mode=ParseMode.HTML)
        return
    lines = ["✅ <b>Your Tasks</b>\n"]
    buttons = []
    for r in rows:
        mark = "☑️" if r["done"] else "⬜"
        snippet = html.escape(r["content"][:50])
        lines.append(f"{mark} {snippet}")
        if r["done"]:
            buttons.append([InlineKeyboardButton(f"🗑 Remove: {r['content'][:25]}", callback_data=f"todos:del:{r['id']}")])
        else:
            buttons.append([InlineKeyboardButton(f"✓ Done: {r['content'][:25]}", callback_data=f"todos:done:{r['id']}")])
    buttons.append([back_btn()])
    await edit_fn("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------
def update_habit_streak(habit_id: int, user_id: int) -> Tuple[int, str]:
    """Returns (new_streak, message)."""
    today = datetime.utcnow().date().isoformat()
    with db() as c:
        row = c.execute("SELECT streak, last_done_date FROM habits WHERE id=? AND user_id=?",
                        (habit_id, user_id)).fetchone()
        if not row:
            return 0, "Habit not found."
        last = row["last_done_date"]
        streak = row["streak"]
        if last == today:
            return streak, f"Already checked in today. Streak: {streak} 🔥"
        yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
        if last == yesterday:
            streak += 1
        else:
            streak = 1
        c.execute("UPDATE habits SET streak=?, last_done_date=? WHERE id=?", (streak, today, habit_id))
    msg = f"Done. Streak: {streak} 🔥"
    if streak in (3, 7, 14, 30, 50, 100):
        msg += f"\n\n🎉 Milestone: {streak} days. Keep going."
    return streak, msg

async def show_habits_list(user_id: int, edit_fn):
    with db() as c:
        rows = c.execute(
            "SELECT id, name, streak, last_done_date FROM habits WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    if not rows:
        await edit_fn("🔥 No habits yet. Tap ➕ New Habit.\n\n"
                      "Examples: <i>'Revise notes'</i>, <i>'30 min reading'</i>, <i>'No phone after 10pm'</i>",
                      reply_markup=habits_menu(), parse_mode=ParseMode.HTML)
        return
    today = datetime.utcnow().date().isoformat()
    lines = ["🔥 <b>Your Habits</b>\n"]
    buttons = []
    for r in rows:
        checked = "✅" if r["last_done_date"] == today else "⬜"
        lines.append(f"{checked} <b>{html.escape(r['name'])}</b> — {r['streak']} day streak")
        if r["last_done_date"] != today:
            buttons.append([InlineKeyboardButton(f"✓ Check in: {r['name'][:25]}", callback_data=f"habits:checkin:{r['id']}")])
        buttons.append([InlineKeyboardButton(f"🗑 Delete: {r['name'][:25]}", callback_data=f"habits:del:{r['id']}")])
    buttons.append([back_btn()])
    await edit_fn("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
def parse_reminder_time(text: str) -> Optional[datetime]:
    """Accepts: '15m', '2h', '1d', or 'HH:MM' (24h, today/tomorrow)."""
    text = text.strip().lower()
    now = datetime.utcnow()
    try:
        if text.endswith("m") and text[:-1].isdigit():
            return now + timedelta(minutes=int(text[:-1]))
        if text.endswith("h") and text[:-1].isdigit():
            return now + timedelta(hours=int(text[:-1]))
        if text.endswith("d") and text[:-1].isdigit():
            return now + timedelta(days=int(text[:-1]))
        if ":" in text:
            hh, mm = text.split(":")
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
    except (ValueError, IndexError):
        return None
    return None

async def show_reminders_list(user_id: int, edit_fn):
    with db() as c:
        rows = c.execute(
            "SELECT id, content, due_at FROM reminders WHERE user_id=? AND sent=0 ORDER BY due_at",
            (user_id,),
        ).fetchall()
    if not rows:
        await edit_fn("⏰ No active reminders.\n\nTap ➕ New Reminder to set one.",
                      reply_markup=reminders_menu(), parse_mode=ParseMode.HTML)
        return
    lines = ["⏰ <b>Your Reminders</b>\n"]
    buttons = []
    for r in rows:
        due = datetime.fromisoformat(r["due_at"])
        when = due.strftime("%d %b, %H:%M UTC")
        snippet = html.escape(r["content"][:50])
        lines.append(f"• <b>{when}</b> — {snippet}")
        buttons.append([InlineKeyboardButton(f"🗑 Cancel: {r['content'][:20]}", callback_data=f"rem:del:{r['id']}")])
    buttons.append([back_btn()])
    await edit_fn("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)

# Background job: deliver due reminders
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now_iso = datetime.utcnow().isoformat()
    with db() as c:
        rows = c.execute(
            "SELECT id, user_id, content FROM reminders WHERE sent=0 AND due_at<=?",
            (now_iso,),
        ).fetchall()
        for r in rows:
            try:
                await context.bot.send_message(
                    chat_id=r["user_id"],
                    text=f"⏰ <b>Reminder</b>\n\n{html.escape(r['content'])}",
                    parse_mode=ParseMode.HTML,
                )
                c.execute("UPDATE reminders SET sent=1 WHERE id=?", (r["id"],))
            except Forbidden:
                # User blocked the bot; mark as sent to avoid retrying
                c.execute("UPDATE reminders SET sent=1 WHERE id=?", (r["id"],))
            except Exception as e:
                log.warning("Reminder send failed for %s: %s", r["user_id"], e)

# ---------------------------------------------------------------------------
# Pomodoro timer
# ---------------------------------------------------------------------------
async def start_timer(user_id: int, minutes: int, context: ContextTypes.DEFAULT_TYPE, edit_fn):
    label = "🍅 Focus session" if minutes >= 25 else "☕ Break"
    await edit_fn(
        f"{label} started: <b>{minutes} minutes</b>\n\n"
        f"I'll ping you when it's done. Put your phone away and go.",
        reply_markup=InlineKeyboardMarkup([[back_btn()]]),
        parse_mode=ParseMode.HTML,
    )
    context.job_queue.run_once(
        timer_done_callback,
        when=minutes * 60,
        data={"user_id": user_id, "minutes": minutes},
        name=f"timer_{user_id}_{datetime.utcnow().timestamp()}",
    )

async def timer_done_callback(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    user_id = data["user_id"]
    minutes = data["minutes"]
    if minutes >= 25:
        with db() as c:
            c.execute("INSERT INTO sessions(user_id, duration_min, completed_at) VALUES (?,?,?)",
                      (user_id, minutes, datetime.utcnow().isoformat()))
        msg = f"🎉 <b>{minutes} min focus complete</b>\n\nGreat work. Take a real break — stand up, stretch, drink water."
    else:
        msg = f"⏰ <b>{minutes} min break done</b>\n\nReady for the next session?"
    try:
        await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
    except Forbidden:
        pass
    except Exception as e:
        log.warning("Timer ping failed: %s", e)

# ---------------------------------------------------------------------------
# Callback dispatcher
# ---------------------------------------------------------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    user_id = q.from_user.id
    data = q.data or ""
    edit = q.edit_message_text  # alias

    try:
        # Main menu
        if data == "m:menu":
            await edit("🏠 <b>Main Menu</b>\n\nPick a tool:", reply_markup=main_menu(), parse_mode=ParseMode.HTML)
            return
        if data == "m:notes":
            await edit("📝 <b>Notes</b>\n\nQuick thoughts, formulas, ideas — anything.",
                       reply_markup=notes_menu(), parse_mode=ParseMode.HTML)
            return
        if data == "m:todos":
            await edit("✅ <b>To-Do List</b>\n\nAdd tasks, check them off.",
                       reply_markup=todos_menu(), parse_mode=ParseMode.HTML)
            return
        if data == "m:habits":
            await edit("🔥 <b>Habits</b>\n\nDaily check-ins build streaks. Miss a day, streak resets.",
                       reply_markup=habits_menu(), parse_mode=ParseMode.HTML)
            return
        if data == "m:reminders":
            await edit("⏰ <b>Reminders</b>\n\nSet one-time pings.",
                       reply_markup=reminders_menu(), parse_mode=ParseMode.HTML)
            return
        if data == "m:timer":
            await edit("🍅 <b>Pomodoro Timer</b>\n\nFocus in chunks. Pick a duration:",
                       reply_markup=timer_menu(), parse_mode=ParseMode.HTML)
            return
        if data == "m:tip":
            await edit(f"💡 <b>Today's Study Tip</b>\n\n{todays_tip()}",
                       reply_markup=InlineKeyboardMarkup([[back_btn()]]), parse_mode=ParseMode.HTML)
            return
        if data == "m:stats":
            await show_stats(user_id, edit)
            return

        # Notes
        if data == "notes:list":
            await show_notes_list(user_id, edit)
            return
        if data == "notes:new":
            context.user_data[STATE_KEY] = STATE_NOTE
            await edit("📝 Send me the note text.\n\n<i>Send /cancel to abort.</i>",
                       reply_markup=InlineKeyboardMarkup([[back_btn()]]), parse_mode=ParseMode.HTML)
            return
        if data.startswith("notes:del:"):
            nid = int(data.split(":")[2])
            with db() as c:
                c.execute("DELETE FROM notes WHERE id=? AND user_id=?", (nid, user_id))
            await show_notes_list(user_id, edit)
            return

        # Todos
        if data == "todos:list":
            await show_todos_list(user_id, edit)
            return
        if data == "todos:new":
            context.user_data[STATE_KEY] = STATE_TODO
            await edit("✅ Send me the task.\n\n<i>/cancel to abort.</i>",
                       reply_markup=InlineKeyboardMarkup([[back_btn()]]), parse_mode=ParseMode.HTML)
            return
        if data.startswith("todos:done:"):
            tid = int(data.split(":")[2])
            with db() as c:
                c.execute("UPDATE todos SET done=1 WHERE id=? AND user_id=?", (tid, user_id))
            await show_todos_list(user_id, edit)
            return
        if data.startswith("todos:del:"):
            tid = int(data.split(":")[2])
            with db() as c:
                c.execute("DELETE FROM todos WHERE id=? AND user_id=?", (tid, user_id))
            await show_todos_list(user_id, edit)
            return

        # Habits
        if data == "habits:list":
            await show_habits_list(user_id, edit)
            return
        if data == "habits:new":
            context.user_data[STATE_KEY] = STATE_HABIT
            await edit("🔥 Send me the habit name.\n\n<i>Examples: 'Revise notes', '30 min reading'</i>",
                       reply_markup=InlineKeyboardMarkup([[back_btn()]]), parse_mode=ParseMode.HTML)
            return
        if data.startswith("habits:checkin:"):
            hid = int(data.split(":")[2])
            _, msg = update_habit_streak(hid, user_id)
            await q.answer(msg, show_alert=True)
            await show_habits_list(user_id, edit)
            return
        if data.startswith("habits:del:"):
            hid = int(data.split(":")[2])
            with db() as c:
                c.execute("DELETE FROM habits WHERE id=? AND user_id=?", (hid, user_id))
            await show_habits_list(user_id, edit)
            return

        # Reminders
        if data == "rem:list":
            await show_reminders_list(user_id, edit)
            return
        if data == "rem:new":
            context.user_data[STATE_KEY] = STATE_REMINDER_TEXT
            await edit("⏰ What's the reminder about?\n\n<i>Send the text first, then I'll ask when.</i>",
                       reply_markup=InlineKeyboardMarkup([[back_btn()]]), parse_mode=ParseMode.HTML)
            return
        if data.startswith("rem:del:"):
            rid = int(data.split(":")[2])
            with db() as c:
                c.execute("UPDATE reminders SET sent=1 WHERE id=? AND user_id=?", (rid, user_id))
            await show_reminders_list(user_id, edit)
            return

        # Timer
        if data.startswith("timer:"):
            mins = int(data.split(":")[1])
            await start_timer(user_id, mins, context, edit)
            return

        await edit("Unknown action.", reply_markup=main_menu())

    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        log.warning("BadRequest: %s", e)
    except Forbidden:
        log.info("User %s blocked the bot", user_id)
    except TelegramError as e:
        log.warning("Telegram error: %s", e)

# ---------------------------------------------------------------------------
# Free-text handler — handles "awaiting" states
# ---------------------------------------------------------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.first_name or "")
    text = (update.message.text or "").strip()
    state = context.user_data.get(STATE_KEY)

    if not state:
        await update.message.reply_text(
            "Tap /start to open the menu, or /help for commands."
        )
        return

    if state == STATE_NOTE:
        with db() as c:
            c.execute("INSERT INTO notes(user_id, content, created_at) VALUES (?,?,?)",
                      (user.id, text[:1000], datetime.utcnow().isoformat()))
        context.user_data.pop(STATE_KEY, None)
        await update.message.reply_text("📝 Note saved.", reply_markup=notes_menu())
        return

    if state == STATE_TODO:
        with db() as c:
            c.execute("INSERT INTO todos(user_id, content, created_at) VALUES (?,?,?)",
                      (user.id, text[:500], datetime.utcnow().isoformat()))
        context.user_data.pop(STATE_KEY, None)
        await update.message.reply_text("✅ Task added.", reply_markup=todos_menu())
        return

    if state == STATE_HABIT:
        with db() as c:
            c.execute("INSERT INTO habits(user_id, name, created_at) VALUES (?,?,?)",
                      (user.id, text[:80], datetime.utcnow().isoformat()))
        context.user_data.pop(STATE_KEY, None)
        await update.message.reply_text(
            f"🔥 Habit '<b>{html.escape(text[:80])}</b>' added.\n\nCheck in daily to build a streak.",
            reply_markup=habits_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

    if state == STATE_REMINDER_TEXT:
        context.user_data["reminder_content"] = text[:500]
        context.user_data[STATE_KEY] = STATE_REMINDER_TIME
        await update.message.reply_text(
            "⏰ When?\n\n"
            "Say it like:\n"
            "• <code>15m</code> — in 15 minutes\n"
            "• <code>2h</code> — in 2 hours\n"
            "• <code>1d</code> — in 1 day\n"
            "• <code>18:30</code> — at 18:30 (24h, UTC)",
            parse_mode=ParseMode.HTML,
        )
        return

    if state == STATE_REMINDER_TIME:
        due = parse_reminder_time(text)
        if not due:
            await update.message.reply_text(
                "I didn't get that. Try <code>15m</code>, <code>2h</code>, <code>1d</code>, or <code>18:30</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        content = context.user_data.pop("reminder_content", "Reminder")
        with db() as c:
            c.execute("INSERT INTO reminders(user_id, content, due_at) VALUES (?,?,?)",
                      (user.id, content, due.isoformat()))
        context.user_data.pop(STATE_KEY, None)
        await update.message.reply_text(
            f"⏰ Set for <b>{due.strftime('%d %b, %H:%M UTC')}</b>.",
            reply_markup=reminders_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(STATE_KEY, None)
    context.user_data.pop("reminder_content", None)
    await update.message.reply_text("Cancelled.", reply_markup=main_menu())

# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception", exc_info=context.error)
    if isinstance(context.error, (NetworkError, TimedOut)):
        return
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again."
            )
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def build_app() -> Application:
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit("BOT_TOKEN env var not set.")
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("tip", cmd_tip))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("todo", cmd_todo))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    # Background reminder checker — every 30 seconds
    app.job_queue.run_repeating(reminder_job, interval=30, first=10)

    return app

def main():
    log.info("Starting Study Buddy Bot…")
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
