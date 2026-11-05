import os
import re
import json
import time
import sqlite3
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    PreCheckoutQueryHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
ROOT_ADMIN = 8695966053
CHANNEL = "@transfermarketth"
DB_FILE = "touchhockey.db"

CLUBS = [
    ("Лада 🦅",), ("Динамо Минск 🇧🇾",), ("Автомобилист 🚘",),
    ("Ак Барс 🐱",), ("Металлург 🦊",), ("ХК Сочи 🐆",),
    ("Авангард 🦅",), ("Барыс 🇰🇿",), ("Сибирь ❄️",),
    ("Салават Юлаев 🟢",)
]

COOLDOWNS = {
    "search": 24 * 60 * 60,
    "take": 24 * 60 * 60,
    "nickname": 7 * 24 * 60 * 60,
    "position": 4 * 24 * 60 * 60,
}
PRODUCTS = {
    "search": (25, "Снять КД на поиск клуба"),
    "take": (50, "Снять КД на получение клуба"),
    "position": (15, "Снять КД на смену позиции"),
    "nickname": (25, "Снять КД на смену ника"),
}

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.RLock()


def init_db():
    with lock:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            nickname TEXT,
            club TEXT,
            position TEXT,
            nick_changed_at REAL DEFAULT 0,
            pos_changed_at REAL DEFAULT 0,
            search_used_at REAL DEFAULT 0,
            take_used_at REAL DEFAULT 0,
            career_status TEXT DEFAULT 'active',
            registration_status TEXT DEFAULT 'unregistered',
            roblox_username TEXT,
            roblox_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS clubs (
            name TEXT PRIMARY KEY,
            owner_id INTEGER,
            closed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            old_club TEXT,
            new_club TEXT,
            status TEXT
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT,
            payload TEXT,
            status TEXT DEFAULT 'pending',
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        # Safe migrations for databases created by the earlier bot version.
        for table, column, definition in [
            ("players", "position", "TEXT"),
            ("players", "nick_changed_at", "REAL DEFAULT 0"),
            ("players", "pos_changed_at", "REAL DEFAULT 0"),
            ("players", "search_used_at", "REAL DEFAULT 0"),
            ("players", "take_used_at", "REAL DEFAULT 0"),
            ("players", "career_status", "TEXT DEFAULT 'active'"),
            ("players", "registration_status", "TEXT DEFAULT 'unregistered'"),
            ("players", "roblox_username", "TEXT"),
            ("players", "roblox_id", "INTEGER"),
            ("clubs", "closed", "INTEGER DEFAULT 0"),
        ]:
            columns = [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        for (name,) in CLUBS:
            db.execute(
                "INSERT OR IGNORE INTO clubs(name, owner_id, closed) VALUES (?, NULL, 0)",
                (name,)
            )
        db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (ROOT_ADMIN,))
        db.commit()


def is_admin(user_id):
    if user_id == ROOT_ADMIN:
        return True
    with lock:
        return db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def is_root(user_id):
    return user_id == ROOT_ADMIN


def get_setting(key):
    with lock:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with lock:
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )
        db.commit()


def get_player(user_id):
    with lock:
        return db.execute("SELECT * FROM players WHERE user_id=?", (user_id,)).fetchone()


def ensure_player(user):
    with lock:
        db.execute("""
            INSERT OR IGNORE INTO players(user_id, username, nickname, club, position)
            VALUES (?, ?, ?, NULL, NULL)
        """, (user.id, user.username, user.first_name or "Игрок"))
        db.execute(
            "UPDATE players SET username=? WHERE user_id=?",
            (user.username, user.id)
        )
        db.commit()


def is_registered(user_id):
    if is_admin(user_id):
        return True
    p = get_player(user_id)
    return bool(p and p["registration_status"] == "approved")


def is_club_owner(user_id):
    with lock:
        return db.execute("SELECT 1 FROM clubs WHERE owner_id=? LIMIT 1", (user_id,)).fetchone() is not None


def lookup_roblox_user(username):
    """Resolve a Roblox username through Roblox's public users API."""
    body = json.dumps({"usernames": [username], "excludeBannedUsers": False}).encode("utf-8")
    request = urllib.request.Request(
        "https://users.roblox.com/v1/usernames/users", data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read().decode("utf-8"))
        users = result.get("data", [])
        return users[0] if users else None
    except Exception:
        return None


def username_text(user):
    return f"@{user.username}" if user.username else f"ID: {user.id}"


def main_menu(user_id):
    # Показываем управление только фактическому владельцу клуба.
    with lock:
        owns_club = db.execute(
            "SELECT 1 FROM clubs WHERE owner_id=? LIMIT 1", (user_id,)
        ).fetchone() is not None

    rows = [
        [InlineKeyboardButton("🏒 Взять клуб", callback_data="clubs")],
        [InlineKeyboardButton("🔎 Искать клуб", callback_data="search")],
        [InlineKeyboardButton("📢 Объявления", callback_data="ads")],
    ]
    if owns_club:
        rows.append([InlineKeyboardButton("⚙️ Управление клубом", callback_data="management")])
    rows.extend([
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton("⭐ Донат", callback_data="donate")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("👑 Админ-панель", callback_data="admin")],
    ])
    return InlineKeyboardMarkup(rows)


