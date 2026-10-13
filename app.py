import os
import csv
import html
import telebot
import requests
import psycopg2
from contextlib import contextmanager
from urllib.parse import quote
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from telebot import types
from flask import Flask, request

# ───────────────────────── Настройки ─────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not BOT_TOKEN:
    try:
        from config import BOT_TOKEN as CFG_TOKEN
        BOT_TOKEN = CFG_TOKEN
    except ImportError:
        raise ValueError("Нет ни TELEGRAM_TOKEN, ни config.py")

# Разрешённые Telegram ID. Впиши сюда 3 ID ИЛИ задай переменную ALLOWED_IDS="1,2,3" на Render.
DEFAULT_ALLOWED_IDS = [
    840970840,    # я
    1069963014,   # коллега 1
    7527393497,   # коллега 2
]
_env_ids = os.environ.get("ALLOWED_IDS", "")
ALLOWED_IDS = set(DEFAULT_ALLOWED_IDS) | {
    int(x) for x in _env_ids.replace(" ", "").split(",") if x.strip().isdigit()
}
if not ALLOWED_IDS:
    print("[!] ALLOWED_IDS пуст: бот никого не пустит. Узнай ID командой /myid")

WEBHOOK_BASE = os.environ.get("RENDER_EXTERNAL_URL", "https://weather-bot-0v1m.onrender.com").rstrip("/")
WEBHOOK_URL = f"{WEBHOOK_BASE}/{BOT_TOKEN}"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

esc = html.escape

STATUS_LABELS = {
    "not_called": "⚪ Не звонили",
    "called": "📞 Дозвонились",
    "kp_sent": "📨 КП отправлено",
    "thinking": "🤔 Думает",
    "client": "✅ Клиент",
    "refused": "❌ Отказ",
    "skipped": "⏭ Пропущен",
}
STATUS_EMOJI = {
    "not_called": "⚪", "called": "📞", "kp_sent": "📨",
    "thinking": "🤔", "client": "✅", "refused": "❌", "skipped": "⏭",
}
RELEVANCE_LABEL = {"A": "⭐", "B": "○", "C": "·"}
PER_PAGE = 5
CITIES_PER_PAGE = 30

# Чаты, ожидающие ввод города для погоды
WEATHER_WAIT = set()

# ───────────────────────── База данных ─────────────────────────
_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(1, 5, DATABASE_URL, sslmode="require")
    return _pool


@contextmanager
def db(dict_cursor=False):
    pool = get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor) if dict_cursor else conn.cursor()
        try:
            yield cur
        finally:
            cur.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db():
    with db() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT,
                email TEXT,
                city TEXT,
                relevance TEXT,
                status TEXT DEFAULT 'not_called',
                note TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)
        sync_csv_to_db(cur)


