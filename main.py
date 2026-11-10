"""
Inglizcha 1500 so'z - mnemonik Telegram bot + Mini App (TMA)

Texnologiyalar: aiogram 3 (bot), aiohttp (Mini App serveri + API),
aiosqlite (SQLite). Hammasi bitta jarayonda ishlaydi: python main.py
"""
import asyncio
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, MenuButtonWebApp, Message,
    ReplyKeyboardMarkup, WebAppInfo,
)
from aiohttp import web
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# SOZLAMALAR (.env faylidan o'qiladi)
# ----------------------------------------------------------------------------
load_dotenv()
BASE_DIR = Path(__file__).parent
BOT_TOKEN = os.getenv("9072083795:AAFNIkjPB7ycGVH9XiiFP7j1aa59QyeAPck", "").strip()
# Admin Telegram ID'lari (vergul bilan): ADMIN_IDS=123456,789012
ADMIN_IDS = {int(x) for x in os.getenv("1048256299", "").replace(" ", "").split(",") if x.isdigit()}
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")  # https://... manzil
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "database.db"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))  # Toshkent vaqti bilan
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"  # brauzerda sinash uchun (initData'siz)
TZ = timezone(timedelta(hours=5))  # O'zbekiston (UTC+5), yozgi vaqt yo'q

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mnemo")