async def send_to_admin_group(context, text, keyboard):
    group_id = get_setting("admin_group_id")
    if not group_id:
        return False
    try:
        await context.bot.send_message(
            chat_id=int(group_id), text=text, reply_markup=keyboard
        )
        return True
    except Exception:
        return False


async def create_request(context, user_id, kind, payload, text, label="Рассмотреть"):
    with lock:
        cur = db.execute(
            "INSERT INTO requests(user_id,kind,payload,status,created_at) "
            "VALUES(?,?,?,'pending',?)",
            (user_id, kind, json.dumps(payload, ensure_ascii=False), time.time())
        )
        db.commit()
        request_id = cur.lastrowid

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"mod_yes_{request_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"mod_no_{request_id}")
    ]])
    sent = await send_to_admin_group(
        context,
        f"📥 НОВАЯ ЗАЯВКА #{request_id}\n\n{text}\n\nОтправитель ID: {user_id}",
        keyboard
    )
    if not sent:
        with lock:
            db.execute("UPDATE requests SET status='no_group' WHERE id=?", (request_id,))
            db.commit()
        return None
    return request_id


def user_cooldown_remaining(player, field, duration):
    used = player[field] or 0
    return max(0, int(duration - (time.time() - used)))


def fmt_time(seconds):
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} д. {hours} ч."
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_player(update.effective_user)
    uid = update.effective_user.id
    if not is_registered(uid):
        p = get_player(uid)
        if p["registration_status"] == "pending":
            await update.message.reply_text("⏳ Твоя регистрация Roblox уже отправлена администраторам. Дождись проверки.")
        else:
            context.user_data["flow"] = "roblox_registration"
            await update.message.reply_text(
                "📝 РЕГИСТРАЦИЯ\n\n"
                "Перед использованием бота нужно подтвердить Roblox-аккаунт.\n"
                "Отправь свой точный Roblox username (не отображаемое имя)."
            )
        return
    await update.message.reply_text(
        "🏒 TOUCH HOCKEY TM\n\nДобро пожаловать!\n"
        "Изменить ник и позицию можно в разделе «Настройки».",
        reply_markup=main_menu(uid)
    )