def sync_csv_to_db(cur):
    """Добавляет в БД только новые компании из CSV (существующие и их статусы не трогает)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_cities.csv")
    if not os.path.exists(path):
        return
    cur.execute("SELECT name, COALESCE(city, ''), COALESCE(phone, '') FROM clients;")
    existing = {(r[0], r[1], r[2]) for r in cur.fetchall()}
    added = 0
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "Без названия").strip()
            phone = (row.get("allPhones") or row.get("phone") or "").strip()
            email = (row.get("email") or "").strip()
            city = (row.get("searchCity") or row.get("city") or "").strip()
            relevance = (row.get("relevance") or "").strip().upper()
            key = (name, city, phone)
            if key in existing:
                continue
            existing.add(key)
            cur.execute(
                "INSERT INTO clients (name, phone, email, city, relevance) VALUES (%s, %s, %s, %s, %s)",
                (name, phone, email, city, relevance),
            )
            added += 1
    if added:
        print(f"[i] Добавлено новых компаний из CSV: {added}")


REL_ORDER_SQL = "CASE relevance WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 ELSE 4 END"


def get_stats():
    with db() as cur:
        cur.execute("SELECT status, COUNT(*) FROM clients GROUP BY status;")
        stats = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM clients;")
        return cur.fetchone()[0], stats


def get_next_client(status="not_called"):
    with db(True) as cur:
        cur.execute(
            f"SELECT * FROM clients WHERE status = %s ORDER BY {REL_ORDER_SQL}, id LIMIT 1;",
            (status,),
        )
        return cur.fetchone()


def count_status(status):
    with db() as cur:
        cur.execute("SELECT COUNT(*) FROM clients WHERE status = %s;", (status,))
        return cur.fetchone()[0]


def set_status(client_id, status):
    if status not in STATUS_LABELS:
        return
    with db() as cur:
        cur.execute(
            "UPDATE clients SET status = %s, updated_at = NOW() WHERE id = %s;",
            (status, client_id),
        )


def get_client(cid):
    with db(True) as cur:
        cur.execute("SELECT * FROM clients WHERE id = %s;", (cid,))
        return cur.fetchone()


def get_cities():
    with db() as cur:
        cur.execute("SELECT DISTINCT city FROM clients WHERE city IS NOT NULL AND city <> '' ORDER BY city;")
        return [r[0] for r in cur.fetchall()]


def get_city_clients(city):
    with db(True) as cur:
        cur.execute(
            f"SELECT * FROM clients WHERE city = %s ORDER BY {REL_ORDER_SQL}, id;",
            (city,),
        )
        return cur.fetchall()


def count_relevance(items):
    return (
        sum(1 for x in items if x["relevance"] == "A"),
        sum(1 for x in items if x["relevance"] == "B"),
        sum(1 for x in items if x["relevance"] == "C"),
    )


# ───────────────────────── Вспомогательное ─────────────────────────
def safe_edit(call, text, markup=None):
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode="HTML",
        )
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            print(f"[!] edit_message_text: {e}")


def get_apify_balance():
    if not APIFY_TOKEN:
        return "⚠️ Токен Apify не настроен."
    try:
        r = requests.get(
            "https://api.apify.com/v2/users/me/limits",
            headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
            timeout=15,
        )
        d = r.json().get("data", {})
        used = d.get("current", {}).get("monthlyUsageUsd", 0) or 0
        limit = d.get("limits", {}).get("maxMonthlyUsageUsd") or d.get("maxMonthlyUsageUsd") or 5
        rem = limit - used
        pct = int(used / limit * 100) if limit else 0
        return (
            f"💰 <b>Баланс Apify</b>\n\n"
            f"Потрачено: <b>${used:.2f}</b> из ${limit:.2f}\n"
            f"Остаток:   <b>${rem:.2f}</b>  ({pct}%)\n\n"
            f"💡 Хватит на <b>~{int(rem / 0.15) if rem > 0 else 0}</b> городов\n"
            f"📅 Следующие $5 придут ~7-го числа"
        )
    except Exception as e:
        return f"⚠️ Ошибка: {esc(str(e))}"


# ───────────────────────── Клавиатуры ─────────────────────────
def main_menu():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("🌤 Погода", callback_data="menu:weather"),
        types.InlineKeyboardButton("🏢 Буровые", callback_data="menu:bur"),
    )
    m.add(
        types.InlineKeyboardButton("📋 CRM", callback_data="crm:menu"),
        types.InlineKeyboardButton("💰 Баланс", callback_data="menu:balance"),
    )
    return m


def back_to_menu_btn():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return m


def crm_menu():
    queue = count_status("not_called")
    skipped = count_status("skipped")
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(types.InlineKeyboardButton(f"📞 Обзвон ({queue})", callback_data="crm:next"))
    if skipped > 0:
        m.add(types.InlineKeyboardButton(f"⏭ Пропущенные ({skipped})", callback_data="crm:skipped"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return m


def client_card_markup(cid):
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("📞 Дозвонились", callback_data=f"crm:set:{cid}:called"),
        types.InlineKeyboardButton("📨 КП", callback_data=f"crm:set:{cid}:kp_sent"),
    )
    m.add(
        types.InlineKeyboardButton("🤔 Думает", callback_data=f"crm:set:{cid}:thinking"),
        types.InlineKeyboardButton("✅ Клиент", callback_data=f"crm:set:{cid}:client"),
    )
    m.add(
        types.InlineKeyboardButton("❌ Отказ", callback_data=f"crm:set:{cid}:refused"),
        types.InlineKeyboardButton("⏭ Пропустить", callback_data=f"crm:set:{cid}:skipped"),
    )
    m.add(types.InlineKeyboardButton("◀️ В CRM", callback_data="crm:menu"))
    return m


def skipped_card_markup(cid):
    # crm:sets — действие из списка пропущенных (после него показываем следующий пропущенный)
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("↩️ В очередь", callback_data=f"crm:sets:{cid}:not_called"),
        types.InlineKeyboardButton("📞 Дозвонились", callback_data=f"crm:sets:{cid}:called"),
    )
    m.add(
        types.InlineKeyboardButton("📨 КП", callback_data=f"crm:sets:{cid}:kp_sent"),
        types.InlineKeyboardButton("❌ Отказ", callback_data=f"crm:sets:{cid}:refused"),
    )
    m.add(types.InlineKeyboardButton("⏭ Следующий пропущенный", callback_data="crm:skipped"))
    m.add(types.InlineKeyboardButton("◀️ В CRM", callback_data="crm:menu"))
    return m


def cities_menu(page=0):
    cities = get_cities()
    total_pages = max(1, (len(cities) + CITIES_PER_PAGE - 1) // CITIES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * CITIES_PER_PAGE
    m = types.InlineKeyboardMarkup(row_width=3)
    btns = [
        types.InlineKeyboardButton(c, callback_data=f"city:{start + i}")
        for i, c in enumerate(cities[start:start + CITIES_PER_PAGE])
    ]
    if btns:
        m.add(*btns)
    if total_pages > 1:
        prev_btn = types.InlineKeyboardButton("◀️", callback_data=f"cities:{page - 1}") if page > 0 else types.InlineKeyboardButton("·", callback_data="noop")
        info = types.InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        nxt = types.InlineKeyboardButton("▶️", callback_data=f"cities:{page + 1}") if page < total_pages - 1 else types.InlineKeyboardButton("·", callback_data="noop")
        m.row(prev_btn, info, nxt)
    m.add(types.InlineKeyboardButton("◀️ В меню", callback_data="menu:start"))
    return len(cities), m


# ───────────────────────── Рендер ─────────────────────────
def render_companies(city_idx, page):
    cities = get_cities()
    if city_idx >= len(cities):
        return "Город не найден.", back_to_menu_btn()
    city = cities[city_idx]
    items = get_city_clients(city)
    total = len(items)
    a, b, c = count_relevance(items)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PER_PAGE
    chunk = items[start:start + PER_PAGE]

    text = f"🏢 <b>{esc(city)}</b> — всего {total}\n⭐ {a}  ○ {b}  · {c}\n\n"
    for i, item in enumerate(chunk, start=start + 1):
        mark = RELEVANCE_LABEL.get(item["relevance"], "")
        status_e = STATUS_EMOJI.get(item["status"], "")
        text += (
            f"{mark} <b>{i}. {esc(item['name'])}</b>  {status_e}\n"
            f"📞 {esc(item['phone'] or '—')}\n✉️ {esc(item['email'] or '—')}\n\n"
        )
    text += f"Страница {page + 1} из {total_pages}"

    m = types.InlineKeyboardMarkup(row_width=5)
    prev_btn = types.InlineKeyboardButton("◀️", callback_data=f"page:{city_idx}:{page - 1}") if page > 0 else types.InlineKeyboardButton("·", callback_data="noop")
    info = types.InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
    nxt = types.InlineKeyboardButton("▶️", callback_data=f"page:{city_idx}:{page + 1}") if page < total_pages - 1 else types.InlineKeyboardButton("·", callback_data="noop")
    m.row(prev_btn, info, nxt)
    num_btns = [
        types.InlineKeyboardButton(str(start + i + 1), callback_data=f"comp:{city_idx}:{page}:{item['id']}")
        for i, item in enumerate(chunk)
    ]
    if num_btns:
        m.row(*num_btns)
    m.add(types.InlineKeyboardButton("◀️ К городам", callback_data="menu:bur"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return text, m


def render_company_card(city_idx, page, cid):
    client = get_client(cid)
    back = types.InlineKeyboardMarkup(row_width=2)
    if not client:
        back.add(types.InlineKeyboardButton("↩️ К списку", callback_data=f"page:{city_idx}:{page}"))
        return "Компания не найдена.", back

    status = STATUS_LABELS.get(client["status"], client["status"])
    mark = RELEVANCE_LABEL.get(client["relevance"], "")
    text = (
        f"{mark} <b>{esc(client['name'])}</b>\n\n"
        f"🏙 {esc(client['city'] or '—')}\n📞 {esc(client['phone'] or '—')}\n"
        f"✉️ {esc(client['email'] or '—')}\n\n"
        f"📊 Статус: {status}\n\n"
        f"Выбери новое действие:"
    )
    p = f"{city_idx}:{page}:{cid}"
    back.add(
        types.InlineKeyboardButton("📞 Дозвонились", callback_data=f"cs:{p}:called"),
        types.InlineKeyboardButton("📨 КП", callback_data=f"cs:{p}:kp_sent"),
    )
    back.add(
        types.InlineKeyboardButton("🤔 Думает", callback_data=f"cs:{p}:thinking"),
        types.InlineKeyboardButton("✅ Клиент", callback_data=f"cs:{p}:client"),
    )
    back.add(
        types.InlineKeyboardButton("❌ Отказ", callback_data=f"cs:{p}:refused"),
        types.InlineKeyboardButton("⏭ Пропустить", callback_data=f"cs:{p}:skipped"),
    )
    back.add(types.InlineKeyboardButton("↩️ К списку", callback_data=f"page:{city_idx}:{page}"))
    back.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return text, back


def render_client_card(client):
    if not client:
        skipped = count_status("skipped")
        m = types.InlineKeyboardMarkup()
        if skipped > 0:
            m.add(types.InlineKeyboardButton(f"⏭ К пропущенным ({skipped})", callback_data="crm:skipped"))
        m.add(types.InlineKeyboardButton("◀️ В CRM", callback_data="crm:menu"))
        return "✅ <b>Очередь пуста!</b>", m
    mark = RELEVANCE_LABEL.get(client["relevance"], "")
    status = STATUS_LABELS.get(client["status"], client["status"])
    queue = count_status("not_called")
    text = (
        f"{mark} <b>{esc(client['name'])}</b>\n\n🏙 {esc(client['city'] or '—')}\n"
        f"📞 {esc(client['phone'] or '—')}\n✉️ {esc(client['email'] or '—')}\n"
        f"📊 Статус: {status}\n📋 Осталось: <b>{queue}</b>\n\nВыбери действие:"
    )
    return text, client_card_markup(client["id"])


def render_skipped_card(client):
    if not client:
        return "✅ Пропущенных нет!", crm_menu()
    skipped = count_status("skipped")
    text = (
        f"⏭ <b>{esc(client['name'])}</b>\n\n🏙 {esc(client['city'] or '—')}\n"
        f"📞 {esc(client['phone'] or '—')}\n✉️ {esc(client['email'] or '—')}\n"
        f"📊 {STATUS_LABELS.get(client['status'], client['status'])}\n"
        f"⏭ Всего пропущено: <b>{skipped}</b>"
    )
    return text, skipped_card_markup(client["id"])


def show_crm_menu(call):
    total, stats = get_stats()
    text = "📋 <b>CRM — учёт клиентов</b>\n\n" + f"Всего: <b>{total}</b>\n\n"
    for key, label in STATUS_LABELS.items():
        text += f"{label}: <b>{stats.get(key, 0)}</b>\n"
    safe_edit(call, text, crm_menu())


# ───────────────────────── Команды ─────────────────────────
@bot.message_handler(commands=['myid'])
def cmd_myid(message):
    bot.send_message(message.chat.id, f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@bot.message_handler(commands=['start'])
def cmd_start(message):
    WEATHER_WAIT.discard(message.chat.id)
    bot.send_message(
        message.chat.id,
        f"Привет, {esc(message.from_user.first_name or '')}! Выбери раздел:",
        reply_markup=main_menu(), parse_mode="HTML",
    )


# ───────────────────────── Меню ─────────────────────────
@bot.callback_query_handler(func=lambda call: call.data == "menu:start")
def cb_menu_start(call):
    bot.answer_callback_query(call.id)
    WEATHER_WAIT.discard(call.message.chat.id)
    safe_edit(call, "Выбери раздел:", main_menu())


@bot.callback_query_handler(func=lambda call: call.data == "menu:weather")
def cb_menu_weather(call):
    bot.answer_callback_query(call.id)
    WEATHER_WAIT.add(call.message.chat.id)
    safe_edit(call, "🌤 Напиши город:", back_to_menu_btn())


@bot.callback_query_handler(func=lambda call: call.data == "menu:balance")
def cb_menu_balance(call):
    bot.answer_callback_query(call.id, "Считаю...")
    text = get_apify_balance()
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="menu:balance"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    safe_edit(call, text, m)


@bot.callback_query_handler(func=lambda call: call.data == "menu:bur" or call.data.startswith("cities:"))
def cb_menu_bur(call):
    bot.answer_callback_query(call.id)
    page = int(call.data.split(":")[1]) if call.data.startswith("cities:") else 0
    count, markup = cities_menu(page)
    if not count:
        safe_edit(call, "База пуста.", back_to_menu_btn())
        return
    safe_edit(call, f"🏢 Выбери город ({count}):", markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("city:"))
def cb_city(call):
    bot.answer_callback_query(call.id)
    idx = int(call.data.split(":")[1])
    text, markup = render_companies(idx, 0)
    safe_edit(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("page:"))
def cb_page(call):
    bot.answer_callback_query(call.id)
    _, city_idx, page = call.data.split(":")
    text, markup = render_companies(int(city_idx), int(page))
    safe_edit(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("comp:"))
def cb_comp(call):
    bot.answer_callback_query(call.id)
    _, city_idx, page, cid = call.data.split(":")
    text, markup = render_company_card(int(city_idx), int(page), int(cid))
    safe_edit(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("cs:"))
def cb_cs(call):
    _, city_idx, page, cid, status = call.data.split(":")
    set_status(int(cid), status)
    bot.answer_callback_query(call.id, f"Статус: {STATUS_LABELS.get(status, status)}")
    text, markup = render_company_card(int(city_idx), int(page), int(cid))
    safe_edit(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data == "noop")
def cb_noop(call):
    bot.answer_callback_query(call.id)


# ───────────────────────── CRM ─────────────────────────
@bot.callback_query_handler(func=lambda call: call.data == "crm:menu")
def cb_crm_menu(call):
    bot.answer_callback_query(call.id)
    show_crm_menu(call)


@bot.callback_query_handler(func=lambda call: call.data == "crm:next")
def cb_crm_next(call):
    bot.answer_callback_query(call.id)
    text, markup = render_client_card(get_next_client("not_called"))
    safe_edit(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data == "crm:skipped")
def cb_crm_skipped(call):
    bot.answer_callback_query(call.id)
    text, markup = render_skipped_card(get_next_client("skipped"))
    safe_edit(call, text, markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("crm:set:") or call.data.startswith("crm:sets:"))
def cb_crm_set(call):
    parts = call.data.split(":")
    from_skipped = parts[1] == "sets"
    cid, status = int(parts[2]), parts[3]
    set_status(cid, status)
    bot.answer_callback_query(call.id, f"Статус: {STATUS_LABELS.get(status, status)}")
    if from_skipped:
        text, markup = render_skipped_card(get_next_client("skipped"))
    else:
        text, markup = render_client_card(get_next_client("not_called"))
    safe_edit(call, text, markup)


# ───────────────────────── Погода ─────────────────────────
WEATHER_DESC = {0: "Ясно ☀️", 1: "В основном ясно 🌤️", 2: "Переменная облачность ⛅", 3: "Пасмурно ☁️", 45: "Туман 🌫️", 48: "Иней 🌫️", 51: "Морось 🌦️", 53: "Морось 🌦️", 55: "Морось 🌦️", 61: "Дождь 🌧️", 63: "Дождь 🌧️", 65: "Ливень 🌧️", 71: "Снег ❄️", 73: "Снег ❄️", 75: "Сильный снег ❄️", 80: "Ливень 🌦️", 81: "Ливень 🌦️", 82: "Сильный ливень 🌦️", 95: "Гроза ⛈️", 96: "Гроза с градом ⛈️", 99: "Гроза с градом ⛈️"}
WTTR = {"Sunny": "Ясно ☀️", "Clear": "Ясно ☀️", "Partly cloudy": "Переменная облачность ⛅", "Cloudy": "Облачно ☁️", "Overcast": "Пасмурно ☁️", "Mist": "Дымка 🌫️", "Fog": "Туман 🌫️", "Patchy rain nearby": "Местами дождь 🌦️", "Light rain": "Небольшой дождь 🌧️", "Moderate rain": "Дождь 🌧️", "Heavy rain": "Сильный дождь 🌧️", "Light drizzle": "Слабая морось 🌦️", "Light snow": "Небольшой снег ❄️", "Moderate snow": "Снег ❄️", "Heavy snow": "Сильный снег ❄️", "Blizzard": "Метель ❄️", "Sleet": "Мокрый снег 🌨️", "Thundery outbreaks possible": "Возможны грозы ⛈️", "Light rain shower": "Небольшой ливень 🌦️", "Moderate or heavy rain shower": "Сильный ливень 🌧️"}
COUNTRY_RU = {"Russia": "Россия", "Kazakhstan": "Казахстан", "Ukraine": "Украина", "Belarus": "Беларусь", "Norway": "Норвегия", "Sweden": "Швеция", "Finland": "Финляндия", "Denmark": "Дания", "Iceland": "Исландия", "Germany": "Германия", "France": "Франция", "Italy": "Италия", "Spain": "Испания", "Poland": "Польша", "Netherlands": "Нидерланды", "Turkey": "Турция", "China": "Китай", "Japan": "Япония", "USA": "США", "United States of America": "США", "United Kingdom": "Великобритания", "Canada": "Канада", "Georgia": "Грузия", "Armenia": "Армения", "Azerbaijan": "Азербайджан", "Uzbekistan": "Узбекистан", "Kyrgyzstan": "Кыргызстан", "Tajikistan": "Таджикистан", "Mongolia": "Монголия", "Egypt": "Египет"}


def try_open_meteo(city):
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "ru"}, timeout=10,
        ).json()
        if not geo.get("results"):
            return "NOT_FOUND"
        loc = geo["results"][0]
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": loc["latitude"], "longitude": loc["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "timezone": "auto",
            },
            timeout=10,
        ).json()
        if "current" not in w:
            return None
        c = w["current"]
        country = COUNTRY_RU.get(loc.get("country", ""), loc.get("country", ""))
        return (
            f"📍 <b>{esc(loc['name'])}</b>, {esc(country)}\n"
            f"Погода: {WEATHER_DESC.get(c['weather_code'], '—')}\n"
            f"🌡️ <b>{c['temperature_2m']}°C</b>\n💧 {c['relative_humidity_2m']}%\n💨 {c['wind_speed_10m']} км/ч"
        )
    except Exception as e:
        print(e)
        return None


def try_wttr(city):
    try:
        r = requests.get(
            f"https://wttr.in/{quote(city)}", params={"format": "j1", "lang": "ru"}, timeout=15
        ).json()
        if "current_condition" not in r:
            return None
        c = r["current_condition"][0]
        desc = c["lang_ru"][0]["value"] if c.get("lang_ru") else c["weatherDesc"][0]["value"]
        desc = WTTR.get(desc, desc)
        country = ""
        if r.get("nearest_area"):
            a = r["nearest_area"][0]
            if a.get("country"):
                raw = a["country"][0]["value"]
                country = COUNTRY_RU.get(raw, raw)
        return (
            f"📍 <b>{esc(city.capitalize())}</b>, {esc(country)}\n"
            f"Погода: {esc(desc)}\n🌡️ <b>{c.get('temp_C', '?')}°C</b>\n"
            f"💧 {c.get('humidity', '?')}%\n💨 {c.get('windspeedKmph', '?')} км/ч"
        )
    except Exception as e:
        print(e)
        return None


@bot.message_handler(content_types=['text'], func=lambda m: True)
def on_text(message):
    # Погоду показываем только если нажата кнопка «🌤 Погода»
    if message.chat.id not in WEATHER_WAIT:
        bot.send_message(message.chat.id, "Выбери раздел:", reply_markup=main_menu())
        return
    try:
        city = message.text.strip()
        res = try_open_meteo(city)
        if res is None or res == "NOT_FOUND":
            res = try_wttr(city)
        if res is None:
            bot.reply_to(message, f"Не нашёл '{esc(city)}'.", parse_mode="HTML", reply_markup=back_to_menu_btn())
            return
        bot.reply_to(message, res, parse_mode="HTML", reply_markup=back_to_menu_btn())
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {esc(str(e))}", parse_mode="HTML", reply_markup=back_to_menu_btn())


# ───────────────────────── Webhook + контроль доступа ─────────────────────────
def is_allowed(update):
    """Пропускаем только обновления от разрешённых ID (/myid доступен всем)."""
    if update.message and update.message.from_user:
        text = update.message.text or ""
        if text.split("@")[0].strip() == "/myid":
            return True
        return update.message.from_user.id in ALLOWED_IDS
    if update.callback_query and update.callback_query.from_user:
        return update.callback_query.from_user.id in ALLOWED_IDS
    return False


@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') != 'application/json':
        return 'Forbidden', 403
    try:
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        if not is_allowed(update):
            if update.callback_query:
                try:
                    bot.answer_callback_query(update.callback_query.id, "⛔ Нет доступа", show_alert=True)
                except Exception:
                    pass
            return '', 200
        bot.process_new_updates([update])
    except Exception as e:
        print(f"[!] Ошибка обработки update: {e}")
    return '', 200  # всегда 200, чтобы Telegram не слал повторы


@app.route('/')
def index():
    return "Bot is running!"


@app.route('/health')
def health():
    return "OK"


if __name__ == "__main__":
    try:
        init_db()
        print("[i] БД инициализирована")
    except Exception as e:
        print(f"[!] Ошибка БД: {e}")
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
