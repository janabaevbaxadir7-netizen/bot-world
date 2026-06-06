import os
import re
import io
import gc
import asyncio
import logging
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    from psycopg2cffi import compat
    compat.register()
    import psycopg2
    import psycopg2.extras
from contextlib import contextmanager
from dotenv import load_dotenv
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode

load_dotenv()
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

# ===================== DATABASE =====================
@contextmanager
def db():
    dsn = DATABASE_URL + ("&" if "?" in DATABASE_URL else "?") + "connect_timeout=10"
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def init_db():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
                language TEXT DEFAULT 'uz', is_subscribed BOOLEAN DEFAULT FALSE,
                subscription_price INTEGER DEFAULT 0, added_by BIGINT, added_by_username TEXT,
                joined_at TIMESTAMP DEFAULT NOW(), last_active TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS admins (
                admin_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
                added_by BIGINT, added_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY, admin_id BIGINT, admin_username TEXT, action TEXT,
                target_user_id BIGINT, target_username TEXT, details TEXT, created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS quizzes (
                id SERIAL PRIMARY KEY, user_id BIGINT, quiz_name TEXT, total_questions INTEGER,
                correct_answers INTEGER DEFAULT 0, wrong_answers INTEGER DEFAULT 0,
                started_at TIMESTAMP DEFAULT NOW(), finished_at TIMESTAMP, is_active BOOLEAN DEFAULT TRUE
            );
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id SERIAL PRIMARY KEY, quiz_session_id INTEGER REFERENCES quizzes(id),
                question_number INTEGER, question_text TEXT,
                option_a TEXT, option_b TEXT, option_c TEXT, option_d TEXT,
                correct_answer TEXT, user_answer TEXT, is_correct BOOLEAN, answered_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT NOW()
            );
            INSERT INTO bot_settings (key, value) VALUES ('subscription_price', '10000') ON CONFLICT (key) DO NOTHING;
            INSERT INTO bot_settings (key, value) VALUES ('admin_contact', '') ON CONFLICT (key) DO NOTHING;
        """)

def get_user(user_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        return cur.fetchone()

def add_user(user_id, username, full_name, language='uz'):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, username, full_name, language) VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username, full_name=EXCLUDED.full_name, last_active=NOW()
        """, (user_id, username, full_name, language))

def update_user_language(user_id, language):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET language=%s WHERE user_id=%s", (language, user_id))

def subscribe_user(user_id, added_by, added_by_username, price=None):
    if price is None:
        price = int(get_setting('subscription_price') or 10000)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_subscribed=TRUE, subscription_price=%s, added_by=%s, added_by_username=%s WHERE user_id=%s",
                    (price, added_by, added_by_username, user_id))

def unsubscribe_user(user_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_subscribed=FALSE WHERE user_id=%s", (user_id,))

def get_all_users(limit=100, offset=0):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, full_name, is_subscribed, joined_at FROM users ORDER BY joined_at DESC LIMIT %s OFFSET %s", (limit, offset))
        return cur.fetchall()

def get_subscribed_users():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, full_name, subscription_price FROM users WHERE is_subscribed=TRUE ORDER BY joined_at DESC")
        return cur.fetchall()

def get_users_count():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT COUNT(*) as total,
            COUNT(*) FILTER (WHERE is_subscribed=TRUE) as subscribed,
            COUNT(*) FILTER (WHERE DATE(joined_at)=CURRENT_DATE) as today FROM users""")
        return cur.fetchone()

def search_user(query):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id::text=%s OR username ILIKE %s OR full_name ILIKE %s LIMIT 10",
                    (query, f"%{query}%", f"%{query}%"))
        return cur.fetchall()

def db_is_admin(user_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admins WHERE admin_id=%s", (user_id,))
        return cur.fetchone() is not None

def add_admin(admin_id, username, full_name, added_by):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO admins (admin_id,username,full_name,added_by) VALUES(%s,%s,%s,%s) ON CONFLICT (admin_id) DO UPDATE SET username=EXCLUDED.username",
                    (admin_id, username, full_name, added_by))

def remove_admin(admin_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM admins WHERE admin_id=%s", (admin_id,))

def get_all_admins():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM admins ORDER BY added_at DESC")
        return cur.fetchall()

def log_action(admin_id, admin_username, action, target_id=None, target_username=None, details=None):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO admin_logs (admin_id,admin_username,action,target_user_id,target_username,details) VALUES(%s,%s,%s,%s,%s,%s)",
                    (admin_id, admin_username, action, target_id, target_username, details))

def get_logs(admin_id=None, limit=15):
    with db() as conn:
        cur = conn.cursor()
        if admin_id:
            cur.execute("SELECT * FROM admin_logs WHERE admin_id=%s ORDER BY created_at DESC LIMIT %s", (admin_id, limit))
        else:
            cur.execute("SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT %s", (limit,))
        return cur.fetchall()

def get_admin_stats(admin_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT COUNT(*) as total,
            COUNT(*) FILTER (WHERE action='subscribe') as subscribed_count,
            COUNT(*) FILTER (WHERE action='unsubscribe') as unsubscribed_count
            FROM admin_logs WHERE admin_id=%s""", (admin_id,))
        return cur.fetchone()

def create_quiz_session(user_id, quiz_name, total_questions):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO quizzes (user_id,quiz_name,total_questions) VALUES(%s,%s,%s) RETURNING id",
                    (user_id, quiz_name, total_questions))
        return cur.fetchone()['id']