# ----------------------------------------------------------------------------
# MA'LUMOTLAR BAZASI (database.db avtomatik yaratiladi)
# ----------------------------------------------------------------------------
db: aiosqlite.Connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    first_name TEXT, username TEXT,
    score INTEGER DEFAULT 0, streak INTEGER DEFAULT 0,
    last_active TEXT, remind INTEGER DEFAULT 1, last_reminder TEXT,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS words(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    english TEXT NOT NULL, uzbek TEXT NOT NULL,
    mnemonic TEXT DEFAULT '', example TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS ux_words_en ON words(english COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS memes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT NOT NULL,
    caption TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS progress(
    user_id INTEGER, word_id INTEGER,
    known INTEGER DEFAULT 0, correct INTEGER DEFAULT 0, wrong INTEGER DEFAULT 0,
    last_seen TEXT, PRIMARY KEY(user_id, word_id));
"""


async def rows(sql, params=()):
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def one(sql, params=()):
    async with db.execute(sql, params) as cur:
        return await cur.fetchone()


async def run(sql, params=()):
    cur = await db.execute(sql, params)
    await db.commit()
    return cur


async def import_csv(data: bytes):
    """CSV (english,uzbek,mnemonic,example) ni bazaga qo'shadi. (qo'shildi, o'tkazildi)"""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    added = skipped = 0
    for r in reader:
        en, uz = (r.get("english") or "").strip(), (r.get("uzbek") or "").strip()
        if not en or not uz:
            skipped += 1
            continue
        cur = await db.execute(
            "INSERT OR IGNORE INTO words(english,uzbek,mnemonic,example) VALUES(?,?,?,?)",
            (en, uz, (r.get("mnemonic") or "").strip(), (r.get("example") or "").strip()))
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    await db.commit()
    return added, skipped


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(SCHEMA)
    await db.commit()
    # Baza bo'sh bo'lsa, words_seed.csv dan boshlang'ich so'zlarni yuklaymiz
    if (await one("SELECT COUNT(*) c FROM words"))["c"] == 0:
        seed = BASE_DIR / "words_seed.csv"
        if seed.exists():
            added, _ = await import_csv(seed.read_bytes())
            log.info("words_seed.csv dan %s ta so'z yuklandi", added)


# ----------------------------------------------------------------------------
# YORDAMCHI FUNKSIYALAR (foydalanuvchi, progress, so'z/savol tanlash)
# ----------------------------------------------------------------------------
def esc(s) -> str:
    return html.escape(str(s or ""))


def today():
    return datetime.now(TZ).date()


async def ensure_user(uid, first_name, username):
    await run(
        "INSERT INTO users(user_id,first_name,username,last_reminder) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username",
        (uid, first_name or "", username or "", today().isoformat()))


async def touch(uid):
    """Kunlik streak (ketma-ket faol kunlar) ni yangilaydi."""
    u = await one("SELECT streak,last_active FROM users WHERE user_id=?", (uid,))
    t = today()
    if not u or u["last_active"] == t.isoformat():
        return
    yesterday = (t - timedelta(days=1)).isoformat()
    streak = u["streak"] + 1 if u["last_active"] == yesterday else 1
    await run("UPDATE users SET streak=?, last_active=? WHERE user_id=?", (streak, t.isoformat(), uid))


async def record(uid, wid, correct: bool, points=0):
    """Javobni yozadi. 2 marta to'g'ri (va xatodan ko'p) bo'lsa so'z 'o'rganilgan' hisoblanadi."""
    await db.execute(
        """INSERT INTO progress(user_id,word_id,known,correct,wrong,last_seen) VALUES(?,?,0,?,?,?)
           ON CONFLICT(user_id,word_id) DO UPDATE SET
             correct=correct+excluded.correct, wrong=wrong+excluded.wrong, last_seen=excluded.last_seen,
             known=CASE WHEN correct+excluded.correct>=2
                         AND correct+excluded.correct>wrong+excluded.wrong THEN 1 ELSE 0 END""",
        (uid, wid, int(correct), int(not correct), today().isoformat()))
    if points:
        await db.execute("UPDATE users SET score=score+? WHERE user_id=?", (points, uid))
    await db.commit()
    await touch(uid)


async def mark_seen(uid, wid):
    await run("INSERT OR IGNORE INTO progress(user_id,word_id,last_seen) VALUES(?,?,?)",
              (uid, wid, today().isoformat()))


async def pick_word(uid):
    """Foydalanuvchi hali ko'rmagan tasodifiy so'z (hammasi ko'rilgan bo'lsa - istalgani)."""
    w = await one(
        "SELECT w.* FROM words w LEFT JOIN progress p ON p.word_id=w.id AND p.user_id=? "
        "WHERE p.word_id IS NULL ORDER BY RANDOM() LIMIT 1", (uid,))
    return w or await one("SELECT * FROM words ORDER BY RANDOM() LIMIT 1")


async def make_questions(uid, n=1):
    """n ta test savoli: (so'z, [4 ta variant]). Hali o'rganilmagan so'zlar ustuvor."""
    words = await rows(
        "SELECT w.* FROM words w LEFT JOIN progress p ON p.word_id=w.id AND p.user_id=? "
        "ORDER BY COALESCE(p.known,0), RANDOM() LIMIT ?", (uid, n))
    result = []
    for w in words:
        others = await rows("SELECT id,uzbek FROM words WHERE id<>? AND uzbek<>? ORDER BY RANDOM() LIMIT 3",
                            (w["id"], w["uzbek"]))
        if len(others) < 3:
            return []  # kamida 4 ta so'z kerak
        opts = [{"id": w["id"], "text": w["uzbek"]}] + [{"id": o["id"], "text": o["uzbek"]} for o in others]
        random.shuffle(opts)
        result.append((w, opts))
    return result


async def todays_task():
    """Kunlik vazifa: kun raqamiga qarab navbatma-navbat tanlanadi."""
    ts = await rows("SELECT * FROM tasks ORDER BY id")
    return ts[today().toordinal() % len(ts)] if ts else None


def fmt_word(w) -> str:
    t = f"🔤 <b>{esc(w['english'])}</b> — <b>{esc(w['uzbek'])}</b>"
    if w["mnemonic"]:
        t += f"\n\n💡 <b>Mnemonika:</b> {esc(w['mnemonic'])}"
    if w["example"]:
        t += f"\n\n📖 <i>{esc(w['example'])}</i>"
    return t


def webapp_ok() -> bool:
    return WEBAPP_URL.startswith("https://")  # Telegram faqat HTTPS qabul qiladi


# ----------------------------------------------------------------------------
# BOT: klaviaturalar va foydalanuvchi buyruqlari
# ----------------------------------------------------------------------------
def main_kb():
    rows_ = []
    if webapp_ok():
        rows_.append([KeyboardButton(text="📚 Mini App'ni ochish", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows_ += [[KeyboardButton(text="🔤 Kunlik so'z"), KeyboardButton(text="📝 Test")],
              [KeyboardButton(text="🎯 Vazifa"), KeyboardButton(text="😂 Meme")],
              [KeyboardButton(text="📊 Natijam")]]
    return ReplyKeyboardMarkup(keyboard=rows_, resize_keyboard=True)


def kb_more():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔤 Yana so'z", callback_data="nxt:w"),
        InlineKeyboardButton(text="📝 Test", callback_data="nxt:q")]])


common_router = Router()  # /cancel
admin_router = Router()   # faqat adminlar
user_router = Router()    # hamma uchun


class IsAdmin(BaseFilter):
    """Whitelist: faqat ADMIN_IDS ichidagi Telegram ID'lar."""
    async def __call__(self, event) -> bool:
        return bool(event.from_user and event.from_user.id in ADMIN_IDS)


admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

DENY = "⛔ Sizda bu buyruq uchun ruxsat yo'q."
USER_HELP = ("📚 <b>Buyruqlar</b>\n/word — kunlik so'z\n/test — test\n/task — bugungi vazifa\n"
             "/meme — mnemonik meme\n/me — natijam\n/remind on|off — kunlik eslatma")


@common_router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.")


@user_router.message(CommandStart())
async def start(m: Message):
    u = m.from_user
    await ensure_user(u.id, u.first_name, u.username)
    text = (f"Salom, <b>{esc(u.first_name)}</b>! 👋\n\n"
            "Bu yerda ingliz tilidagi eng ko'p ishlatiladigan <b>1500 ta so'z</b>ni "
            "o'zbekcha o'xshash so'zlar, qiziq obrazlar va hikoyalar orqali oson eslab qolasiz.\n\n"
            "📚 Pastdagi tugma orqali Mini App'ni oching: kartochkalar, o'yin va memelar sizni kutmoqda.\n"
            "🔔 Har kuni ertalab yangi so'z yuboraman (o'chirish: /remind off).")
    if not webapp_ok():
        text += "\n\n⚠️ Mini App manzili (WEBAPP_URL) hali sozlanmagan."
    await m.answer(text, reply_markup=main_kb())


@user_router.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(USER_HELP)


async def send_daily_word(msg: Message, uid: int):
    w = await pick_word(uid)
    if not w:
        return await msg.answer("Hozircha bazada so'z yo'q. Tez orada qo'shiladi!")
    await mark_seen(uid, w["id"])
    await touch(uid)
    await msg.answer("☀️ <b>Kunlik so'z</b>\n\n" + fmt_word(w), reply_markup=kb_more())


async def ask_question(msg: Message, uid: int):
    qs = await make_questions(uid, 1)
    if not qs:
        return await msg.answer("Test uchun bazada kamida 4 ta so'z bo'lishi kerak.")
    w, opts = qs[0]
    kb = [[InlineKeyboardButton(text=o["text"], callback_data=f"qz:{w['id']}:{o['id']}")] for o in opts]
    await msg.answer(f"📝 <b>{esc(w['english'])}</b> so'zining tarjimasi qaysi?",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@user_router.message(Command("word"))
@user_router.message(F.text == "🔤 Kunlik so'z")
async def word_cmd(m: Message):
    await send_daily_word(m, m.from_user.id)


@user_router.message(Command("test"))
@user_router.message(F.text == "📝 Test")
async def test_cmd(m: Message):
    await ask_question(m, m.from_user.id)


@user_router.callback_query(F.data.startswith("nxt:"))
async def next_cb(cb: CallbackQuery):
    await cb.answer()
    if cb.data == "nxt:w":
        await send_daily_word(cb.message, cb.from_user.id)
    else:
        await ask_question(cb.message, cb.from_user.id)


@user_router.callback_query(F.data.startswith("qz:"))
async def quiz_cb(cb: CallbackQuery):
    _, wid, opt = cb.data.split(":")
    wid, opt = int(wid), int(opt)
    w = await one("SELECT * FROM words WHERE id=?", (wid,))
    if not w:
        return await cb.answer("So'z topilmadi", show_alert=True)
    ok = wid == opt
    await record(cb.from_user.id, wid, ok, 10 if ok else 0)
    head = "✅ <b>To'g'ri!</b> +10 ball" if ok else f"❌ <b>Noto'g'ri.</b> To'g'ri javob: <b>{esc(w['uzbek'])}</b>"
    try:
        await cb.message.edit_text(head + "\n\n" + fmt_word(w), reply_markup=kb_more())
    except TelegramBadRequest:
        pass
    await cb.answer()


@user_router.message(Command("task"))
@user_router.message(F.text == "🎯 Vazifa")
async def task_cmd(m: Message):
    t = await todays_task()
    await m.answer(f"🎯 <b>Bugungi vazifa</b>\n\n{esc(t['text'])}" if t else "Hozircha vazifa yo'q.")


@user_router.message(Command("meme"))
@user_router.message(F.text == "😂 Meme")
async def meme_cmd(m: Message):
    meme = await one("SELECT * FROM memes ORDER BY RANDOM() LIMIT 1")
    if not meme:
        return await m.answer("Hozircha memelar yo'q.")
    try:
        await m.answer_photo(meme["file_id"], caption=esc(meme["caption"]) or "😂")
    except TelegramBadRequest:
        await m.answer("Rasmni yuborib bo'lmadi, keyinroq urinib ko'ring.")


@user_router.message(Command("me"))
@user_router.message(F.text == "📊 Natijam")
async def me_cmd(m: Message):
    uid = m.from_user.id
    await ensure_user(uid, m.from_user.first_name, m.from_user.username)
    u = await one("SELECT * FROM users WHERE user_id=?", (uid,))
    total = (await one("SELECT COUNT(*) c FROM words"))["c"]
    known = (await one("SELECT COUNT(*) c FROM progress WHERE user_id=? AND known=1", (uid,)))["c"]
    await m.answer(f"📊 <b>Natijam</b>\n\n⭐ Ball: <b>{u['score']}</b>\n🔥 Streak: <b>{u['streak']}</b> kun\n"
                   f"✅ O'rganilgan: <b>{known}</b> / {total}")


@user_router.message(Command("remind"))
async def remind_cmd(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        return await m.answer("Foydalanish: /remind on yoki /remind off")
    await run("UPDATE users SET remind=? WHERE user_id=?", (1 if arg == "on" else 0, m.from_user.id))
    await m.answer("🔔 Eslatma yoqildi." if arg == "on" else "🔕 Eslatma o'chirildi.")


# Oddiy foydalanuvchi admin buyruqlarini yozsa - ruxsat yo'q
ADMIN_CMDS = ("admin", "addword", "delword", "listwords", "addtask", "deltask", "listtasks",
              "addmeme", "delmeme", "listmemes", "importwords", "stats")


@user_router.message(Command(*ADMIN_CMDS))
async def deny_msg(m: Message):
    await m.answer(DENY)


@user_router.callback_query(F.data.startswith(("adm:", "lw:")))
async def deny_cb(cb: CallbackQuery):
    await cb.answer(DENY, show_alert=True)


# ----------------------------------------------------------------------------
# ADMIN PANEL (/admin)
# ----------------------------------------------------------------------------
ADMIN_HELP = (
    "🛠 <b>Admin panel</b>\n\n"
    "<code>/addword english | o'zbekcha | mnemonika | misol</code>\n"
    "yoki faqat /addword — bosqichma-bosqich so'raydi\n"
    "<code>/delword ID</code> — so'zni o'chirish\n"
    "<code>/listwords [sahifa]</code> — so'zlar ro'yxati\n"
    "<code>/addtask matn</code> · <code>/deltask ID</code> · /listtasks\n"
    "/addmeme (keyin rasm yuboring) · <code>/delmeme ID</code> · /listmemes\n"
    "/importwords — CSV fayldan ko'p so'z yuklash\n"
    "/stats — statistika · /cancel — bekor qilish")


def admin_kb():
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text="➕ So'z qo'shish", callback_data="adm:addword"), b(text="📃 So'zlar", callback_data="adm:listwords")],
        [b(text="🖼 Meme qo'shish", callback_data="adm:addmeme"), b(text="📥 CSV import", callback_data="adm:import")],
        [b(text="📊 Statistika", callback_data="adm:stats")]])


