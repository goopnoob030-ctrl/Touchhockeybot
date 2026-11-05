
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "8695966053").split(",")
    if x.strip()
]

CLUBS = [
    "Amsterdam",
    "Madrid",
    "London",
    "Paris",
    "Milan",
    "Munich",
    "Barcelona",
    "Rotterdam",
    "Copenhagen",
    "Liverpool",
]

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

for club in CLUBS:
    db.execute(
        "INSERT OR IGNORE INTO clubs (name, owner_id) VALUES (?, NULL)",
        (club,)
    )
db.commit()


def get_player(user_id):
    return db.execute(
        "SELECT user_id, username, nickname, club FROM players WHERE user_id=?",
        (user_id,)
    ).fetchone()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    db.execute("""
        INSERT OR IGNORE INTO players (user_id, username, nickname, club)
        VALUES (?, ?, ?, NULL)
    """, (user.id, user.username, user.first_name))

    db.execute(
        "UPDATE players SET username=? WHERE user_id=?",
        (user.username, user.id)
    )
    db.commit()

    keyboard = [
        [InlineKeyboardButton("🏆 Взять клуб", callback_data="clubs")],
        [InlineKeyboardButton("📋 Реестр клубов", callback_data="registry")],
        [InlineKeyboardButton("⚙️ Управление клубом", callback_data="management")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🆓 Свободные агенты", callback_data="free")],
    ]

    await update.message.reply_text(
        "🏒 TOUCH HOCKEY TM\n\n"
        "Добро пожаловать!\n\n"
        "Для изменения игрового ника используй /nickname.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: /nickname ТВОЙ_НИК"
        )
        return

    name = " ".join(context.args).strip()

    if len(name) > 30:
        await update.message.reply_text("❌ Максимум 30 символов.")
        return

    db.execute(
        "UPDATE players SET nickname=? WHERE user_id=?",
        (name, update.effective_user.id)
    )
    db.commit()

    await update.message.reply_text(
        f"✅ Твой игровой ник: {name}"
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "clubs":
        rows = db.execute(
            "SELECT name, owner_id FROM clubs ORDER BY name"
        ).fetchall()

        keyboard = [
            [InlineKeyboardButton(
                f"{'🔒' if owner else '🟢'} {name}",
                callback_data=f"take_{name}"
            )]
            for name, owner in rows
        ]

        await query.edit_message_text(
            "🏆 ВЫБЕРИ КЛУБ:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("take_"):
        club = data[5:]

        owner = db.execute(
            "SELECT owner_id FROM clubs WHERE name=?",
            (club,)
        ).fetchone()

        player = get_player(user_id)

        if not owner:
            await query.edit_message_text("❌ Клуб не найден.")
            return

        if owner[0] is not None:
            await query.edit_message_text("❌ Этот клуб уже занят.")
            return

        if player and player[3]:
            await query.edit_message_text(
                "❌ У тебя уже есть клуб или ты состоишь в другом клубе."
            )
            return

        db.execute(
            "UPDATE clubs SET owner_id=? WHERE name=? AND owner_id IS NULL",
            (user_id, club)
        )
        db.execute(
            "UPDATE players SET club=? WHERE user_id=?",
            (club, user_id)
        )
        db.commit()

        await query.edit_message_text(
            f"✅ Ты стал владельцем клуба {club}!"
        )

    elif data == "registry":
        rows = db.execute(
            "SELECT name, owner_id FROM clubs ORDER BY name"
        ).fetchall()

        text = "📋 РЕЕСТР КЛУБОВ\n\n"

        for name, owner in rows:
            status = "Занят" if owner else "Свободен"
            text += f"🏒 {name} — {status}\n"

        await query.edit_message_text(text)

    elif data == "management":
        owner = db.execute(
            "SELECT name FROM clubs WHERE owner_id=?",
            (user_id,)
        ).fetchone()

        if not owner:
            await query.edit_message_text(
                "❌ Сначала получи клуб."
            )
            return

        keyboard = [
            [InlineKeyboardButton(
                "📨 Пригласить игрока",
                callback_data="invite"
            )],
            [InlineKeyboardButton(
                "👥 Состав клуба",
                callback_data="squad"
            )],
        ]

        await query.edit_message_text(
            f"⚙️ УПРАВЛЕНИЕ КЛУБОМ\n\n🏒 {owner[0]}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "invite":
        context.user_data["waiting_invite"] = True

        await query.edit_message_text(
            "📨 Отправь игровой ник или Telegram @username игрока.\n\n"
            "Игрок должен предварительно запустить бота через /start."
        )

    elif data == "squad":
        club = db.execute(
            "SELECT name FROM clubs WHERE owner_id=?",
            (user_id,)
        ).fetchone()

        if not club:
            await query.edit_message_text("❌ У тебя нет клуба.")
            return

        players = db.execute(
            "SELECT nickname FROM players WHERE club=?",
            (club[0],)
        ).fetchall()

        text = f"👥 СОСТАВ {club[0]}\n\n"
        text += "\n".join(p[0] for p in players) if players else "Пока пусто."

        await query.edit_message_text(text)

    elif data == "profile":
        player = get_player(user_id)

        if not player:
            await query.edit_message_text("Сначала напиши /start.")
            return

        await query.edit_message_text(
            f"👤 ПРОФИЛЬ\n\n"
            f"Ник: {player[2]}\n"
            f"Telegram: @{player[1] or 'не указан'}\n"
            f"Клуб: {player[3] or 'Свободный агент'}"
        )

    elif data == "free":
        players = db.execute(
            "SELECT nickname FROM players WHERE club IS NULL"
        ).fetchall()

        text = "🆓 СВОБОДНЫЕ АГЕНТЫ\n\n"
        text += "\n".join(p[0] for p in players) if players else "Нет игроков."

        await query.edit_message_text(text)

    elif data.startswith("accept_"):
        transfer_id = int(data.split("_")[1])

        transfer = db.execute(
            "SELECT player_id, old_club, new_club, status "
            "FROM transfers WHERE id=?",
            (transfer_id,)
        ).fetchone()

        if not transfer or transfer[0] != user_id or transfer[3] != "invited":
            await query.edit_message_text("❌ Заявка недействительна.")
            return

        db.execute(
            "UPDATE transfers SET status='pending' WHERE id=?",
            (transfer_id,)
        )
        db.commit()

        keyboard = [[
            InlineKeyboardButton(
                "✅ Одобрить",
                callback_data=f"approve_{transfer_id}"
            ),
            InlineKeyboardButton(
                "❌ Отклонить",
                callback_data=f"reject_{transfer_id}"
            )
        ]]

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"📨 ЗАЯВКА НА ТРАНСФЕР\n\n"
                    f"Игрок: {user_id}\n"
                    f"Из клуба: {transfer[1] or 'Свободный агент'}\n"
                    f"В клуб: {transfer[2]}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception:
                pass

        await query.edit_message_text(
            "✅ Ты согласился!\nЗаявка отправлена администраторам."
        )

    elif data.startswith("rejectinvite_"):
        transfer_id = int(data.split("_")[1])

        db.execute(
            "UPDATE transfers SET status='rejected' "
            "WHERE id=? AND player_id=? AND status='invited'",
            (transfer_id, user_id)
        )
        db.commit()

        await query.edit_message_text("❌ Ты отказался от приглашения.")

    elif data.startswith("approve_") or data.startswith("reject_"):
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Нет доступа.")
            return

        transfer_id = int(data.split("_")[1])

        transfer = db.execute(
            "SELECT player_id, old_club, new_club, status "
            "FROM transfers WHERE id=?",
            (transfer_id,)
        ).fetchone()

        if not transfer or transfer[3] != "pending":
            await query.edit_message_text("Заявка уже обработана.")
            return

        player_id, old_club, new_club, status = transfer

        if data.startswith("approve_"):
            db.execute(
                "UPDATE players SET club=? WHERE user_id=?",
                (new_club, player_id)
            )
            db.execute(
                "UPDATE transfers SET status='approved' WHERE id=?",
                (transfer_id,)
            )

            await context.bot.send_message(
                player_id,
                f"🎉 Трансфер одобрен!\nТеперь твой клуб: {new_club}"
            )

            await query.edit_message_text("✅ Трансфер одобрен.")

        else:
            db.execute(
                "UPDATE transfers SET status='rejected' WHERE id=?",
                (transfer_id,)
            )

            await context.bot.send_message(
                player_id,
                "❌ Администраторы отклонили трансфер."
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
            "SELECT user_id, club FROM players WHERE user_id=?",
            (int(target),)
        ).fetchone()
    else:
        player = db.execute(
            "SELECT user_id, club FROM players "
            "WHERE LOWER(username)=LOWER(?) "
            "OR LOWER(nickname)=LOWER(?)",
            (target, target)
        ).fetchone()

    if not player:
        await update.message.reply_text(
            "❌ Игрок не найден. Он должен запустить бота через /start."
        )
        return

    player_id, old_club = player

    new_club = db.execute(
        "SELECT name FROM clubs WHERE owner_id=?",
        (update.effective_user.id,)
    ).fetchone()

    if not new_club:
        await update.message.reply_text("❌ У тебя нет клуба.")
        return

    if player_id == update.effective_user.id:
        await update.message.reply_text("❌ Нельзя пригласить себя.")
        return

    if old_club == new_club[0]:
        await update.message.reply_text("❌ Игрок уже в твоём клубе.")
        return

    pending = db.execute(
        "SELECT id FROM transfers WHERE player_id=? "
        "AND status IN ('invited', 'pending')",
        (player_id,)
    ).fetchone()

    if pending:
        await update.message.reply_text(
            "❌ У игрока уже есть активная заявка."
        )
        return

    cursor = db.execute(
        "INSERT INTO transfers "
        "(player_id, old_club, new_club, status) "
        "VALUES (?, ?, ?, 'invited')",
        (player_id, old_club, new_club[0])
    )
    db.commit()

    transfer_id = cursor.lastrowid

    keyboard = [[
        InlineKeyboardButton(
            "✅ Согласиться",
            callback_data=f"accept_{transfer_id}"
        ),
        InlineKeyboardButton(
            "❌ Отказаться",
            callback_data=f"rejectinvite_{transfer_id}"
        )
    ]]

    try:
        await context.bot.send_message(
            player_id,
            f"📨 ТЕБЯ ПРИГЛАШАЮТ В КЛУБ!\n\n"
            f"Текущий клуб: {old_club or 'Свободный агент'}\n"
            f"Новый клуб: {new_club[0]}\n\n"
            "Согласен перейти?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        db.execute(
            "UPDATE transfers SET status='rejected' WHERE id=?",
            (transfer_id,)
        )
        db.commit()

        await update.message.reply_text(
            "❌ Не удалось отправить приглашение. "
            "Игрок должен открыть бота и написать /start."
        )
        return

    await update.message.reply_text("✅ Приглашение отправлено!")


async def addclub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /addclub Название"
        )
        return

    name = " ".join(context.args).strip()

    try:
        db.execute(
            "INSERT INTO clubs (name, owner_id) VALUES (?, NULL)",
            (name,)
        )
        db.commit()

        await update.message.reply_text(
            f"✅ Клуб {name} добавлен."
        )

    except sqlite3.IntegrityError:
        await update.message.reply_text(
            "❌ Такой клуб уже существует."
        )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"TouchHockey bot is running")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )
    server.serve_forever()


def main():
    if not TOKEN:
        raise RuntimeError("Добавь BOT_TOKEN в Environment Variables.")

    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nickname", nickname))
    app.add_handler(CommandHandler("addclub", addclub))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    print("TouchHockey bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