def get_active_quiz(user_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM quizzes WHERE user_id=%s AND is_active=TRUE ORDER BY started_at DESC LIMIT 1", (user_id,))
        return cur.fetchone()

def save_question(session_id, q_num, question, a, b, c, d, correct):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO quiz_questions (quiz_session_id,question_number,question_text,option_a,option_b,option_c,option_d,correct_answer) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (session_id, q_num, question, a, b, c, d, correct))

def answer_question(session_id, q_num, user_answer):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT correct_answer FROM quiz_questions WHERE quiz_session_id=%s AND question_number=%s", (session_id, q_num))
        q = cur.fetchone()
        if not q:
            return None
        is_correct = user_answer.upper() == q['correct_answer'].upper()
        cur.execute("UPDATE quiz_questions SET user_answer=%s, is_correct=%s, answered_at=NOW() WHERE quiz_session_id=%s AND question_number=%s",
                    (user_answer, is_correct, session_id, q_num))
        if is_correct:
            cur.execute("UPDATE quizzes SET correct_answers=correct_answers+1 WHERE id=%s", (session_id,))
        else:
            cur.execute("UPDATE quizzes SET wrong_answers=wrong_answers+1 WHERE id=%s", (session_id,))
        return is_correct

def finish_quiz(session_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM quizzes WHERE id=%s", (session_id,))
        quiz = cur.fetchone()
        cur.execute("UPDATE quizzes SET is_active=FALSE, finished_at=NOW() WHERE id=%s", (session_id,))
        return quiz

def get_setting(key):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_settings WHERE key=%s", (key,))
        row = cur.fetchone()
        return row['value'] if row else None

def set_setting(key, value):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO bot_settings (key,value,updated_at) VALUES(%s,%s,NOW()) ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                    (key, value, value))

# ===================== PARSER =====================
def parse_docx(file_bytes):
    try:
        doc = Document(io.BytesIO(file_bytes))
        lines = [p.text.strip() for p in doc.paragraphs]
        del doc
        gc.collect()
        quiz_name = "Quiz"
        questions = []
        current_q = None
        for line in lines:
            if line.upper().startswith('# QUIZ:'):
                quiz_name = line[7:].strip()
                continue
            q_match = re.match(r'^Q(\d+)\s*[:.)]\s*(.+)', line, re.IGNORECASE)
            if q_match:
                if current_q and all(k in current_q for k in ['question','a','b','c','d','answer']):
                    questions.append(current_q)
                current_q = {'num': int(q_match.group(1)), 'question': q_match.group(2).strip()}
                continue
            if current_q:
                for letter in ['a','b','c','d']:
                    m = re.match(rf'^{letter.upper()}\s*[).]\s*(.+)', line, re.IGNORECASE)
                    if m:
                        current_q[letter] = m.group(1).strip()
                        break
                ans = re.match(r'^ANSWER\s*[:)]\s*([ABCD])', line, re.IGNORECASE)
                if ans:
                    current_q['answer'] = ans.group(1).upper()
        if current_q and all(k in current_q for k in ['question','a','b','c','d','answer']):
            questions.append(current_q)
        return {'name': quiz_name, 'questions': questions} if questions else None
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return None