class AddWord(StatesGroup):
    english = State()
    uzbek = State()
    mnemonic = State()
    example = State()


class AddMeme(StatesGroup):
    photo = State()


class ImportWords(StatesGroup):
    file = State()


async def save_word(en, uz, mn="", ex=""):
    try:
        cur = await run("INSERT INTO words(english,uzbek,mnemonic,example) VALUES(?,?,?,?)", (en, uz, mn, ex))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # bu inglizcha so'z allaqachon bor


async def stats_text():
    c = lambda sql: one(sql)
    users, words, tasks, memes = [(await c(f"SELECT COUNT(*) c FROM {t}"))["c"]
                                  for t in ("users", "words", "tasks", "memes")]
    active = (await one("SELECT COUNT(*) c FROM users WHERE last_active=?", (today().isoformat(),)))["c"]
    return (f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {users} (bugun faol: {active})\n"
            f"🔤 So'zlar: {words}\n🎯 Vazifalar: {tasks}\n😂 Memelar: {memes}")


@admin_router.message(Command("admin"))
async def admin_cmd(m: Message):
    await m.answer(ADMIN_HELP, reply_markup=admin_kb())


@admin_router.message(Command("stats"))
async def stats_cmd(m: Message):
    await m.answer(await stats_text())


# --- so'z qo'shish ---------------------------------------------------------
@admin_router.message(Command("addword"))
async def addword_cmd(m: Message, command: CommandObject, state: FSMContext):
    if not command.args:
        await state.set_state(AddWord.english)
        return await m.answer("1/4 Inglizcha so'zni yozing (bekor qilish: /cancel):")
    parts = [p.strip() for p in command.args.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return await m.answer("Format: <code>/addword english | o'zbekcha | mnemonika | misol</code>")
    parts += [""] * (4 - len(parts))
    wid = await save_word(*parts[:4])
    await m.answer(f"✅ Qo'shildi (ID: <code>{wid}</code>)" if wid else "⚠️ Bu so'z bazada allaqachon bor.")


@admin_router.callback_query(F.data == "adm:addword")
async def addword_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddWord.english)
    await cb.message.answer("1/4 Inglizcha so'zni yozing (bekor qilish: /cancel):")
    await cb.answer()


