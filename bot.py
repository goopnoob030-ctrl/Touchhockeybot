import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
Application, CommandHandler, CallbackQueryHandler,
MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [8695966053]   # Замени на Telegram ID администраторов

db = sqlite3.connect("touchhockey.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS players (
user_id INTEGER PRIMARY KEY,
username TEXT,
nickname TEXT,
club TEXT
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS clubs (
name TEXT PRIMARY KEY,
owner_id INTEGER
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS transfers (
id INTEGER PRIMARY KEY AUTOINCREMENT,
player_id INTEGER,
old_club TEXT,
new_club TEXT,
status TEXT
)
""")
db.commit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
user = update.effective_user
db.execute(
"INSERT OR IGNORE INTO players VALUES (?, ?, ?, ?)",
(user.id, user.username, user.first_name, None)
)
db.commit()

keyboard = [
    [InlineKeyboardButton("🏆 Взять клуб", callback_data="clubs")],
    [InlineKeyboardButton("📋 Реестр клубов", callback_data="registry")],
    [InlineKeyboardButton("⚙️ Управление клубом", callback_data="management")],
    [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
    [InlineKeyboardButton("🆓 Свободные агенты", callback_data="free")]
]

await update.message.reply_text(
    "🏒 TOUCH HOCKEY TM\n\nДобро пожаловать!",
    reply_markup=InlineKeyboardMarkup(keyboard)
)

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
user_id = query.from_user.id

if query.data == "clubs":
    rows = db.execute("SELECT name FROM clubs").fetchall()
    keyboard = [
        [InlineKeyboardButton(row[0], callback_data="take_" + row[0])]
        for row in rows
    ]
    await query.edit_message_text(
        "🏆 Выбери клуб:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

elif query.data.startswith("take_"):
    club = query.data[5:]
    owner = db.execute(
        "SELECT owner_id FROM clubs WHERE name=?", (club,)
    ).fetchone()

    if owner and owner[0] is not None:
        await query.edit_message_text("❌ Этот клуб уже занят.")
        return

    db.execute("UPDATE clubs SET owner_id=? WHERE name=?", (user_id, club))
    db.execute("UPDATE players SET club=? WHERE user_id=?", (club, user_id))
    db.commit()
    await query.edit_message_text(f"✅ Ты получил клуб: {club}")

elif query.data == "registry":
    rows = db.execute("SELECT name, owner_id FROM clubs").fetchall()
    text = "📋 РЕЕСТР КЛУБОВ\n\n"
    for name, owner in rows:
        text += f"🏒 {name} — {'Занят' if owner else 'Свободен'}\n"
    await query.edit_message_text(text)

elif query.data == "management":
    owner = db.execute(
        "SELECT name FROM clubs WHERE owner_id=?", (user_id,)
    ).fetchone()

    if not owner:
        await query.edit_message_text("❌ Сначала возьми клуб.")
        return

    keyboard = [
        [InlineKeyboardButton("📨 Пригласить игрока", callback_data="invite")],
        [InlineKeyboardButton("👥 Состав клуба", callback_data="squad")],
    ]
    await query.edit_message_text(
        f"⚙️ Управление клубом: {owner[0]}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

elif query.data == "invite":
    context.user_data["waiting_invite"] = True
    await query.edit_message_text(
        "📨 Введи игровой ник или Telegram @username игрока."
    )

elif query.data == "squad":
    club = db.execute(
        "SELECT name FROM clubs WHERE owner_id=?", (user_id,)
    ).fetchone()
    players = db.execute(
        "SELECT nickname FROM players WHERE club=?", (club[0],)
    ).fetchall()
    text = "👥 СОСТАВ КЛУБА\n\n"
    text += "\n".join(p[0] for p in players) if players else "Пока пусто."
    await query.edit_message_text(text)

elif query.data == "profile":
    player = db.execute(
        "SELECT nickname, club FROM players WHERE user_id=?", (user_id,)
    ).fetchone()
    await query.edit_message_text(
        f"👤 Профиль\n\nНик: {player[0]}\nКлуб: {player[1] or 'Свободный агент'}"
    )

elif query.data == "free":
    players = db.execute(
        "SELECT nickname FROM players WHERE club IS NULL"
    ).fetchall()
    text = "🆓 СВОБОДНЫЕ АГЕНТЫ\n\n"
    text += "\n".join(p[0] for p in players) if players else "Нет игроков."
    await query.edit_message_text(text)

elif query.data.startswith("accept_"):
    transfer_id = int(query.data.split("_")[1])
    transfer = db.execute(
        "SELECT player_id, old_club, new_club, status FROM transfers WHERE id=?",
        (transfer_id,)
    ).fetchone()

    if not transfer or transfer[0] != user_id or transfer[3] != "invited":
        await query.edit_message_text("Заявка недействительна.")
        return

    db.execute(
        "UPDATE transfers SET status='pending' WHERE id=?", (transfer_id,)
    )
    db.commit()

    keyboard = [[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{transfer_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{transfer_id}")
    ]]

    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            admin_id,
            f"📨 ЗАЯВКА НА ТРАНСФЕР\n\n"
            f"Игрок: {user_id}\n"
            f"Из клуба: {transfer[1]}\n"
            f"В клуб: {transfer[2]}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    await query.edit_message_text(
        "✅ Ты согласился! Заявка отправлена администраторам."
    )

elif query.data.startswith("rejectinvite_"):
    await query.edit_message_text("❌ Ты отказался от приглашения.")

elif query.data.startswith("approve_") or query.data.startswith("reject_"):
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("⛔ Нет доступа.")
        return

    transfer_id = int(query.data.split("_")[1])
    transfer = db.execute(
        "SELECT player_id, old_club, new_club, status FROM transfers WHERE id=?",
        (transfer_id,)
    ).fetchone()

    if not transfer or transfer[3] != "pending":
        await query.edit_message_text("Заявка уже обработана.")
        return

    player_id, old_club, new_club, status = transfer

    if query.data.startswith("approve_"):
        db.execute(
            "UPDATE players SET club=? WHERE user_id=?",
            (new_club, player_id)
        )
        db.execute(
            "UPDATE transfers SET status='approved' WHERE id=?",
            (transfer_id,)
        )
        await context.bot.send_message(
            player_id, f"🎉 Трансфер одобрен! Теперь твой клуб: {new_club}"
        )
        await query.edit_message_text("✅ Трансфер одобрен.")

    else:
        db.execute(
            "UPDATE transfers SET status='rejected' WHERE id=?",
            (transfer_id,)
        )
        await context.bot.send_message(
            player_id, "❌ Администраторы отклонили трансфер."
        )
        await query.edit_message_text("❌ Трансфер отклонён.")

    db.commit()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not context.user_data.get("waiting_invite"):
return

context.user_data["waiting_invite"] = False
target = update.message.text.strip().lstrip("@")

if target.isdigit():
    player = db.execute(
        "SELECT user_id, club FROM players WHERE user_id=?", (int(target),)
    ).fetchone()
else:
    player = db.execute(
        "SELECT user_id, club FROM players WHERE LOWER(username)=LOWER(?) OR LOWER(nickname)=LOWER(?)",
        (target, target)
    ).fetchone()

if not player:
    await update.message.reply_text(
        "❌ Игрок не найден. Он должен запустить бота через /start."
    )
    return

player_id, old_club = player
new_club = db.execute(
    "SELECT name FROM clubs WHERE owner_id=?", (update.effective_user.id,)
).fetchone()

if not new_club:
    await update.message.reply_text("❌ У тебя нет клуба.")
    return

if player_id == update.effective_user.id:
    await update.message.reply_text("❌ Нельзя пригласить самого себя.")
    return

if old_club == new_club[0]:
    await update.message.reply_text("❌ Игрок уже в твоём клубе.")
    return

pending = db.execute(
    "SELECT id FROM transfers WHERE player_id=? AND status IN ('invited','pending')",
    (player_id,)
).fetchone()

if pending:
    await update.message.reply_text("❌ У игрока уже есть активная заявка.")
    return

cursor = db.execute(
    "INSERT INTO transfers (player_id, old_club, new_club, status) VALUES (?, ?, ?, 'invited')",
    (player_id, old_club, new_club[0])
)
db.commit()

transfer_id = cursor.lastrowid
keyboard = [[
    InlineKeyboardButton("✅ Согласиться", callback_data=f"accept_{transfer_id}"),
    InlineKeyboardButton("❌ Отказаться", callback_data=f"rejectinvite_{transfer_id}")
]]

await context.bot.send_message(
    player_id,
    f"📨 ТЕБЯ ПРИГЛАШАЮТ В КЛУБ!\n\n"
    f"Текущий клуб: {old_club or 'Свободный агент'}\n"
    f"Новый клуб: {new_club[0]}\n\n"
    "Согласен перейти?",
    reply_markup=InlineKeyboardMarkup(keyboard)
)

await update.message.reply_text("✅ Приглашение отправлено игроку.")

async def addclub(update: Update, context: ContextTypes.DEFAULT_TYPE):
if update.effective_user.id not in ADMIN_IDS:
return

if not context.args:
    await update.message.reply_text("Использование: /addclub Название")
    return

name = " ".join(context.args)
try:
    db.execute("INSERT INTO clubs (name) VALUES (?)", (name,))
    db.commit()
    await update.message.reply_text(f"✅ Клуб {name} добавлен.")
except sqlite3.IntegrityError:
    await update.message.reply_text("Такой клуб уже существует.")

def main():
if not TOKEN:
raise RuntimeError("Добавь BOT_TOKEN в Secrets хостинга.")

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("addclub", addclub))
app.add_handler(CallbackQueryHandler(buttons))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
app.run_polling()

if name == "main":
main()