# ===================== TEXTS =====================
TEXTS = {
    'uz': {
        'welcome': "👋 Assalomu alaykum, {name}!\n\n🧠 <b>QuizMasterUz</b> botiga xush kelibsiz!\n\n.docx fayl yuboring va quiz boshlang!\n\n/help — yordam",
        'no_sub': "🔒 Bu funksiya uchun <b>obuna</b> kerak.\n\n💰 Narx: <b>{price} so'm</b>\n\n📩 Bog'lanish: {contact}",
        'sub_ok': "✅ Obuna faol!\n💰 To'langan: <b>{price} so'm</b>\n👤 Qo'shgan: @{by}",
        'no_sub_status': "❌ Obuna yo'q.",
        'send_docx': "📄 .docx fayl yuboring.",
        'invalid_file': "❌ Fayl .docx bo'lishi kerak!",
        'parse_err': "❌ Fayl formati noto'g'ri. /help ga qarang.",
        'already_active': "⚠️ Faol quiz bor. /finish bilan to'xtating.",
        'quiz_started': "🎯 Quiz: <b>{name}</b>\n📊 Savollar: <b>{total}</b>",
        'quiz_stopped': "🛑 Quiz to'xtatildi.",
        'no_quiz': "❌ Faol quiz yo'q.",
        'correct': "✅ <b>To'g'ri!</b>",
        'wrong': "❌ <b>Noto'g'ri!</b> To'g'ri: <b>{ans}</b>",
        'help': """📖 <b>Yordam — QuizMasterUz</b>

Bu bot .docx formatidagi fayl asosida sizga quiz o'tkazib beradi.

━━━━━━━━━━━━━━━
📄 <b>Fayl formati:</b>
━━━━━━━━━━━━━━━
<code># QUIZ: Matematika

Q1: 2 × 5 = ?
A) 8
B) 10
C) 12
D) 15
ANSWER: B

Q2: O'zbekiston poytaxti?
A) Samarqand
B) Buxoro
C) Toshkent
D) Namangan
ANSWER: C</code>

━━━━━━━━━━━━━━━
🎮 <b>Buyruqlar:</b>
━━━━━━━━━━━━━━━
/start — Botni boshlash
/quiz — Quiz boshlash
/finish — Quizni to'xtatish
/restart — Qayta boshlash
/status — Obuna holati
/lang — Tilni o'zgartirish
/help — Yordam""",
        'menu_quiz': "📝 Quiz boshlash",
        'menu_status': "ℹ️ Holat",
        'menu_lang': "🌐 Til",
        'menu_help': "❓ Yordam",
        'menu_admin': "👑 Admin panel",
        'choose_lang': "🌐 Tilni tanlang:",
    },
    'ru': {
        'welcome': "👋 Привет, {name}!\n\n🧠 Добро пожаловать в <b>QuizMasterUz</b>!\n\nОтправьте .docx файл для квиза.\n\n/help — помощь",
        'no_sub': "🔒 Нужна <b>подписка</b>.\n\n💰 Цена: <b>{price} сум</b>\n\n📩 Связаться: {contact}",
        'sub_ok': "✅ Подписка активна!\n💰 Оплачено: <b>{price} сум</b>\n👤 Добавил: @{by}",
        'no_sub_status': "❌ Подписки нет.",
        'send_docx': "📄 Отправьте .docx файл.",
        'invalid_file': "❌ Файл должен быть .docx!",
        'parse_err': "❌ Неверный формат. Смотрите /help.",
        'already_active': "⚠️ Уже есть активный квиз. /finish для остановки.",
        'quiz_started': "🎯 Квиз: <b>{name}</b>\n📊 Вопросов: <b>{total}</b>",
        'quiz_stopped': "🛑 Квиз остановлен.",
        'no_quiz': "❌ Нет активного квиза.",
        'correct': "✅ <b>Правильно!</b>",
        'wrong': "❌ <b>Неправильно!</b> Ответ: <b>{ans}</b>",
        'help': """📖 <b>Помощь — QuizMasterUz</b>

Этот бот проводит квизы на основе .docx файлов.

━━━━━━━━━━━━━━━
📄 <b>Формат файла:</b>
━━━━━━━━━━━━━━━
<code># QUIZ: Математика

Q1: 2 × 5 = ?
A) 8
B) 10
C) 12
D) 15
ANSWER: B</code>

━━━━━━━━━━━━━━━
🎮 <b>Команды:</b>
━━━━━━━━━━━━━━━
/start — Запустить бота
/quiz — Начать квиз
/finish — Остановить квиз
/restart — Перезапустить
/status — Статус подписки
/lang — Сменить язык
/help — Помощь""",
        'menu_quiz': "📝 Начать квиз",
        'menu_status': "ℹ️ Статус",
        'menu_lang': "🌐 Язык",
        'menu_help': "❓ Помощь",
        'menu_admin': "👑 Admin panel",
        'choose_lang': "🌐 Выберите язык:",
    },
    'en': {
        'welcome': "👋 Hello, {name}!\n\n🧠 Welcome to <b>QuizMasterUz</b>!\n\nSend a .docx file to start a quiz.\n\n/help — help",
        'no_sub': "🔒 <b>Subscription</b> required.\n\n💰 Price: <b>{price} sum</b>\n\n📩 Contact: {contact}",
        'sub_ok': "✅ Subscription active!\n💰 Paid: <b>{price} sum</b>\n👤 Added by: @{by}",
        'no_sub_status': "❌ No subscription.",
        'send_docx': "📄 Send a .docx file.",
        'invalid_file': "❌ File must be .docx!",
        'parse_err': "❌ Invalid format. See /help.",
        'already_active': "⚠️ Quiz already active. Use /finish.",
        'quiz_started': "🎯 Quiz: <b>{name}</b>\n📊 Questions: <b>{total}</b>",
        'quiz_stopped': "🛑 Quiz stopped.",
        'no_quiz': "❌ No active quiz.",
        'correct': "✅ <b>Correct!</b>",
        'wrong': "❌ <b>Wrong!</b> Answer: <b>{ans}</b>",
        'help': """📖 <b>Help — QuizMasterUz</b>

This bot runs quizzes based on .docx files.

━━━━━━━━━━━━━━━
📄 <b>File format:</b>
━━━━━━━━━━━━━━━
<code># QUIZ: Math

Q1: 2 × 5 = ?
A) 8
B) 10
C) 12
D) 15
ANSWER: B</code>

━━━━━━━━━━━━━━━
🎮 <b>Commands:</b>
━━━━━━━━━━━━━━━
/start — Start bot
/quiz — Start quiz
/finish — Stop quiz
/restart — Restart quiz
/status — Subscription status
/lang — Change language
/help — Help""",
        'menu_quiz': "📝 Start quiz",
        'menu_status': "ℹ️ Status",
        'menu_lang': "🌐 Language",
        'menu_help': "❓ Help",
        'menu_admin': "👑 Admin panel",
        'choose_lang': "🌐 Choose language:",
    }
}

def t(user_id, key, **kw):
    u = get_user(user_id)
    lang = u['language'] if u and u.get('language') else 'uz'
    lang = lang if lang in TEXTS else 'uz'
    text = TEXTS[lang].get(key, TEXTS['uz'].get(key, key))
    return text.format(**kw) if kw else text

# ===================== KEYBOARDS =====================
def lang_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🇺🇿 O'zbek", callback_data="lang_uz"),
        InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    ]])

def answer_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🅐 A", callback_data="answer_A"),
        InlineKeyboardButton("🅑 B", callback_data="answer_B"),
        InlineKeyboardButton("🅒 C", callback_data="answer_C"),
        InlineKeyboardButton("🅓 D", callback_data="answer_D"),
    ]])