@admin_router.message(AddWord.english, F.text)
async def aw_en(m: Message, state: FSMContext):
    await state.update_data(en=m.text.strip())
    await state.set_state(AddWord.uzbek)
    await m.answer("2/4 O'zbekcha tarjimasi:")


@admin_router.message(AddWord.uzbek, F.text)
async def aw_uz(m: Message, state: FSMContext):
    await state.update_data(uz=m.text.strip())
    await state.set_state(AddWord.mnemonic)
    await m.answer("3/4 Mnemonika (o'xshash so'z yoki eslab qolish hikoyasi):")


@admin_router.message(AddWord.mnemonic, F.text)
async def aw_mn(m: Message, state: FSMContext):
    await state.update_data(mn=m.text.strip())
    await state.set_state(AddWord.example)
    await m.answer("4/4 Misol gap (o'tkazib yuborish uchun '-' yuboring):")


@admin_router.message(AddWord.example, F.text)
async def aw_ex(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    ex = "" if m.text.strip() == "-" else m.text.strip()
    wid = await save_word(d["en"], d["uz"], d["mn"], ex)
    await m.answer(f"✅ Qo'shildi (ID: <code>{wid}</code>)" if wid else "⚠️ Bu so'z bazada allaqachon bor.")


# --- ro'yxat (sahifalab) ---------------------------------------------------
PAGE = 30


async def words_page(page: int):
    total = (await one("SELECT COUNT(*) c FROM words"))["c"]
    pages = max(1, -(-total // PAGE))
    page = min(max(page, 1), pages)
    ws = await rows("SELECT id,english,uzbek FROM words ORDER BY id LIMIT ? OFFSET ?", (PAGE, (page - 1) * PAGE))
    if not ws:
        return "Bazada hali so'z yo'q.", None
    body = "\n".join(f"<code>{w['id']}</code> · {esc(w['english'])} — {esc(w['uzbek'])}" for w in ws)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"lw:{page - 1}"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"lw:{page + 1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None
    return f"📃 <b>So'zlar</b> ({total} ta) — {page}/{pages}\n\n{body}", kb


@admin_router.message(Command("listwords"))
async def listwords_cmd(m: Message, command: CommandObject):
    page = int(command.args) if command.args and command.args.strip().isdigit() else 1
    text, kb = await words_page(page)
    await m.answer(text, reply_markup=kb)


@admin_router.callback_query(F.data.startswith("lw:"))
async def listwords_cb(cb: CallbackQuery):
    text, kb = await words_page(int(cb.data.split(":")[1]))
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer()


@admin_router.callback_query(F.data == "adm:listwords")
async def adm_listwords(cb: CallbackQuery):
    text, kb = await words_page(1)
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@admin_router.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery):
    await cb.message.answer(await stats_text())
    await cb.answer()


# --- o'chirish buyruqlari (/delword, /deltask, /delmeme) --------------------
def register_delete(cmd: str, table: str, extra_sql: str | None = None):
    @admin_router.message(Command(cmd))
    async def _delete(m: Message, command: CommandObject):
        arg = (command.args or "").strip()
        if not arg.isdigit():
            return await m.answer(f"Foydalanish: <code>/{cmd} ID</code>")
        cur = await run(f"DELETE FROM {table} WHERE id=?", (int(arg),))
        if extra_sql:
            await run(extra_sql, (int(arg),))
        await m.answer("🗑 O'chirildi." if cur.rowcount else "❌ Bunday ID topilmadi.")


register_delete("delword", "words", "DELETE FROM progress WHERE word_id=?")
register_delete("deltask", "tasks")
register_delete("delmeme", "memes")


# --- vazifalar ---------------------------------------------------------------
@admin_router.message(Command("addtask"))
async def addtask_cmd(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        return await m.answer("Foydalanish: <code>/addtask Bugun 10 ta yangi so'z o'rganing</code>")
    cur = await run("INSERT INTO tasks(text) VALUES(?)", (text,))
    await m.answer(f"✅ Vazifa qo'shildi (ID: <code>{cur.lastrowid}</code>)")


@admin_router.message(Command("listtasks"))
async def listtasks_cmd(m: Message):
    ts = await rows("SELECT id,text FROM tasks ORDER BY id")
    await m.answer("🎯 <b>Vazifalar</b>\n\n" + "\n".join(f"<code>{t['id']}</code> · {esc(t['text'])}" for t in ts)
                   if ts else "Vazifalar yo'q.")


# --- memelar -----------------------------------------------------------------
@admin_router.message(Command("addmeme"))
async def addmeme_cmd(m: Message, state: FSMContext):
    await state.set_state(AddMeme.photo)
    await m.answer("🖼 Meme rasmini yuboring (izohni rasm ostiga yozing). Bekor qilish: /cancel")


@admin_router.callback_query(F.data == "adm:addmeme")
async def addmeme_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddMeme.photo)
    await cb.message.answer("🖼 Meme rasmini yuboring (izohni rasm ostiga yozing). Bekor qilish: /cancel")
    await cb.answer()


@admin_router.message(AddMeme.photo, F.photo)
async def addmeme_photo(m: Message, state: FSMContext):
    cur = await run("INSERT INTO memes(file_id,caption) VALUES(?,?)", (m.photo[-1].file_id, m.caption or ""))
    await state.clear()
    await m.answer(f"✅ Meme qo'shildi (ID: <code>{cur.lastrowid}</code>)")


@admin_router.message(AddMeme.photo)
async def addmeme_wrong(m: Message):
    await m.answer("Iltimos, rasm yuboring (yoki /cancel).")


@admin_router.message(Command("listmemes"))
async def listmemes_cmd(m: Message):
    ms = await rows("SELECT id,caption FROM memes ORDER BY id")
    await m.answer("😂 <b>Memelar</b>\n\n" + "\n".join(f"<code>{x['id']}</code> · {esc(x['caption']) or '(izohsiz)'}" for x in ms)
                   if ms else "Memelar yo'q.")


# --- CSV import --------------------------------------------------------------
IMPORT_TEXT = ("📥 CSV faylni yuboring. Ustunlar: <code>english,uzbek,mnemonic,example</code> "
               "(birinchi qator sarlavha, UTF-8). Bekor qilish: /cancel")


@admin_router.message(Command("importwords"))
async def import_cmd(m: Message, state: FSMContext):
    await state.set_state(ImportWords.file)
    await m.answer(IMPORT_TEXT)


@admin_router.callback_query(F.data == "adm:import")
async def import_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ImportWords.file)
    await cb.message.answer(IMPORT_TEXT)
    await cb.answer()


@admin_router.message(ImportWords.file, F.document)
async def import_file(m: Message, state: FSMContext, bot: Bot):
    buf = io.BytesIO()
    await bot.download(m.document, destination=buf)
    try:
        added, skipped = await import_csv(buf.getvalue())
    except Exception as e:  # noto'g'ri format/kodlash
        return await m.answer(f"❌ Faylni o'qib bo'lmadi: {esc(e)}")
    await state.clear()
    await m.answer(f"✅ Qo'shildi: <b>{added}</b> ta. O'tkazib yuborildi (takror/bo'sh): {skipped} ta.")


@admin_router.message(ImportWords.file)
async def import_wrong(m: Message):
    await m.answer("Iltimos, CSV fayl yuboring (yoki /cancel).")


# ----------------------------------------------------------------------------
# KUNLIK ESLATMALAR
# ----------------------------------------------------------------------------
async def reminder_loop(bot: Bot):
    """Har daqiqada tekshiradi: REMINDER_HOUR dan keyin har bir foydalanuvchiga kuniga 1 marta yuboradi."""
    while True:
        try:
            now = datetime.now(TZ)
            if now.hour >= REMINDER_HOUR:
                day = now.date().isoformat()
                users = await rows("SELECT user_id FROM users WHERE remind=1 AND "
                                   "(last_reminder IS NULL OR last_reminder<>?)", (day,))
                for u in users:
                    uid = u["user_id"]
                    await run("UPDATE users SET last_reminder=? WHERE user_id=?", (day, uid))
                    w = await pick_word(uid)
                    if not w:
                        break
                    await mark_seen(uid, w["id"])
                    text = "☀️ <b>Kunlik so'z</b>\n\n" + fmt_word(w)
                    t = await todays_task()
                    if t:
                        text += f"\n\n🎯 <b>Bugungi vazifa:</b> {esc(t['text'])}"
                    kb = None
                    if webapp_ok():
                        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                            text="📚 Mini App", web_app=WebAppInfo(url=WEBAPP_URL))]])
                    try:
                        await bot.send_message(uid, text, reply_markup=kb)
                    except TelegramForbiddenError:
                        await run("UPDATE users SET remind=0 WHERE user_id=?", (uid,))  # bot bloklangan
                    except Exception as e:
                        log.warning("Eslatma yuborilmadi (%s): %s", uid, e)
                    await asyncio.sleep(0.06)  # Telegram limitlari (~30 xabar/soniya)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reminder_loop xatosi")
        await asyncio.sleep(60)