async def nickname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Измени ник через меню ⚙️ Настройки.")


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Команду /setgroup нужно отправить в группе администраторов.")
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    set_setting("admin_group_id", update.effective_chat.id)
    await update.message.reply_text("✅ Группа для модерации подключена.")


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    ensure_player(q.from_user)
    player = get_player(uid)
    if not is_registered(uid) and not data.startswith("mod_"):
        await q.edit_message_text("⛔ Сначала пройди регистрацию Roblox. Напиши /start.")
        return

    if data == "home":
        await q.edit_message_text("🏒 Главное меню", reply_markup=main_menu(uid))

    elif data == "clubs":
        with lock:
            rows = db.execute(
                "SELECT name,owner_id,closed FROM clubs ORDER BY rowid"
            ).fetchall()
        text = (
            "📌 Официальный реестр клубов\n\n"
            "📥 Количество клубов – 10\n"
            "🔎 Регистрация клуба – @chibixov\n\n"
            "🏒 Клубы 🏒\n\n"
        )
        keyboard = []
        for row in rows:
            status = "🔒" if row["owner_id"] or row["closed"] else "🟢"
            text += f"{status} {row['name']}\n"
            if not row["owner_id"] and not row["closed"]:
                keyboard.append([InlineKeyboardButton(
                    row["name"], callback_data="take_" + str(row["name"])
                )])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("take_"):
        club = data[5:]
        p = get_player(uid)
        if not p:
            await q.edit_message_text("Сначала напиши /start.")
            return
        if p["club"]:
            await q.edit_message_text("❌ У тебя уже есть клуб.")
            return
        remaining = user_cooldown_remaining(p, "take_used_at", COOLDOWNS["take"])
        if remaining:
            await q.edit_message_text(
                f"⏳ Получить клуб снова можно через {fmt_time(remaining)}."
            )
            return
        with lock:
            c = db.execute("SELECT owner_id,closed FROM clubs WHERE name=?", (club,)).fetchone()
        if not c or c["owner_id"] or c["closed"]:
            await q.edit_message_text("❌ Этот клуб уже занят или закрыт.")
            return
        rid = await create_request(
            context, uid, "take_club", {"club": club},
            f"🏒 ЗАЯВКА НА КЛУБ\nКлуб: {club}\nИгрок: {p['nickname']}\n"
            f"Пользователь: {username_text(q.from_user)}"
        )
        if rid:
            await q.edit_message_text("✅ Заявка отправлена в группу администраторов.")
        else:
            await q.edit_message_text("❌ Не удалось отправить заявку. Проверь подключение группы через /setgroup.")

    elif data == "search":
        p = get_player(uid)
        remaining = user_cooldown_remaining(p, "search_used_at", COOLDOWNS["search"])
        if remaining:
            await q.edit_message_text(f"⏳ Следующую анкету можно отправить через {fmt_time(remaining)}.")
            return
        context.user_data["flow"] = "free_agent"
        nick = p["nickname"] or "Ваш ник"
        handle = username_text(q.from_user)
        await q.edit_message_text(
            "🔎 ИСКАТЬ КЛУБ\n\n"
            "Напиши дополнительный текст о себе. Можешь использовать шаблон:\n\n"
            f"🏒 Свободный агент\nНик в боте: {nick}\n"
            f"Статус: Ищет клуб!\n📩: {handle}\n\nP.S.: Ваш текст\n\n"
            "Твой текст будет отправлен администраторам на проверку."
        )

    elif data == "ads":
        await q.edit_message_text(
            "📢 ОБЪЯВЛЕНИЯ\n\nВыбери тип объявления:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📣 Реклама ТХ канала", callback_data="ad_channel")],
                [InlineKeyboardButton("🏒 Поиск игроков", callback_data="ad_players")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
            ])
        )

    elif data == "ad_channel":
        context.user_data["flow"] = "channel_ad"
        await q.edit_message_text(
            "📣 РЕКЛАМА ТХ КАНАЛА\n\nОтправь рекламный текст. "
            "В нём обязательно должна быть ссылка на твой Telegram-канал."
        )

    elif data == "ad_players":
        owner_club = db.execute("SELECT name FROM clubs WHERE owner_id=?", (uid,)).fetchone()
        if not owner_club:
            await q.edit_message_text("❌ Создавать объявление «Поиск игроков» могут только владельцы клубов.")
            return
        context.user_data["flow"] = "player_ad"
        await q.edit_message_text(
            f"🏒 ПОИСК ИГРОКОВ\n\nОтправь текст объявления. "
            f"Бот добавит в начало название клуба «{owner_club['name']}», "
            f"а в конец — твой контакт {username_text(q.from_user)}."
        )

    elif data == "management":
        with lock:
            club = db.execute("SELECT name,closed FROM clubs WHERE owner_id=?", (uid,)).fetchone()
        if not club:
            await q.edit_message_text("❌ Ты не являешься владельцем клуба.")
            return
        await q.edit_message_text(
            f"⚙️ УПРАВЛЕНИЕ КЛУБОМ\n\n🏒 {club['name']}\n"
            f"Статус: {'Закрыт' if club['closed'] else 'Открыт'}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Состав клуба", callback_data="squad")],
                [InlineKeyboardButton("🔄 Трансферы", callback_data="transfer")],
                [InlineKeyboardButton("🚪 Выгнать игрока", callback_data="kick")],
                [InlineKeyboardButton("🔒 Закрыть клуб", callback_data="owner_close")],
                [InlineKeyboardButton("👑 Передать права", callback_data="owner_transfer")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
            ])
        )

    elif data == "squad":
        with lock:
            club = db.execute("SELECT name FROM clubs WHERE owner_id=?", (uid,)).fetchone()
            members = db.execute(
                "SELECT nickname,position,user_id FROM players WHERE club=? ORDER BY nickname",
                (club["name"],)
            ).fetchall() if club else []
        if not club:
            await q.edit_message_text("❌ Клуб не найден.")
            return
        lines = [f"👥 СОСТАВ {club['name']}\n"]
        for m in members:
            lines.append(f"• {m['nickname']} — {m['position'] or 'позиция не указана'} (ID: {m['user_id']})")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="management")]])
        )

    elif data == "transfer":
        context.user_data["flow"] = "transfer_target"
        await q.edit_message_text("🔄 Отправь Telegram ID, @username или игровой ник игрока.")

    elif data == "kick":
        context.user_data["flow"] = "kick_target"
        await q.edit_message_text("🚪 Отправь Telegram ID, @username или игровой ник игрока, которого хочешь исключить.")

    elif data == "owner_close":
        with lock:
            club = db.execute("SELECT name FROM clubs WHERE owner_id=?", (uid,)).fetchone()
            if club:
                db.execute("UPDATE clubs SET closed=1 WHERE owner_id=?", (uid,))
                db.commit()
        await q.edit_message_text(
            "🔒 Клуб закрыт для новых заявок.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="management")]])
        )

    elif data == "owner_transfer":
        context.user_data["flow"] = "owner_transfer_target"
        await q.edit_message_text("👑 Отправь Telegram ID, @username или игровой ник нового владельца.")

    elif data == "settings":
        await q.edit_message_text(
            "⚙️ НАСТРОЙКИ\n\nВыбери, что изменить:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить ник", callback_data="change_nick")],
                [InlineKeyboardButton("🏒 Изменить позицию", callback_data="change_position")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
            ])
        )

    elif data == "change_nick":
        p = get_player(uid)
        remaining = user_cooldown_remaining(p, "nick_changed_at", COOLDOWNS["nickname"])
        if remaining:
            await q.edit_message_text(f"⏳ Смена ника доступна через {fmt_time(remaining)}.")
            return
        context.user_data["flow"] = "nickname"
        await q.edit_message_text("✏️ Отправь новый игровой ник (до 30 символов).")

    elif data == "change_position":
        p = get_player(uid)
        remaining = user_cooldown_remaining(p, "pos_changed_at", COOLDOWNS["position"])
        if remaining:
            await q.edit_message_text(f"⏳ Смена позиции доступна через {fmt_time(remaining)}.")
            return
        await q.edit_message_text(
            "🏒 Выбери позицию:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Нападающий", callback_data="pos_Нападающий")],
                [InlineKeyboardButton("Полузащитник", callback_data="pos_Полузащитник")],
                [InlineKeyboardButton("Вратарь", callback_data="pos_Вратарь")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="settings")]
            ])
        )

    elif data.startswith("pos_"):
        pos = data[4:]
        if pos not in ("Нападающий", "Полузащитник", "Вратарь"):
            return
        p = get_player(uid)
        remaining = user_cooldown_remaining(p, "pos_changed_at", COOLDOWNS["position"])
        if remaining:
            await q.edit_message_text(f"⏳ Смена позиции доступна через {fmt_time(remaining)}.")
            return
        with lock:
            db.execute("UPDATE players SET position=?,pos_changed_at=? WHERE user_id=?", (pos, time.time(), uid))
            db.commit()
        await q.edit_message_text(f"✅ Твоя позиция: {pos}")

    elif data == "profile":
        p = get_player(uid)
        career = p["career_status"] or "active"
        career_label = "Завершена" if career == "retired" else "Активна"
        career_button = ("↩️ Вернуться в карьеру" if career == "retired"
                         else "🏁 Завершить карьеру")
        career_callback = "career_return_confirm" if career == "retired" else "career_retire_confirm"
        await q.edit_message_text(
            f"👤 ПРОФИЛЬ\n\nНик: {p['nickname'] or 'Не указан'}\n"
            f"Контакт: {username_text(q.from_user)}\n"
            f"Позиция: {p['position'] or 'Не указана'}\n"
            f"Клуб: {p['club'] or 'Свободный агент'}\n"
            f"Карьера: {career_label}\n"
            f"Roblox: {p['roblox_username'] or 'Не привязан'}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(career_button, callback_data=career_callback)],
                [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
            ])
        )

    elif data == "career_retire_confirm":
        await q.edit_message_text(
            "🏁 Завершить карьеру?\n\nТвой профиль останется в боте, но статус станет «Завершена».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, завершить", callback_data="career_retire")],
                [InlineKeyboardButton("↩️ Отмена", callback_data="profile")]
            ])
        )

    elif data == "career_return_confirm":
        await q.edit_message_text(
            "↩️ Вернуться в карьеру?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, вернуться", callback_data="career_return")],
                [InlineKeyboardButton("↩️ Отмена", callback_data="profile")]
            ])
        )

    elif data == "career_retire":
        with lock:
            db.execute("UPDATE players SET career_status='retired' WHERE user_id=?", (uid,))
            db.commit()
        await q.edit_message_text("🏁 Карьера завершена.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 Профиль", callback_data="profile")]]))

    elif data == "career_return":
        with lock:
            db.execute("UPDATE players SET career_status='active' WHERE user_id=?", (uid,))
            db.commit()
        await q.edit_message_text("✅ Ты вернулся в карьеру!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 Профиль", callback_data="profile")]]))

    elif data == "donate":
        keyboard = [
            [InlineKeyboardButton(f"🔎 Снять КД на поиск клуба — {PRODUCTS['search'][0]} ⭐", callback_data="buy_search")],
            [InlineKeyboardButton(f"🏒 Снять КД на получение клуба — {PRODUCTS['take'][0]} ⭐", callback_data="buy_take")],
            [InlineKeyboardButton(f"🏒 Снять КД на смену позиции — {PRODUCTS['position'][0]} ⭐", callback_data="buy_position")],
            [InlineKeyboardButton(f"✏️ Снять КД на смену ника — {PRODUCTS['nickname'][0]} ⭐", callback_data="buy_nickname")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
        ]
        await q.edit_message_text("⭐ DONATE\n\nВыбери услугу:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("buy_"):
        product = data[4:]
        if product not in PRODUCTS:
            return
        amount, title = PRODUCTS[product]
        await context.bot.send_invoice(
            chat_id=uid,
            title=title,
            description="Однократное снятие текущего ограничения по времени.",
            payload="cooldown:" + product,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(title, amount)]
        )

    elif data == "admin":
        if not is_admin(uid):
            await q.edit_message_text("⛔ Нет доступа.")
            return
        rows = [
            [InlineKeyboardButton("➕ Выдать админку", callback_data="admin_add")],
            [InlineKeyboardButton("➖ Снять админку", callback_data="admin_remove")],
            [InlineKeyboardButton("🔒 Закрыть клуб", callback_data="admin_close")],
            [InlineKeyboardButton("🔓 Освободить клуб", callback_data="admin_release")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
        ]
        if not is_root(uid):
            rows = rows[2:]
            rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
        await q.edit_message_text("👑 АДМИН-ПАНЕЛЬ", reply_markup=InlineKeyboardMarkup(rows))

    elif data == "admin_add":
        if not is_root(uid):
            await q.edit_message_text("⛔ Только главный администратор.")
            return
        context.user_data["flow"] = "admin_add"
        await q.edit_message_text("Отправь Telegram ID пользователя, которому выдать админку.")

    elif data == "admin_remove":
        if not is_root(uid):
            await q.edit_message_text("⛔ Только главный администратор.")
            return
        context.user_data["flow"] = "admin_remove"
        await q.edit_message_text("Отправь Telegram ID администратора, которого нужно удалить.")

    elif data in ("admin_close", "admin_release"):
        if not is_admin(uid):
            await q.edit_message_text("⛔ Нет доступа.")
            return
        context.user_data["flow"] = data
        await q.edit_message_text("Отправь точное название клуба из списка.")

    elif data.startswith("mod_yes_") or data.startswith("mod_no_"):
        if not is_admin(uid):
            await q.answer("Нет доступа.", show_alert=True)
            return
        approve = data.startswith("mod_yes_")
        request_id = int(data.rsplit("_", 1)[1])
        await moderate_request(q, context, request_id, approve)

    elif data.startswith("transfer_accept_") or data.startswith("transfer_reject_"):
        accept = data.startswith("transfer_accept_")
        transfer_id = int(data.rsplit("_", 1)[1])
        with lock:
            tr = db.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not tr or tr["player_id"] != uid or tr["status"] != "invited":
                await q.edit_message_text("❌ Предложение недействительно.")
                return
            db.execute("UPDATE transfers SET status=? WHERE id=?", ("accepted" if accept else "rejected", transfer_id))
            db.commit()
        if not accept:
            await q.edit_message_text("❌ Ты отклонил предложение.")
            return
        rid = await create_request(
            context, uid, "transfer",
            {"transfer_id": transfer_id},
            f"🔄 ПОДТВЕРЖДЁННЫЙ ТРАНСФЕР\nИгрок ID: {uid}\n"
            f"Из клуба: {tr['old_club'] or 'Свободный агент'}\nВ клуб: {tr['new_club']}"
        )
        await q.edit_message_text(
            "✅ Ты согласился. Заявка отправлена администраторам." if rid
            else "❌ Не удалось отправить заявку администраторам."
        )

    elif data.startswith("owner_accept_") or data.startswith("owner_reject_"):
        accept = data.startswith("owner_accept_")
        req_id = int(data.rsplit("_", 1)[1])
        with lock:
            req = db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
            if not req or req["user_id"] != uid or req["kind"] != "owner_change" or req["status"] != "waiting_user":
                await q.edit_message_text("❌ Запрос недействителен.")
                return
            payload = json.loads(req["payload"])
            db.execute("UPDATE requests SET status=? WHERE id=?", ("user_accepted" if accept else "rejected", req_id))
            db.commit()
        if accept:
            new_rid = await create_request(
                context, uid, "owner_change_admin",
                {"club": payload["club"], "old_owner": payload["old_owner"], "new_owner": uid},
                f"👑 ПЕРЕДАЧА ПРАВ НА КЛУБ\nКлуб: {payload['club']}\n"
                f"Старый владелец ID: {payload['old_owner']}\nНовый владелец ID: {uid}"
            )
            await q.edit_message_text("✅ Согласие отправлено администраторам." if new_rid else "❌ Не удалось отправить заявку.")
        else:
            await q.edit_message_text("❌ Ты отказался от передачи клуба.")


async def moderate_request(q, context, request_id, approve):
    with lock:
        req = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not req or req["status"] != "pending":
            await q.edit_message_text("Заявка уже обработана.")
            return
        payload = json.loads(req["payload"])
        kind = req["kind"]
        user_id = req["user_id"]

        if approve and kind == "roblox_registration":
            db.execute(
                "UPDATE players SET registration_status='approved',roblox_username=?,roblox_id=? WHERE user_id=?",
                (payload.get("roblox_username"), payload.get("roblox_id"), user_id)
            )
        elif not approve and kind == "roblox_registration":
            db.execute("UPDATE players SET registration_status='rejected' WHERE user_id=?", (user_id,))

        elif approve and kind == "take_club":
            club = payload["club"]
            c = db.execute("SELECT owner_id,closed FROM clubs WHERE name=?", (club,)).fetchone()
            p = db.execute("SELECT club FROM players WHERE user_id=?", (user_id,)).fetchone()
            if not c or c["owner_id"] is not None or c["closed"] or not p or p["club"]:
                db.execute("UPDATE requests SET status='rejected' WHERE id=?", (request_id,))
                db.commit()
                await q.edit_message_text("❌ Клуб уже занят/закрыт или игрок уже состоит в клубе.")
                return
            updated = db.execute(
                "UPDATE clubs SET owner_id=? WHERE name=? AND owner_id IS NULL AND closed=0",
                (user_id, club)
            )
            if updated.rowcount != 1:
                db.execute("UPDATE requests SET status='rejected' WHERE id=?", (request_id,))
                db.commit()
                await q.edit_message_text("❌ Этот клуб уже успели занять или закрыть.")
                return
            db.execute("UPDATE players SET club=?,take_used_at=? WHERE user_id=?", (club, time.time(), user_id))
            owner = db.execute(
                "SELECT nickname, username FROM players WHERE user_id=?", (user_id,)
            ).fetchone()
            owner_handle = "@" + owner["username"] if owner and owner["username"] else f"ID: {user_id}"
            publish_text = f"✅ Клуб «{club}» взят игроком {owner_handle}"

        elif approve and kind == "free_agent":
            p = db.execute("SELECT nickname,username FROM players WHERE user_id=?", (user_id,)).fetchone()
            nick = p["nickname"] or "Не указан"
            handle = "@" + p["username"] if p and p["username"] else f"ID: {user_id}"
            post = (
                "🏒 Свободный агент\n\n"
                f"Ник в боте: {nick}\n"
                "Статус: Ищет клуб!\n"
                f"📩: {handle}\n\n"
                f"P.S.: {payload['text']}"
            )
            publish_text = post

        elif approve and kind in ("channel_ad", "player_ad"):
            publish_text = payload["text"]

        elif approve and kind == "transfer":
            tr = db.execute("SELECT * FROM transfers WHERE id=?", (payload["transfer_id"],)).fetchone()
            if not tr or tr["status"] != "accepted":
                db.execute("UPDATE requests SET status='rejected' WHERE id=?", (request_id,))
                db.commit()
                await q.edit_message_text("❌ Трансфер уже недействителен.")
                return
            new_club = tr["new_club"]
            old_club = tr["old_club"]
            target = db.execute("SELECT owner_id,closed FROM clubs WHERE name=?", (new_club,)).fetchone()
            if not target or target["closed"]:
                db.execute("UPDATE requests SET status='rejected' WHERE id=?", (request_id,))
                db.execute("UPDATE transfers SET status='rejected' WHERE id=?", (payload["transfer_id"],))
                db.commit()
                await q.edit_message_text("❌ Новый клуб закрыт или не найден.")
                return
            db.execute("UPDATE players SET club=? WHERE user_id=?", (new_club, user_id))
            db.execute("UPDATE transfers SET status='approved' WHERE id=?", (payload["transfer_id"],))
            player_info = db.execute(
                "SELECT nickname FROM players WHERE user_id=?", (user_id,)
            ).fetchone()
            player_nick = player_info["nickname"] if player_info and player_info["nickname"] else f"ID {user_id}"
            previous_club = old_club or "Свободный агент"
            publish_text = (
                "🏒 Переход в клуб:\n\n"
                f"🥅 {player_nick} — {previous_club} ➡️ {new_club}"
            )

        elif approve and kind == "kick":
            club = payload["club"]
            target_id = payload["target_id"]
            db.execute("UPDATE players SET club=NULL WHERE user_id=? AND club=?", (target_id, club))

        elif approve and kind == "owner_change_admin":
            club = payload["club"]
            old_owner = payload["old_owner"]
            new_owner = payload["new_owner"]
            c = db.execute("SELECT owner_id FROM clubs WHERE name=?", (club,)).fetchone()
            if not c or c["owner_id"] != old_owner:
                db.execute("UPDATE requests SET status='rejected' WHERE id=?", (request_id,))
                db.commit()
                await q.edit_message_text("❌ Владелец клуба уже изменился.")
                return
            db.execute("UPDATE clubs SET owner_id=? WHERE name=?", (new_owner, club))
            db.execute("UPDATE players SET club=? WHERE user_id=?", (club, new_owner))

        db.execute(
            "UPDATE requests SET status=? WHERE id=?",
            ("approved" if approve else "rejected", request_id)
        )
        db.commit()

    if approve and kind in ("take_club", "free_agent", "channel_ad", "player_ad", "transfer"):
        try:
            await context.bot.send_message(CHANNEL, publish_text)
        except Exception:
            with lock:
                db.execute("UPDATE requests SET status='publish_error' WHERE id=?", (request_id,))
                db.commit()
            await q.edit_message_text("⚠️ Заявка одобрена, но публикация не удалась. Проверь права бота в канале.")
            return

    try:
        if kind == "roblox_registration":
            message = "✅ Регистрация Roblox подтверждена! Напиши /start, чтобы открыть меню." if approve else "❌ Регистрация отклонена. Напиши /start и попробуй отправить Roblox username ещё раз."
        elif kind == "take_club":
            message = "🎉 Твоя заявка одобрена! Клуб закреплён за тобой." if approve else "❌ Заявка на клуб отклонена."
        elif kind == "transfer":
            message = "✅ Трансфер одобрен администраторами." if approve else "❌ Трансфер отклонён администраторами."
        elif kind == "free_agent":
            message = "✅ Твоя анкета опубликована в канале." if approve else "❌ Твоя анкета отклонена."
        elif kind in ("channel_ad", "player_ad"):
            message = "✅ Твоё объявление опубликовано." if approve else "❌ Твоё объявление отклонено."
        elif kind == "kick":
            message = "✅ Игрок исключён из клуба." if approve else "❌ Заявка на исключение отклонена."
        elif kind == "owner_change_admin":
            message = "✅ Передача прав одобрена." if approve else "❌ Передача прав отклонена."
        else:
            message = "Заявка обработана."
        await context.bot.send_message(user_id, message)
    except Exception:
        pass
    await q.edit_message_text(
        f"{'✅ Принято' if approve else '❌ Отклонено'} — заявка #{request_id}"
    )


async def find_target(text):
    raw = text.strip()
    if raw.startswith("@"):
        raw = raw[1:]
    with lock:
        if raw.isdigit():
            return db.execute("SELECT user_id,club,nickname FROM players WHERE user_id=?", (int(raw),)).fetchone()
        return db.execute(
            "SELECT user_id,club,nickname FROM players "
            "WHERE LOWER(username)=LOWER(?) OR LOWER(nickname)=LOWER(?) LIMIT 1",
            (raw, raw)
        ).fetchone()


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    if not flow:
        return
    text = (update.message.text or "").strip()
    uid = update.effective_user.id
    p = get_player(uid)

    if flow == "roblox_registration":
        username = text.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
            await update.message.reply_text("❌ Введи Roblox username: 3–20 символов, только латинские буквы, цифры и _. Попробуй ещё раз.")
            return
        await update.message.reply_text("🔎 Проверяю Roblox-профиль, подожди...")
        roblox_user = lookup_roblox_user(username)
        if not roblox_user:
            await update.message.reply_text("❌ Не удалось найти аккаунт Roblox или сервис временно недоступен. Проверь username и отправь его снова.")
            return
        rid = await create_request(
            context, uid, "roblox_registration",
            {"roblox_username": roblox_user.get("name", username), "roblox_id": roblox_user.get("id")},
            f"📝 РЕГИСТРАЦИЯ ROBLOX\nИгрок Telegram: {username_text(update.effective_user)}\n"
            f"Roblox username: {roblox_user.get('name', username)}\nRoblox ID: {roblox_user.get('id')}"
        )
        if rid:
            with lock:
                db.execute("UPDATE players SET registration_status='pending' WHERE user_id=?", (uid,))
                db.commit()
            context.user_data.pop("flow", None)
            await update.message.reply_text("✅ Roblox-профиль найден! Заявка отправлена администраторам. После одобрения откроется меню бота.")
        else:
            await update.message.reply_text("❌ Не удалось отправить заявку. Администраторская группа не подключена.")
    elif flow == "free_agent":
        context.user_data.pop("flow", None)
        if len(text) > 800:
            await update.message.reply_text("❌ Максимум 800 символов.")
            return
        rid = await create_request(
            context, uid, "free_agent", {"text": text},
            f"🔎 АНКЕТА СВОБОДНОГО АГЕНТА\nИгрок: {p['nickname']}\n"
            f"Контакт: {username_text(update.effective_user)}\nP.S.: {text}"
        )
        if rid:
            with lock:
                db.execute("UPDATE players SET search_used_at=? WHERE user_id=?", (time.time(), uid))
                db.commit()
            await update.message.reply_text("✅ Анкета отправлена на проверку.")
        else:
            await update.message.reply_text("❌ Не удалось отправить анкету. Проверь, подключена ли группа.")
    elif flow == "channel_ad":
        context.user_data.pop("flow", None)
        if not re.search(r"(https?://)?(www\.)?t\.me/[A-Za-z0-9_+/?=-]+", text):
            await update.message.reply_text("❌ В объявлении обязательно должна быть ссылка на твой Telegram-канал.")
            return
        if len(text) > 1000:
            await update.message.reply_text("❌ Максимум 1000 символов.")
            return
        rid = await create_request(context, uid, "channel_ad", {"text": text}, f"📣 РЕКЛАМА ТХ КАНАЛА\n{text}")
        await update.message.reply_text("✅ Отправлено на проверку." if rid else "❌ Не удалось отправить. Проверь группу.")
    elif flow == "player_ad":
        context.user_data.pop("flow", None)
        owner_club = db.execute("SELECT name FROM clubs WHERE owner_id=?", (uid,)).fetchone()
        if not owner_club:
            await update.message.reply_text("❌ Только владелец клуба может отправлять объявление «Поиск игроков».")
            return
        if len(text) > 800:
            await update.message.reply_text("❌ Максимум 800 символов.")
            return
        post = f"🏒 {owner_club['name']}\n\n{text}\n\n📩: {username_text(update.effective_user)}"
        rid = await create_request(context, uid, "player_ad", {"text": post}, f"🏒 ПОИСК ИГРОКОВ\n\n{post}")
        await update.message.reply_text("✅ Объявление отправлено на проверку." if rid else "❌ Не удалось отправить. Проверь группу.")
    elif flow == "nickname":
        context.user_data.pop("flow", None)
        if not 1 <= len(text) <= 30:
            await update.message.reply_text("❌ Ник должен содержать от 1 до 30 символов.")
            return
        remaining = user_cooldown_remaining(p, "nick_changed_at", COOLDOWNS["nickname"])
        if remaining:
            await update.message.reply_text(f"⏳ Смена ника доступна через {fmt_time(remaining)}.")
            return
        with lock:
            db.execute("UPDATE players SET nickname=?,nick_changed_at=? WHERE user_id=?", (text, time.time(), uid))
            db.commit()
        await update.message.reply_text(f"✅ Новый ник: {text}")
    elif flow == "transfer_target":
        context.user_data.pop("flow", None)
        club = db.execute("SELECT name FROM clubs WHERE owner_id=?", (uid,)).fetchone()
        target = await find_target(text)
        if not club:
            await update.message.reply_text("❌ Ты не владелец клуба.")
            return
        if not target:
            await update.message.reply_text("❌ Игрок не найден. Он должен запустить бота через /start.")
            return
        if target["user_id"] == uid or target["club"] == club["name"]:
            await update.message.reply_text("❌ Нельзя отправить такой трансфер.")
            return
        pending = db.execute(
            "SELECT id FROM transfers WHERE player_id=? AND status IN ('invited','accepted')",
            (target["user_id"],)
        ).fetchone()
        if pending:
            await update.message.reply_text("❌ У игрока уже есть активное предложение.")
            return
        cur = db.execute(
            "INSERT INTO transfers(player_id,old_club,new_club,status) VALUES(?,?,?,'invited')",
            (target["user_id"], target["club"], club["name"])
        )
        db.commit()
        tid = cur.lastrowid
        try:
            await context.bot.send_message(
                target["user_id"],
                f"🔄 ТРАНСФЕРНОЕ ПРЕДЛОЖЕНИЕ\n\n"
                f"Клуб: {club['name']}\nТекущий клуб: {target['club'] or 'Свободный агент'}\n\n"
                "Ты согласен перейти?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Принять", callback_data=f"transfer_accept_{tid}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_reject_{tid}")
                ]])
            )
            await update.message.reply_text("✅ Предложение отправлено игроку.")
        except Exception:
            db.execute("UPDATE transfers SET status='rejected' WHERE id=?", (tid,))
            db.commit()
            await update.message.reply_text("❌ Игрок должен сначала открыть бота через /start.")
    elif flow == "kick_target":
        context.user_data.pop("flow", None)
        club = db.execute("SELECT name FROM clubs WHERE owner_id=?", (uid,)).fetchone()
        target = await find_target(text)
        if not club or not target or target["club"] != club["name"] or target["user_id"] == uid:
            await update.message.reply_text("❌ Игрок не найден в твоём клубе.")
            return
        rid = await create_request(
            context, uid, "kick",
            {"club": club["name"], "target_id": target["user_id"]},
            f"🚪 ИСКЛЮЧЕНИЕ ИГРОКА\nКлуб: {club['name']}\nИгрок: {target['nickname']}\nID: {target['user_id']}"
        )
        await update.message.reply_text("✅ Заявка на исключение отправлена администраторам." if rid else "❌ Не удалось отправить заявку.")
    elif flow == "owner_transfer_target":
        context.user_data.pop("flow", None)
        club = db.execute("SELECT name FROM clubs WHERE owner_id=?", (uid,)).fetchone()
        target = await find_target(text)
        if not club or not target or target["user_id"] == uid or target["club"]:
            await update.message.reply_text("❌ Игрок не найден или уже состоит в клубе.")
            return
        with lock:
            cur = db.execute(
                "INSERT INTO requests(user_id,kind,payload,status,created_at) VALUES(?,?,?,?,?)",
                (target["user_id"], "owner_change", json.dumps({
                    "club": club["name"], "old_owner": uid
                }, ensure_ascii=False), "waiting_user", time.time())
            )
            db.commit()
            rid = cur.lastrowid
        try:
            await context.bot.send_message(
                target["user_id"],
                f"👑 Тебе предлагают стать владельцем клуба {club['name']}.\nСогласен?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Согласиться", callback_data=f"owner_accept_{rid}"),
                    InlineKeyboardButton("❌ Отказаться", callback_data=f"owner_reject_{rid}")
                ]])
            )
            await update.message.reply_text("✅ Запрос на передачу прав отправлен игроку.")
        except Exception:
            await update.message.reply_text("❌ Игрок должен сначала запустить бота через /start.")
    elif flow in ("admin_add", "admin_remove"):
        context.user_data.pop("flow", None)
        if not is_root(uid):
            await update.message.reply_text("⛔ Только главный администратор.")
            return
        if not text.isdigit():
            await update.message.reply_text("Отправь числовой Telegram ID.")
            return
        target_id = int(text)
        if target_id == ROOT_ADMIN:
            await update.message.reply_text("❌ Главного администратора нельзя удалить или повторно назначить.")
            return
        with lock:
            if flow == "admin_add":
                db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (target_id,))
            else:
                db.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
            db.commit()
        await update.message.reply_text("✅ Права администратора обновлены.")
    elif flow in ("admin_close", "admin_release"):
        context.user_data.pop("flow", None)
        if not is_admin(uid):
            await update.message.reply_text("⛔ Нет доступа.")
            return
        with lock:
            club = db.execute("SELECT name FROM clubs WHERE name=?", (text,)).fetchone()
            if not club:
                await update.message.reply_text("❌ Клуб не найден. Проверь точное название.")
                return
            if flow == "admin_close":
                db.execute("UPDATE clubs SET closed=1 WHERE name=?", (text,))
                result = "🔒 Клуб закрыт."
            else:
                db.execute("UPDATE players SET club=NULL WHERE club=?", (text,))
                db.execute("UPDATE clubs SET owner_id=NULL,closed=0 WHERE name=?", (text,))
                result = "🔓 Клуб освобождён. Все игроки удалены из его состава."
            db.commit()
        await update.message.reply_text(result)


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if not query.invoice_payload.startswith("cooldown:"):
        await query.answer(ok=False, error_message="Неизвестный товар.")
        return
    product = query.invoice_payload.split(":", 1)[1]
    if product not in PRODUCTS or query.currency != "XTR" or query.total_amount != PRODUCTS[product][0]:
        await query.answer(ok=False, error_message="Платёж не прошёл проверку.")
        return
    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    if not payment.invoice_payload.startswith("cooldown:"):
        return
    product = payment.invoice_payload.split(":", 1)[1]
    fields = {
        "search": "search_used_at",
        "take": "take_used_at",
        "position": "pos_changed_at",
        "nickname": "nick_changed_at"
    }
    if product not in fields:
        return
    with lock:
        db.execute(
            f"UPDATE players SET {fields[product]}=0 WHERE user_id=?",
            (update.effective_user.id,)
        )
        db.commit()
    await update.message.reply_text("⭐ Оплата получена! Ограничение по времени снято.")


async def addclub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /addclub Название")
        return
    name = " ".join(context.args).strip()
    with lock:
        try:
            db.execute("INSERT INTO clubs(name,owner_id,closed) VALUES(?,NULL,0)", (name,))
            db.commit()
            await update.message.reply_text(f"✅ Клуб {name} добавлен.")
        except sqlite3.IntegrityError:
            await update.message.reply_text("❌ Такой клуб уже существует.")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"TouchHockey bot is running")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def main():
    if not TOKEN:
        raise RuntimeError("Добавь BOT_TOKEN в Environment Variables.")
    init_db()
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nickname", nickname_command))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("addclub", addclub))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("TouchHockey TM started")
    app.run_polling()


if __name__ == "__main__":
    main()