def menu_kb(user_id):
    is_adm = db_is_admin(user_id) or user_id == SUPER_ADMIN_ID
    rows = [
        [KeyboardButton(t(user_id, 'menu_quiz')), KeyboardButton(t(user_id, 'menu_status'))],
        [KeyboardButton(t(user_id, 'menu_lang')), KeyboardButton(t(user_id, 'menu_help'))],
    ]
    if is_adm:
        rows.append([KeyboardButton(t(user_id, 'menu_admin'))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def admin_kb(is_super=False):
    rows = [
        [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin_users"),
         InlineKeyboardButton("✅ Obunalilar", callback_data="admin_subscribers")],
        [InlineKeyboardButton("➕ Obunaga qo'shish", callback_data="admin_add_sub"),
         InlineKeyboardButton("➖ Obunadan olish", callback_data="admin_remove_sub")],
        [InlineKeyboardButton("🔍 Qidirish", callback_data="admin_search"),
         InlineKeyboardButton("📊 Statistika", callback_data="admin_stats")],
        [InlineKeyboardButton("📋 Loglar", callback_data="admin_logs")],
    ]
    if is_super:
        rows.append([InlineKeyboardButton("💰 Narx", callback_data="admin_set_price"),
                     InlineKeyboardButton("👮 Adminlar", callback_data="admin_manage_admins")])
        rows.append([InlineKeyboardButton("📢 Xabar yuborish", callback_data="admin_broadcast"),
                     InlineKeyboardButton("👤 Bog'lanish link", callback_data="admin_set_contact")])
    return InlineKeyboardMarkup(rows)

def back_kb(cb="admin_back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data=cb)]])

def user_action_kb(uid, is_sub):
    rows = []
    if is_sub:
        rows.append([InlineKeyboardButton("➖ Obunadan olish", callback_data=f"admin_unsub_{uid}")])
    else:
        rows.append([InlineKeyboardButton("➕ Obunaga qo'shish", callback_data=f"admin_sub_{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_users")])
    return InlineKeyboardMarkup(rows)

# ===================== STATE (in-memory, minimal) =====================
quiz_state = {}   # {user_id: {session_id, questions, current}}
admin_states = {} # {user_id: state_string}

# ===================== USER HANDLERS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    existing = get_user(u.id)
    add_user(u.id, u.username or "", u.full_name)
    if not existing:
        await update.message.reply_text(
            "🌐 Tilni tanlang / Выберите язык / Choose language:",
            reply_markup=lang_kb()
        )
        return
    await update.message.reply_text(t(u.id, 'welcome', name=u.first_name),
                                    parse_mode=ParseMode.HTML, reply_markup=menu_kb(u.id))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(t(uid, 'help'), parse_mode=ParseMode.HTML)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    if not u:
        await update.message.reply_text("❌ /start yuboring.")
        return
    if u['is_subscribed']:
        price = u.get('subscription_price', 0) or 0
        by = u.get('added_by_username') or 'Admin'
        await update.message.reply_text(t(uid, 'sub_ok', price=f"{price:,}", by=by), parse_mode=ParseMode.HTML)
    else:
        price = get_setting('subscription_price') or '10000'
        contact = get_setting('admin_contact') or "@admin"
        await update.message.reply_text(t(uid, 'no_sub', price=f"{int(price):,}", contact=contact), parse_mode=ParseMode.HTML)

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 Tilni tanlang:", reply_markup=lang_kb())

async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    if not u or (not u['is_subscribed'] and uid != SUPER_ADMIN_ID):
        price = get_setting('subscription_price') or '10000'
        contact = get_setting('admin_contact') or "@admin"
        await update.message.reply_text(t(uid, 'no_sub', price=f"{int(price):,}", contact=contact), parse_mode=ParseMode.HTML)
        return
    if uid in quiz_state:
        await update.message.reply_text(t(uid, 'already_active'))
        return
    await update.message.reply_text(t(uid, 'send_docx'))

async def cmd_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = quiz_state.get(uid)
    if state:
        quiz = finish_quiz(state['session_id'])
        quiz_state.pop(uid, None)
        gc.collect()
        await send_result(update, quiz, uid, context)
    else:
        active = get_active_quiz(uid)
        if active:
            quiz = finish_quiz(active['id'])
            await send_result(update, quiz, uid, context)
        else:
            await update.message.reply_text(t(uid, 'no_quiz'))

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in quiz_state:
        finish_quiz(quiz_state[uid]['session_id'])
        quiz_state.pop(uid, None)
        gc.collect()
    await update.message.reply_text(t(uid, 'send_docx'))

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    if not u or (not u['is_subscribed'] and uid != SUPER_ADMIN_ID):
        price = get_setting('subscription_price') or '10000'
        contact = get_setting('admin_contact') or "@admin"
        await update.message.reply_text(t(uid, 'no_sub', price=f"{int(price):,}", contact=contact), parse_mode=ParseMode.HTML)
        return
    doc = update.message.document
    if not doc.file_name.endswith('.docx'):
        await update.message.reply_text(t(uid, 'invalid_file'))
        return
    if uid in quiz_state:
        await update.message.reply_text(t(uid, 'already_active'))
        return
    msg = await update.message.reply_text("⏳ O'qilmoqda...")
    try:
        file = await doc.get_file()
        file_bytes = bytes(await file.download_as_bytearray())
        data = parse_docx(file_bytes)
        del file_bytes
        gc.collect()
        if not data:
            await msg.edit_text(t(uid, 'parse_err'))
            return
        questions = data['questions']
        session_id = create_quiz_session(uid, data['name'], len(questions))
        for q in questions:
            save_question(session_id, q['num'], q['question'], q['a'], q['b'], q['c'], q['d'], q['answer'])
        quiz_state[uid] = {'session_id': session_id, 'questions': questions, 'current': 0}
        await msg.edit_text(t(uid, 'quiz_started', name=data['name'], total=len(questions)), parse_mode=ParseMode.HTML)
        await send_question(uid, context)
    except Exception as e:
        logger.error(f"handle_document error: {e}")
        await msg.edit_text(f"❌ Xatolik: {str(e)[:200]}")

async def send_question(uid, context):
    state = quiz_state.get(uid)
    if not state: return
    idx = state['current']
    qs = state['questions']
    if idx >= len(qs):
        quiz = finish_quiz(state['session_id'])
        quiz_state.pop(uid, None)
        gc.collect()
        await context.bot.send_message(uid, build_result(quiz), parse_mode=ParseMode.HTML)
        return
    q = qs[idx]
    text = (f"❓ <b>Savol {idx+1}/{len(qs)}</b>\n\n<b>{q['question']}</b>\n\n"
            f"🔵 A) {q['a']}\n🔵 B) {q['b']}\n🔵 C) {q['c']}\n🔵 D) {q['d']}")
    await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=answer_kb())

async def send_result(update, quiz, uid, context):
    text = build_result(quiz)
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)

def build_result(quiz):
    if not quiz: return "🏁 Quiz yakunlandi."
    c = quiz['correct_answers'] or 0
    w = quiz['wrong_answers'] or 0
    total = quiz['total_questions'] or 1
    p = round(c/total*100, 1)
    emoji = "🌟" if p >= 90 else "👍" if p >= 70 else "📚" if p >= 50 else "💪"
    result = "Ajoyib!" if p >= 90 else "Yaxshi!" if p >= 70 else "O'rtacha." if p >= 50 else "Ko'proq o'qing!"
    return (f"🏁 <b>Quiz yakunlandi!</b>\n\n📝 <b>{quiz['quiz_name']}</b>\n\n"
            f"✅ To'g'ri: <b>{c}</b>\n❌ Noto'g'ri: <b>{w}</b>\n📈 Foiz: <b>{p}%</b>\n\n{emoji} {result}")

# ===================== CALLBACKS =====================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data.startswith("lang_"):
        lang = data.replace("lang_", "")
        update_user_language(uid, lang)
        u = query.from_user
        await query.edit_message_text(
            t(uid, 'welcome', name=u.first_name),
            parse_mode=ParseMode.HTML
        )
        await context.bot.send_message(uid, t(uid, 'menu_quiz'), reply_markup=menu_kb(uid))
        return

    if data.startswith("answer_"):
        state = quiz_state.get(uid)
        if not state:
            await query.edit_message_reply_markup(None)
            return
        answer = data.replace("answer_", "")
        idx = state['current']
        qs = state['questions']
        if idx >= len(qs): return
        q = qs[idx]
        is_correct = answer_question(state['session_id'], q['num'], answer)
        res = t(uid, 'correct') if is_correct else t(uid, 'wrong', ans=q['answer'])
        q_text = (f"❓ <b>Savol {idx+1}/{len(qs)}</b>\n\n<b>{q['question']}</b>\n\n"
                  f"{'✅' if answer=='A' else '🔵'} A) {q['a']}\n"
                  f"{'✅' if answer=='B' else '🔵'} B) {q['b']}\n"
                  f"{'✅' if answer=='C' else '🔵'} C) {q['c']}\n"
                  f"{'✅' if answer=='D' else '🔵'} D) {q['d']}\n\n{res}")
        try:
            await query.edit_message_text(q_text, parse_mode=ParseMode.HTML)
        except: pass
        state['current'] += 1
        quiz_state[uid] = state
        await asyncio.sleep(0.3)
        await send_question(uid, context)
        return

    if data.startswith("admin_") or data == "noop":
        await handle_admin_cb(query, uid, data, context)

# ===================== ADMIN CALLBACKS =====================
async def handle_admin_cb(query, uid, data, context):
    is_super = uid == SUPER_ADMIN_ID
    is_adm = db_is_admin(uid) or is_super
    if not is_adm:
        await query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return

    if data == "noop": return
    if data in ("admin_back", "admin_cancel"):
        admin_states.pop(uid, None)
        await query.edit_message_text(
            "👑 Admin Panel" if is_super else "👮 Admin Panel",
            parse_mode=ParseMode.HTML, reply_markup=admin_kb(is_super))
        return

    if data in ("admin_stats", "admin_full_stats"):
        counts = get_users_count()
        s = get_admin_stats(uid)
        text = (f"📊 <b>Statistika</b>\n\n👥 Jami: <b>{counts['total']}</b>\n"
                f"✅ Obunalilar: <b>{counts['subscribed']}</b>\n📅 Bugun: <b>{counts['today']}</b>\n\n"
                f"➕ Siz qo'shgan: <b>{s['subscribed_count']}</b>\n➖ Siz olgan: <b>{s['unsubscribed_count']}</b>")
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data.startswith("admin_users"):
        page = int(data.split("_page_")[1]) if "_page_" in data else 0
        per = 8
        users = get_all_users(limit=per, offset=page*per)
        counts = get_users_count()
        tp = max(1, -(-counts['total'] // per))
        buttons = []
        for u in users:
            icon = "✅" if u['is_subscribed'] else "❌"
            name = u.get('full_name') or "?"
            uname = f"@{u['username']}" if u.get('username') else str(u['user_id'])
            buttons.append([InlineKeyboardButton(f"{icon} {name} | {uname}", callback_data=f"admin_user_{u['user_id']}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"admin_users_page_{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{tp}", callback_data="noop"))
        if page < tp-1: nav.append(InlineKeyboardButton("▶️", callback_data=f"admin_users_page_{page+1}"))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")])
        await query.edit_message_text(f"👥 <b>Foydalanuvchilar</b> ({page+1}/{tp})",
                                       parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("admin_user_") and not data.startswith("admin_users"):
        target_id = int(data.replace("admin_user_", ""))
        u = get_user(target_id)
        if not u:
            await query.edit_message_text("❌ Topilmadi.", reply_markup=back_kb("admin_users"))
            return
        price = u.get('subscription_price', 0) or 0
        by = u.get('added_by_username') or '-'
        name = u.get('full_name') or "?"
        uname = f"@{u['username']}" if u.get('username') else "yo'q"
        sub = "✅ Obunali" if u['is_subscribed'] else "❌ Obunasiz"
        text = (f"👤 <b>{name}</b>\n🔗 {uname}\n🆔 <code>{target_id}</code>\n"
                f"📊 {sub}\n🌐 {u.get('language','uz').upper()}\n"
                f"📅 {str(u.get('joined_at',''))[:10]}\n")
        if u['is_subscribed']:
            text += f"💰 {price:,} so'm\n👮 @{by}"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                       reply_markup=user_action_kb(target_id, u['is_subscribed']))
        return

    if data.startswith("admin_subscribers"):
        page = int(data.split("_page_")[1]) if "_page_" in data else 0
        per = 8
        all_s = get_subscribed_users()
        tp = max(1, -(-len(all_s) // per))
        subs = all_s[page*per:(page+1)*per]
        buttons = []
        for u in subs:
            name = u.get('full_name') or "?"
            uname = f"@{u['username']}" if u.get('username') else str(u['user_id'])
            price = u.get('subscription_price', 0) or 0
            buttons.append([InlineKeyboardButton(f"✅ {name} | {uname} | {price:,}", callback_data=f"admin_user_{u['user_id']}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"admin_subscribers_page_{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{tp}", callback_data="noop"))
        if page < tp-1: nav.append(InlineKeyboardButton("▶️", callback_data=f"admin_subscribers_page_{page+1}"))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")])
        await query.edit_message_text(f"✅ <b>Obunalilar</b> ({len(all_s)} ta)",
                                       parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("admin_sub_"):
        target_id = int(data.replace("admin_sub_", ""))
        u = get_user(target_id)
        price = int(get_setting('subscription_price') or 10000)
        subscribe_user(target_id, uid, query.from_user.username or str(uid), price)
        uname = u.get('username') or str(target_id) if u else str(target_id)
        log_action(uid, query.from_user.username or str(uid), "subscribe", target_id, uname, f"{price:,}")
        await query.edit_message_text(f"✅ @{uname} obunaga qo'shildi! 💰 {price:,} so'm",
                                       parse_mode=ParseMode.HTML, reply_markup=back_kb("admin_users"))
        try: await context.bot.send_message(target_id, f"✅ Obuna berildi! 💰 {price:,} so'm\n👤 @{query.from_user.username or 'Admin'}", parse_mode=ParseMode.HTML)
        except: pass
        return

    if data.startswith("admin_unsub_"):
        target_id = int(data.replace("admin_unsub_", ""))
        u = get_user(target_id)
        unsubscribe_user(target_id)
        uname = u.get('username') or str(target_id) if u else str(target_id)
        log_action(uid, query.from_user.username or str(uid), "unsubscribe", target_id, uname, "Olindi")
        await query.edit_message_text(f"✅ @{uname} obunadan olindi.", parse_mode=ParseMode.HTML, reply_markup=back_kb("admin_users"))
        try: await context.bot.send_message(target_id, "❌ Obunangiz bekor qilindi.")
        except: pass
        return

    if data == "admin_add_sub":
        admin_states[uid] = "waiting_add_sub"
        await query.edit_message_text("➕ <b>Obunaga qo'shish</b>\n\nUsername yoki ID yuboring:",
                                       parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "admin_remove_sub":
        admin_states[uid] = "waiting_remove_sub"
        await query.edit_message_text("➖ <b>Obunadan olish</b>\n\nUsername yoki ID yuboring:",
                                       parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "admin_search":
        admin_states[uid] = "waiting_search"
        await query.edit_message_text("🔍 Username yoki ID yuboring:", parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "admin_set_price":
        if not is_super:
            await query.answer("❌ Faqat super admin!", show_alert=True)
            return
        cur_price = get_setting('subscription_price') or '10000'
        admin_states[uid] = "waiting_set_price"
        await query.edit_message_text(f"💰 Hozirgi narx: <b>{int(cur_price):,} so'm</b>\n\nYangi narx yuboring:",
                                       parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "admin_manage_admins":
        if not is_super:
            await query.answer("❌ Ruxsat yo'q!", show_alert=True)
            return
        admins = get_all_admins()
        buttons = []
        for adm in admins:
            name = adm.get('full_name') or "?"
            uname = f"@{adm['username']}" if adm.get('username') else str(adm['admin_id'])
            buttons.append([
                InlineKeyboardButton(f"👮 {name} | {uname}", callback_data="noop"),
                InlineKeyboardButton("🗑", callback_data=f"admin_del_admin_{adm['admin_id']}")
            ])
        buttons.append([InlineKeyboardButton("➕ Admin qo'shish", callback_data="admin_add_admin")])
        buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_back")])
        await query.edit_message_text("👮 <b>Adminlar</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "admin_add_admin":
        if not is_super:
            await query.answer("❌ Ruxsat yo'q!", show_alert=True)
            return
        admin_states[uid] = "waiting_add_admin"
        await query.edit_message_text("➕ Yangi admin ID sini yuboring:", reply_markup=back_kb("admin_manage_admins"))
        return

    if data.startswith("admin_del_admin_"):
        if not is_super:
            await query.answer("❌ Ruxsat yo'q!", show_alert=True)
            return
        adm_id = int(data.replace("admin_del_admin_", ""))
        remove_admin(adm_id)
        log_action(uid, query.from_user.username or str(uid), "remove_admin", adm_id)
        await query.edit_message_text("✅ Admin o'chirildi.", reply_markup=back_kb("admin_manage_admins"))
        return

    if data == "admin_logs":
        logs = get_logs(None if is_super else uid, limit=15)
        icons = {'subscribe':'➕','unsubscribe':'➖','add_admin':'👮','remove_admin':'🗑','set_price':'💰','broadcast':'📢'}
        text = "📋 <b>Loglar</b>\n\n"
        for log in logs:
            icon = icons.get(log['action'], '📝')
            adm = f"@{log['admin_username']}" if log.get('admin_username') else str(log['admin_id'])
            target = f"→ @{log['target_username']}" if log.get('target_username') else ""
            date = str(log['created_at'])[:16]
            text += f"{icon} {adm} {target} | {date}\n"
        if not logs: text += "Loglar yo'q."
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

    if data == "admin_broadcast":
        if not is_super:
            await query.answer("❌ Ruxsat yo'q!", show_alert=True)
            return
        admin_states[uid] = "waiting_broadcast"
        await query.edit_message_text("📢 Barcha userlarga yuboriladigan matn yozing:", reply_markup=back_kb())
        return

    if data == "admin_set_contact":
        if not is_super:
            await query.answer("❌ Ruxsat yo'q!", show_alert=True)
            return
        current = get_setting('admin_contact') or "Yo'q"
        admin_states[uid] = "waiting_set_contact"
        await query.edit_message_text(
            f"👤 <b>Bog'lanish linki</b>\n\nHozirgi: <b>{current}</b>\n\nYangi username yoki link yuboring:",
            parse_mode=ParseMode.HTML, reply_markup=back_kb())
        return

# ===================== TEXT MESSAGES =====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document:
        await handle_document(update, context)
        return

    uid = update.effective_user.id
    text = update.message.text or ""
    is_adm = db_is_admin(uid) or uid == SUPER_ADMIN_ID
    is_super = uid == SUPER_ADMIN_ID

    if is_adm and uid in admin_states:
        state = admin_states[uid]

        if state == "waiting_add_sub":
            results = search_user(text.lstrip('@'))
            if not results:
                await update.message.reply_text("❌ Topilmadi.")
            elif len(results) == 1:
                u = results[0]
                if u['is_subscribed']:
                    await update.message.reply_text("⚠️ Allaqachon obunali!")
                else:
                    price = int(get_setting('subscription_price') or 10000)
                    subscribe_user(u['user_id'], uid, update.effective_user.username or str(uid), price)
                    uname = u.get('username') or str(u['user_id'])
                    log_action(uid, update.effective_user.username or str(uid), "subscribe", u['user_id'], uname, f"{price:,}")
                    await update.message.reply_text(f"✅ @{uname} obunaga qo'shildi! 💰 {price:,} so'm", parse_mode=ParseMode.HTML)
                    try: await context.bot.send_message(u['user_id'], f"✅ Obuna berildi! 💰 {price:,} so'm", parse_mode=ParseMode.HTML)
                    except: pass
            else:
                btns = []
                for u in results[:6]:
                    n = u.get('full_name') or "?"
                    un = f"@{u['username']}" if u.get('username') else str(u['user_id'])
                    icon = "✅" if u['is_subscribed'] else "❌"
                    btns.append([InlineKeyboardButton(f"{icon} {n} | {un}", callback_data=f"admin_sub_{u['user_id']}")])
                btns.append([InlineKeyboardButton("⬅️ Bekor", callback_data="admin_back")])
                await update.message.reply_text("Qaysi?", reply_markup=InlineKeyboardMarkup(btns))
            admin_states.pop(uid, None)
            return

        if state == "waiting_remove_sub":
            results = search_user(text.lstrip('@'))
            if not results:
                await update.message.reply_text("❌ Topilmadi.")
            elif len(results) == 1:
                u = results[0]
                if not u['is_subscribed']:
                    await update.message.reply_text("⚠️ Obunali emas!")
                else:
                    unsubscribe_user(u['user_id'])
                    uname = u.get('username') or str(u['user_id'])
                    log_action(uid, update.effective_user.username or str(uid), "unsubscribe", u['user_id'], uname)
                    await update.message.reply_text(f"✅ @{uname} obunadan olindi.", parse_mode=ParseMode.HTML)
                    try: await context.bot.send_message(u['user_id'], "❌ Obunangiz bekor qilindi.")
                    except: pass
            else:
                btns = []
                for u in results[:6]:
                    n = u.get('full_name') or "?"
                    un = f"@{u['username']}" if u.get('username') else str(u['user_id'])
                    icon = "✅" if u['is_subscribed'] else "❌"
                    btns.append([InlineKeyboardButton(f"{icon} {n} | {un}", callback_data=f"admin_unsub_{u['user_id']}")])
                btns.append([InlineKeyboardButton("⬅️ Bekor", callback_data="admin_back")])
                await update.message.reply_text("Qaysi?", reply_markup=InlineKeyboardMarkup(btns))
            admin_states.pop(uid, None)
            return

        if state == "waiting_search":
            results = search_user(text.lstrip('@'))
            admin_states.pop(uid, None)
            if not results:
                await update.message.reply_text("❌ Topilmadi.")
                return
            for u in results[:5]:
                price = u.get('subscription_price', 0) or 0
                by = u.get('added_by_username') or '-'
                name = u.get('full_name') or "?"
                uname = f"@{u['username']}" if u.get('username') else "yo'q"
                sub = "✅ Obunali" if u['is_subscribed'] else "❌ Obunasiz"
                info = (f"👤 <b>{name}</b>\n🔗 {uname}\n🆔 <code>{u['user_id']}</code>\n"
                        f"📊 {sub}\n📅 {str(u.get('joined_at',''))[:10]}")
                if u['is_subscribed']:
                    info += f"\n💰 {price:,} so'm\n👮 @{by}"
                await update.message.reply_text(info, parse_mode=ParseMode.HTML,
                                                reply_markup=user_action_kb(u['user_id'], u['is_subscribed']))
            return

        if state == "waiting_set_price" and is_super:
            try:
                price = int(text.replace(' ','').replace(',',''))
                set_setting('subscription_price', str(price))
                log_action(uid, update.effective_user.username or str(uid), "set_price", details=f"{price:,}")
                await update.message.reply_text(f"✅ Narx: <b>{price:,} so'm</b>", parse_mode=ParseMode.HTML)
            except:
                await update.message.reply_text("❌ Noto'g'ri raqam.")
            admin_states.pop(uid, None)
            return

        if state == "waiting_set_contact" and is_super:
            contact = text.strip()
            if not contact.startswith("@") and not contact.startswith("http"):
                contact = "@" + contact
            set_setting('admin_contact', contact)
            log_action(uid, update.effective_user.username or str(uid), "set_contact", details=contact)
            admin_states.pop(uid, None)
            await update.message.reply_text(f"✅ Bog'lanish linki yangilandi: <b>{contact}</b>", parse_mode=ParseMode.HTML)
            return

        if state == "waiting_add_admin" and is_super:
            try:
                new_id = int(text)
                u = get_user(new_id)
                fn = u['full_name'] if u else "Noma'lum"
                un = u['username'] if u else ""
                add_admin(new_id, un, fn, uid)
                log_action(uid, update.effective_user.username or str(uid), "add_admin", new_id, un)
                await update.message.reply_text(f"✅ Admin qo'shildi: <code>{new_id}</code>", parse_mode=ParseMode.HTML)
                try: await context.bot.send_message(new_id, "👮 Siz admin qilindingiz! /start yuboring.")
                except: pass
            except:
                await update.message.reply_text("❌ Noto'g'ri ID.")
            admin_states.pop(uid, None)
            return

        if state == "waiting_broadcast" and is_super:
            admin_states.pop(uid, None)
            users = get_all_users(limit=10000)
            ok = err = 0
            msg = await update.message.reply_text("📢 Yuborilmoqda...")
            for u in users:
                try:
                    await context.bot.send_message(u['user_id'], text, parse_mode=ParseMode.HTML)
                    ok += 1
                    await asyncio.sleep(0.05)
                except:
                    err += 1
            await msg.edit_text(f"✅ Yuborildi: <b>{ok}</b>\n❌ Xato: <b>{err}</b>", parse_mode=ParseMode.HTML)
            log_action(uid, update.effective_user.username or str(uid), "broadcast", details=f"OK:{ok} ERR:{err}")
            return

    # Menu buttons
    if "Quiz boshlash" in text or "Начать квиз" in text or "Start quiz" in text:
        await cmd_quiz(update, context)
    elif "Holat" in text or "Статус" in text or "Status" in text:
        await cmd_status(update, context)
    elif "Til" in text or "Язык" in text or "Language" in text:
        await cmd_lang(update, context)
    elif "Yordam" in text or "Помощь" in text or "Help" in text:
        await cmd_help(update, context)
    elif "Admin panel" in text and is_adm:
        await update.message.reply_text(
            "👑 Admin Panel" if is_super else "👮 Admin Panel",
            parse_mode=ParseMode.HTML, reply_markup=admin_kb(is_super))

# ===================== MAIN =====================
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Botni boshlash"),
        BotCommand("quiz", "Quiz boshlash"),
        BotCommand("restart", "Qayta boshlash"),
        BotCommand("finish", "To'xtatish"),
        BotCommand("status", "Obuna holati"),
        BotCommand("lang", "Til o'zgartirish"),
        BotCommand("help", "Yordam"),
    ])

def main():
    init_db()
    logger.warning("✅ DB tayyor, bot ishga tushmoqda...")
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .concurrent_updates(True)
           .connection_pool_size(4)
           .read_timeout(30)
           .write_timeout(30)
           .connect_timeout(30)
           .pool_timeout(30)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("quiz", cmd_quiz))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("finish", cmd_finish))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
    PORT = int(os.getenv("PORT", 8080))

    if WEBHOOK_URL:
        logger.warning(f"🚀 Webhook mode: {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.warning("🚀 Polling mode")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )

if __name__ == "__main__":
    main()