# ----------------------------------------------------------------------------
# MINI APP SERVERI (aiohttp): statik sahifa + API
# ----------------------------------------------------------------------------
def verify_init_data(init_data: str):
    """Telegram initData imzosini tekshiradi va foydalanuvchini qaytaradi (yoki None)."""
    if DEV_MODE and not init_data:
        return {"id": 1, "first_name": "Dev", "username": "dev"}
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        recv_hash = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


async def auth(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise web.HTTPUnauthorized(text='{"error":"unauthorized"}', content_type="application/json")
    await ensure_user(user["id"], user.get("first_name"), user.get("username"))
    await touch(user["id"])
    return user["id"], user, data


async def api_me(request):
    uid, user, _ = await auth(request)
    u = await one("SELECT * FROM users WHERE user_id=?", (uid,))
    total = (await one("SELECT COUNT(*) c FROM words"))["c"]
    known = (await one("SELECT COUNT(*) c FROM progress WHERE user_id=? AND known=1", (uid,)))["c"]
    rank = (await one("SELECT COUNT(*)+1 c FROM users WHERE score>?", (u["score"],)))["c"]
    task = await todays_task()
    top = await rows("SELECT first_name,username,score FROM users ORDER BY score DESC LIMIT 10")
    return web.json_response({
        "name": user.get("first_name") or "do'st", "score": u["score"], "streak": u["streak"],
        "total": total, "known": known, "rank": rank, "task": task["text"] if task else None,
        "top": [{"name": t["first_name"] or t["username"] or "Foydalanuvchi", "score": t["score"]} for t in top]})


async def api_cards(request):
    uid, _, _ = await auth(request)
    ws = await rows(
        "SELECT w.id,w.english,w.uzbek,w.mnemonic,w.example FROM words w "
        "LEFT JOIN progress p ON p.word_id=w.id AND p.user_id=? "
        "ORDER BY COALESCE(p.known,0), RANDOM() LIMIT 20", (uid,))
    return web.json_response({"cards": [dict(w) for w in ws]})


async def api_answer(request):
    uid, _, data = await auth(request)
    known = bool(data.get("known"))
    if await one("SELECT 1 FROM words WHERE id=?", (int(data.get("word_id", 0)),)):
        await record(uid, int(data["word_id"]), known, 5 if known else 0)
    u = await one("SELECT score,streak FROM users WHERE user_id=?", (uid,))
    return web.json_response({"score": u["score"], "streak": u["streak"]})


async def api_quiz(request):
    uid, _, _ = await auth(request)
    qs = await make_questions(uid, 10)
    return web.json_response({"questions": [{"word_id": w["id"], "english": w["english"], "options": o} for w, o in qs]})


async def api_quiz_answer(request):
    uid, _, data = await auth(request)
    wid, opt = int(data.get("word_id", 0)), int(data.get("option_id", -1))
    w = await one("SELECT * FROM words WHERE id=?", (wid,))
    if not w:
        raise web.HTTPNotFound()
    ok = wid == opt
    await record(uid, wid, ok, 10 if ok else 0)
    u = await one("SELECT score,streak FROM users WHERE user_id=?", (uid,))
    return web.json_response({"correct": ok, "correct_id": wid, "uzbek": w["uzbek"], "mnemonic": w["mnemonic"],
                              "example": w["example"], "score": u["score"], "streak": u["streak"]})


async def api_memes(request):
    await auth(request)
    ms = await rows("SELECT id,caption FROM memes ORDER BY RANDOM() LIMIT 30")
    return web.json_response({"memes": [dict(m) for m in ms]})


async def meme_media(request: web.Request):
    """Meme rasmini Telegram'dan olib beradi (bot tokeni brauzerga chiqmaydi)."""
    if not request.match_info["id"].isdigit():
        raise web.HTTPNotFound()
    meme = await one("SELECT file_id FROM memes WHERE id=?", (int(request.match_info["id"]),))
    if not meme:
        raise web.HTTPNotFound()
    buf = io.BytesIO()
    try:
        await request.app["bot"].download(meme["file_id"], destination=buf)
    except Exception:
        raise web.HTTPNotFound()
    return web.Response(body=buf.getvalue(), content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


async def index(request):
    return web.FileResponse(BASE_DIR / "webapp" / "index.html", headers={"Cache-Control": "no-store"})


def build_web_app(bot: Bot) -> web.Application:
    app = web.Application(client_max_size=1_000_000)
    app["bot"] = bot
    app.router.add_get("/", index)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    for path, handler in (("/api/me", api_me), ("/api/cards", api_cards), ("/api/answer", api_answer),
                          ("/api/quiz", api_quiz), ("/api/quiz_answer", api_quiz_answer), ("/api/memes", api_memes)):
        app.router.add_post(path, handler)
    app.router.add_get("/media/meme/{id}", meme_media)
    return app


# ----------------------------------------------------------------------------
# ISHGA TUSHIRISH
# ----------------------------------------------------------------------------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylini to'ldiring.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS bo'sh - admin panelga hech kim kira olmaydi.")
    await init_db()

    # Barqaror ulanish: so'rov timeout'i 60 soniya, long-polling 30 soniya.
    # aiogram tarmoq xatolarida (Timed out va h.k.) o'zi avtomatik qayta urinadi.
    session = AiohttpSession(timeout=60)
    bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(common_router)
    dp.include_router(admin_router)
    dp.include_router(user_router)

    runner = web.AppRunner(build_web_app(bot))
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    log.info("Mini App serveri: http://%s:%s", HOST, PORT)

    for attempt in range(1, 6):  # webhook'ni o'chirish (vaqtinchalik xatolarda qayta urinish)
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            break
        except Exception as e:
            log.warning("delete_webhook xatosi (%s/5): %s", attempt, e)
            await asyncio.sleep(3 * attempt)

    try:
        await bot.set_my_commands([BotCommand(command="start", description="Boshlash"),
                                   BotCommand(command="word", description="Kunlik so'z"),
                                   BotCommand(command="test", description="Test"),
                                   BotCommand(command="task", description="Bugungi vazifa"),
                                   BotCommand(command="meme", description="Meme"),
                                   BotCommand(command="me", description="Natijam"),
                                   BotCommand(command="remind", description="Eslatma on/off")])
        for aid in ADMIN_IDS:  # adminlarga qo'shimcha buyruqlar menyusi
            await bot.set_my_commands(
                [BotCommand(command="admin", description="Admin panel"),
                 BotCommand(command="addword", description="So'z qo'shish"),
                 BotCommand(command="listwords", description="So'zlar ro'yxati"),
                 BotCommand(command="stats", description="Statistika"),
                 BotCommand(command="start", description="Boshlash")],
                scope=BotCommandScopeChat(chat_id=aid))
        if webapp_ok():
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="📚 O'rganish", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception as e:
        log.warning("Buyruqlar menyusini o'rnatib bo'lmadi: %s", e)

    reminder = asyncio.create_task(reminder_loop(bot))
    try:
        log.info("Bot ishga tushdi.")
        await dp.start_polling(bot, polling_timeout=30, allowed_updates=dp.resolve_used_update_types())
    finally:
        reminder.cancel()
        await runner.cleanup()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
