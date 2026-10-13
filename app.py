import os
import csv
import html
import urllib.parse
import telebot
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from telebot import types
from flask import Flask, request

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not BOT_TOKEN:
    try:
        from config import BOT_TOKEN as CFG_TOKEN
        BOT_TOKEN = CFG_TOKEN
    except ImportError:
        raise ValueError("Нет ни TELEGRAM_TOKEN, ни config.py")

# ==== БЕЛЫЙ СПИСОК ДОСТУПА ====
ALLOWED_IDS = {840970840, 1069963014, 7527393497}

def is_allowed(user_id):
    return user_id in ALLOWED_IDS

def denied_message(chat_id):
    bot.send_message(chat_id, "⛔ Доступ закрыт. Обратитесь к владельцу бота.")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

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

def db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
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
            cur.execute("SELECT COUNT(*) FROM clients;")
            if cur.fetchone()[0] == 0:
                print("[i] Заливаю компании из CSV в базу...")
                load_csv_to_db(cur)
        conn.commit()
    finally:
        conn.close()

def load_csv_to_db(cur):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_cities.csv")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            cur.execute(
                "INSERT INTO clients (name, phone, email, city, relevance) VALUES (%s, %s, %s, %s, %s)",
                (
                    (row.get("name") or "Без названия").strip(),
                    (row.get("allPhones") or row.get("phone") or "").strip(),
                    (row.get("email") or "").strip(),
                    (row.get("searchCity") or row.get("city") or "").strip(),
                    (row.get("relevance") or "").strip().upper(),
                )
            )

def get_stats():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM clients GROUP BY status;")
            stats = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM clients;")
            return cur.fetchone()[0], stats
    finally:
        conn.close()

def get_next_client(status="not_called"):
    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM clients WHERE status = %s
                ORDER BY CASE relevance WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 ELSE 4 END, id
                LIMIT 1;
            """, (status,))
            return cur.fetchone()
    finally:
        conn.close()

def get_queue_count():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM clients WHERE status = 'not_called';")
            return cur.fetchone()[0]
    finally:
        conn.close()

def get_skipped_count():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM clients WHERE status = 'skipped';")
            return cur.fetchone()[0]
    finally:
        conn.close()

def set_status(client_id, status):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE clients SET status = %s, updated_at = NOW() WHERE id = %s;", (status, client_id))
        conn.commit()
    finally:
        conn.close()

def get_statuses_for_page(city, names):
    """Один запрос: получить статусы для списка названий в городе."""
    if not names:
        return {}
    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, status FROM clients WHERE city = %s AND name = ANY(%s);",
                (city, names)
            )
            result = {}
            for row in cur.fetchall():
                # Если дубли имён — оставим первый
                if row["name"] not in result:
                    result[row["name"]] = {"id": row["id"], "status": row["status"]}
            return result
    finally:
        conn.close()

def get_client_by_name_city(name, city):
    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM clients WHERE name = %s AND city = %s LIMIT 1;", (name, city))
            return cur.fetchone()
    finally:
        conn.close()

RELEVANCE_LABEL = {"A": "⭐", "B": "○", "C": "·"}
RELEVANCE_ORDER = {"A": 0, "B": 1, "C": 2, "": 3}

def load_bur_data():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_cities.csv")
    data = {}
    if not os.path.exists(path):
        return data
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            city = (row.get("searchCity") or row.get("city") or "").strip()
            if not city:
                continue
            data.setdefault(city, []).append({
                "name": (row.get("name") or "Без названия").strip(),
                "phone": (row.get("allPhones") or row.get("phone") or "—").strip(),
                "email": (row.get("email") or "—").strip(),
                "relevance": (row.get("relevance") or "").strip().upper(),
            })
    for city in data:
        data[city].sort(key=lambda x: RELEVANCE_ORDER.get(x["relevance"], 3))
    return data

BUR_DATA = load_bur_data()
CITIES = sorted(BUR_DATA.keys())
PER_PAGE = 5

def count_relevance(items):
    return (
        sum(1 for x in items if x["relevance"] == "A"),
        sum(1 for x in items if x["relevance"] == "B"),
        sum(1 for x in items if x["relevance"] == "C"),
    )

def get_apify_balance():
    if not APIFY_TOKEN:
        return "⚠️ Токен Apify не настроен."
    try:
        url = f"https://api.apify.com/v2/users/me/limits?token={APIFY_TOKEN}"
        d = requests.get(url, timeout=15).json().get("data", {})
        used = d.get("current", {}).get("monthlyUsageUsd", 0)
        limit = d.get("maxMonthlyUsageUsd", 5)
        rem = limit - used
        percent = int(used / limit * 100) if limit > 0 else 0
        cities_left = int(rem / 0.15) if rem > 0 else 0
        return (
            f"💰 <b>Баланс Apify</b>\n\n"
            f"Потрачено: <b>${used:.2f}</b> из ${limit:.2f}\n"
            f"Остаток:   <b>${rem:.2f}</b>  ({percent}%)\n\n"
            f"💡 Хватит на <b>~{cities_left}</b> городов\n"
            f"📅 Следующие $5 придут ~7-го числа"
        )
    except Exception as e:
        return f"⚠️ Ошибка: {e}"

def safe_edit(call, text, markup=None, parse_mode=None):
    """Edit сообщения с защитой от 'message is not modified'."""
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode=parse_mode
        )
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            print(f"edit error: {e}")
    except Exception as e:
        print(f"edit error: {e}")

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
    queue = get_queue_count()
    skipped = get_skipped_count()
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(types.InlineKeyboardButton(f"📞 Обзвон ({queue})", callback_data="crm:next"))
    if skipped > 0:
        m.add(types.InlineKeyboardButton(f"⏭ Пропущенные ({skipped})", callback_data="crm:skipped"))
    m.add(types.InlineKeyboardButton("📊 Статистика", callback_data="crm:stats"))
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
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("↩️ В очередь", callback_data=f"crm:set:{cid}:not_called"),
        types.InlineKeyboardButton("📞 Дозвонились", callback_data=f"crm:set:{cid}:called"),
    )
    m.add(
        types.InlineKeyboardButton("📨 КП", callback_data=f"crm:set:{cid}:kp_sent"),
        types.InlineKeyboardButton("❌ Отказ", callback_data=f"crm:set:{cid}:refused"),
    )
    m.add(types.InlineKeyboardButton("⏭ Следующий пропущенный", callback_data="crm:skipped"))
    m.add(types.InlineKeyboardButton("◀️ В CRM", callback_data="crm:menu"))
    return m

def cities_menu():
    m = types.InlineKeyboardMarkup(row_width=3)
    btns = [types.InlineKeyboardButton(c, callback_data=f"city:{i}") for i, c in enumerate(CITIES)]
    if btns:
        m.add(*btns)
    m.add(types.InlineKeyboardButton("◀️ В меню", callback_data="menu:start"))
    return m

def render_companies(city_idx, page):
    city = CITIES[city_idx]
    items = BUR_DATA[city]
    total = len(items)
    a, b, c = count_relevance(items)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PER_PAGE
    chunk = items[start:start + PER_PAGE]

    # Один запрос на страницу — статусы всех компаний
    names = [item["name"] for item in chunk]
    statuses = get_statuses_for_page(city, names)

    text = f"🏢 <b>{html.escape(city)}</b> — всего {total}\n⭐ {a}  ○ {b}  · {c}\n\n"
    for i, item in enumerate(chunk, start=start + 1):
        mark = RELEVANCE_LABEL.get(item["relevance"], "")
        st = statuses.get(item["name"], {})
        status_e = STATUS_EMOJI.get(st.get("status", ""), "") if st else ""
        name_esc = html.escape(item["name"])
        phone_esc = html.escape(item["phone"])
        email_esc = html.escape(item["email"])
        text += f"{mark} <b>{i}. {name_esc}</b>  {status_e}\n📞 {phone_esc}\n✉️ {email_esc}\n\n"
    text += f"Страница {page + 1} из {total_pages}"

    m = types.InlineKeyboardMarkup(row_width=3)
    prev_btn = types.InlineKeyboardButton("◀️", callback_data=f"page:{city_idx}:{page-1}") if page > 0 else types.InlineKeyboardButton("·", callback_data="noop")
    info = types.InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop")
    nxt = types.InlineKeyboardButton("▶️", callback_data=f"page:{city_idx}:{page+1}") if page < total_pages - 1 else types.InlineKeyboardButton("·", callback_data="noop")
    m.add(prev_btn, info, nxt)

    num_btns = []
    for i in range(len(chunk)):
        global_idx = start + i
        num_btns.append(types.InlineKeyboardButton(str(i + 1), callback_data=f"comp:{city_idx}:{global_idx}"))
    if num_btns:
        m.add(*num_btns)

    m.add(types.InlineKeyboardButton("◀️ К городам", callback_data="menu:bur"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return text, m

def render_company_card(city_idx, global_idx):
    city = CITIES[city_idx]
    items = BUR_DATA[city]
    item = items[global_idx]
    page = global_idx // PER_PAGE

    client = get_client_by_name_city(item["name"], city)
    if client:
        status = STATUS_LABELS.get(client["status"], client["status"])
        cid = client["id"]
    else:
        status = "—"
        cid = None

    mark = RELEVANCE_LABEL.get(item["relevance"], "")
    text = (
        f"{mark} <b>{html.escape(item['name'])}</b>\n\n"
        f"🏙 {html.escape(city)}\n📞 {html.escape(item['phone'])}\n✉️ {html.escape(item['email'])}\n\n"
        f"📊 Статус: {status}\n\n"
        f"Выбери новое действие:"
    )
    m = types.InlineKeyboardMarkup(row_width=2)
    if cid:
        m.add(
            types.InlineKeyboardButton("📞 Дозвонились", callback_data=f"cs:{city_idx}:{global_idx}:called"),
            types.InlineKeyboardButton("📨 КП", callback_data=f"cs:{city_idx}:{global_idx}:kp_sent"),
        )
        m.add(
            types.InlineKeyboardButton("🤔 Думает", callback_data=f"cs:{city_idx}:{global_idx}:thinking"),
            types.InlineKeyboardButton("✅ Клиент", callback_data=f"cs:{city_idx}:{global_idx}:client"),
        )
        m.add(
            types.InlineKeyboardButton("❌ Отказ", callback_data=f"cs:{city_idx}:{global_idx}:refused"),
            types.InlineKeyboardButton("⏭ Пропустить", callback_data=f"cs:{city_idx}:{global_idx}:skipped"),
        )
    m.add(types.InlineKeyboardButton("↩️ К списку", callback_data=f"page:{city_idx}:{page}"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return text, m

@bot.message_handler(commands=['start'])
def cmd_start(message):
    if not is_allowed(message.from_user.id):
        denied_message(message.chat.id)
        return
    bot.send_message(message.chat.id, f"Привет, {html.escape(message.from_user.first_name)}! Выбери раздел:", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def guard(call):
    if not is_allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Доступ закрыт")
        return
    return False

@bot.callback_query_handler(func=lambda call: call.data == "menu:start")
def cb_menu_start(call):
    bot.answer_callback_query(call.id)
    safe_edit(call, "Выбери раздел:", main_menu())

@bot.callback_query_handler(func=lambda call: call.data == "menu:weather")
def cb_menu_weather(call):
    bot.answer_callback_query(call.id)
    safe_edit(call, "🌤 Напиши город:", back_to_menu_btn())

@bot.callback_query_handler(func=lambda call: call.data == "menu:balance")
def cb_menu_balance(call):
    bot.answer_callback_query(call.id, "Считаю...")
    text = get_apify_balance()
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="menu:balance"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    safe_edit(call, text, m, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "menu:bur")
def cb_menu_bur(call):
    bot.answer_callback_query(call.id)
    if not CITIES:
        safe_edit(call, "База пуста.", back_to_menu_btn())
        return
    safe_edit(call, f"🏢 Выбери город ({len(CITIES)}):", cities_menu())

@bot.callback_query_handler(func=lambda call: call.data.startswith("city:"))
def cb_city(call):
    bot.answer_callback_query(call.id)
    idx = int(call.data.split(":")[1])
    text, markup = render_companies(idx, 0)
    safe_edit(call, text, markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("page:"))
def cb_page(call):
    bot.answer_callback_query(call.id)
    _, city_idx, page = call.data.split(":")
    text, markup = render_companies(int(city_idx), int(page))
    safe_edit(call, text, markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("comp:"))
def cb_comp(call):
    bot.answer_callback_query(call.id)
    _, city_idx, global_idx = call.data.split(":")
    text, markup = render_company_card(int(city_idx), int(global_idx))
    safe_edit(call, text, markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("cs:"))
def cb_cs(call):
    _, city_idx, global_idx, status = call.data.split(":")
    city_idx, global_idx = int(city_idx), int(global_idx)
    city = CITIES[city_idx]
    item = BUR_DATA[city][global_idx]
    client = get_client_by_name_city(item["name"], city)
    if client:
        set_status(client["id"], status)
        bot.answer_callback_query(call.id, f"Статус: {STATUS_LABELS.get(status, status)}")
    else:
        bot.answer_callback_query(call.id, "Не найден в CRM")
    text, markup = render_company_card(city_idx, global_idx)
    safe_edit(call, text, markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "noop")
def cb_noop(call):
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "crm:menu")
def cb_crm_menu(call):
    bot.answer_callback_query(call.id)
    total, stats = get_stats()
    text = "📋 <b>CRM — учёт клиентов</b>\n\n" + f"Всего: <b>{total}</b>\n\n"
    for key, label in STATUS_LABELS.items():
        text += f"{label}: <b>{stats.get(key, 0)}</b>\n"
    safe_edit(call, text, crm_menu(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "crm:stats")
def cb_crm_stats(call):
    bot.answer_callback_query(call.id)
    total, stats = get_stats()
    text = "📊 <b>Статистика CRM</b>\n\n" + f"Всего: <b>{total}</b>\n\n"
    for key, label in STATUS_LABELS.items():
        text += f"{label}: <b>{stats.get(key, 0)}</b>\n"
    safe_edit(call, text, crm_menu(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "crm:next")
def cb_crm_next(call):
    bot.answer_callback_query(call.id)
    client = get_next_client("not_called")
    text, markup = render_client_card(client)
    safe_edit(call, text, markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "crm:skipped")
def cb_crm_skipped(call):
    bot.answer_callback_query(call.id)
    client = get_next_client("skipped")
    text, markup = render_skipped_card(client)
    safe_edit(call, text, markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("crm:set:"))
def cb_crm_set(call):
    _, _, cid, status = call.data.split(":")
    set_status(int(cid), status)
    bot.answer_callback_query(call.id, f"Статус: {STATUS_LABELS.get(status, status)}")
    if status == "not_called":
        client = get_next_client("skipped")
        text, markup = render_skipped_card(client)
    else:
        client = get_next_client("not_called")
        text, markup = render_client_card(client)
    safe_edit(call, text, markup, parse_mode="HTML")

def render_client_card(client):
    if not client:
        skipped = get_skipped_count()
        m = types.InlineKeyboardMarkup()
        if skipped > 0:
            m.add(types.InlineKeyboardButton(f"⏭ К пропущенным ({skipped})", callback_data="crm:skipped"))
        m.add(types.InlineKeyboardButton("◀️ В CRM", callback_data="crm:menu"))
        return ("✅ <b>Очередь пуста!</b>", m)
    mark = RELEVANCE_LABEL.get(client["relevance"], "")
    status = STATUS_LABELS.get(client["status"], client["status"])
    queue = get_queue_count()
    text = (
        f"{mark} <b>{html.escape(client['name'])}</b>\n\n"
        f"🏙 {html.escape(client['city'] or '—')}\n"
        f"📞 {html.escape(client['phone'] or '—')}\n"
        f"✉️ {html.escape(client['email'] or '—')}\n"
        f"📊 Статус: {status}\n"
        f"📋 Осталось: <b>{queue}</b>\n\nВыбери действие:"
    )
    return text, client_card_markup(client["id"])

def render_skipped_card(client):
    if not client:
        return ("✅ Пропущенных нет!", crm_menu())
    skipped = get_skipped_count()
    text = (
        f"⏭ <b>{html.escape(client['name'])}</b>\n\n"
        f"🏙 {html.escape(client['city'] or '—')}\n"
        f"📞 {html.escape(client['phone'] or '—')}\n"
        f"✉️ {html.escape(client['email'] or '—')}\n"
        f"📊 {STATUS_LABELS.get(client['status'], client['status'])}\n"
        f"⏭ Всего пропущено: <b>{skipped}</b>"
    )
    return text, skipped_card_markup(client["id"])

WEATHER_DESC = {0:"Ясно ☀️",1:"В основном ясно 🌤️",2:"Переменная облачность ⛅",3:"Пасмурно ☁️",45:"Туман 🌫️",48:"Иней 🌫️",51:"Морось 🌦️",53:"Морось 🌦️",55:"Морось 🌦️",61:"Дождь 🌧️",63:"Дождь 🌧️",65:"Ливень 🌧️",71:"Снег ❄️",73:"Снег ❄️",75:"Сильный снег ❄️",80:"Ливень 🌦️",81:"Ливень 🌦️",82:"Сильный ливень 🌦️",95:"Гроза ⛈️",96:"Гроза с градом ⛈️",99:"Гроза с градом ⛈️"}
WTTR = {"Sunny":"Ясно ☀️","Clear":"Ясно ☀️","Partly cloudy":"Переменная облачность ⛅","Cloudy":"Облачно ☁️","Overcast":"Пасмурно ☁️","Mist":"Дымка 🌫️","Fog":"Туман 🌫️","Patchy rain nearby":"Местами дождь 🌦️","Light rain":"Небольшой дождь 🌧️","Moderate rain":"Дождь 🌧️","Heavy rain":"Сильный дождь 🌧️","Light drizzle":"Слабая морось 🌦️","Light snow":"Небольшой снег ❄️","Moderate snow":"Снег ❄️","Heavy snow":"Сильный снег ❄️","Blizzard":"Метель ❄️","Sleet":"Мокрый снег 🌨️","Thundery outbreaks possible":"Возможны грозы ⛈️","Light rain shower":"Небольшой ливень 🌦️","Moderate or heavy rain shower":"Сильный ливень 🌧️"}
COUNTRY_RU = {"Russia":"Россия","Kazakhstan":"Казахстан","Ukraine":"Украина","Belarus":"Беларусь","Norway":"Норвегия","Sweden":"Швеция","Finland":"Финляндия","Denmark":"Дания","Iceland":"Исландия","Germany":"Германия","France":"Франция","Italy":"Италия","Spain":"Испания","Poland":"Польша","Netherlands":"Нидерланды","Turkey":"Турция","China":"Китай","Japan":"Япония","USA":"США","United States of America":"США","United Kingdom":"Великобритания","Canada":"Канада","Georgia":"Грузия","Armenia":"Армения","Azerbaijan":"Азербайджан","Uzbekistan":"Узбекистан","Kyrgyzstan":"Кыргызстан","Tajikistan":"Таджикистан","Mongolia":"Монголия","Egypt":"Египет"}

def try_open_meteo(city):
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "ru"},
            timeout=10
        ).json()
        if not geo.get("results"):
            return "NOT_FOUND"
        loc = geo["results"][0]
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "timezone": "auto"
            },
            timeout=10
        ).json()
        if "current" not in w:
            return None
        c = w["current"]
        country = COUNTRY_RU.get(loc.get("country", ""), loc.get("country", ""))
        return (
            f"📍 <b>{html.escape(loc['name'])}</b>, {html.escape(country)}\n"
            f"Погода: {WEATHER_DESC.get(c['weather_code'], '—')}\n"
            f"🌡️ <b>{c['temperature_2m']}°C</b>\n"
            f"💧 {c['relative_humidity_2m']}%\n"
            f"💨 {c['wind_speed_10m']} км/ч"
        )
    except Exception as e:
        print(f"open-meteo: {e}")
        return None

def try_wttr(city):
    try:
        safe_city = urllib.parse.quote(city)
        r = requests.get(f"https://wttr.in/{safe_city}", params={"format": "j1", "lang": "ru"}, timeout=15).json()
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
            f"📍 <b>{html.escape(city.capitalize())}</b>, {html.escape(country)}\n"
            f"Погода: {desc}\n"
            f"🌡️ <b>{c.get('temp_C','?')}°C</b>\n"
            f"💧 {c.get('humidity','?')}%\n"
            f"💨 {c.get('windspeedKmph','?')} км/ч"
        )
    except Exception as e:
        print(f"wttr: {e}")
        return None

@bot.message_handler(func=lambda m: True)
def get_weather(message):
    if not is_allowed(message.from_user.id):
        denied_message(message.chat.id)
        return
    try:
        city = message.text.strip()
        res = try_open_meteo(city)
        if res is None or res == "NOT_FOUND":
            res = try_wttr(city)
        if res is None:
            bot.reply_to(message, f"Не нашёл '{html.escape(city)}'.", reply_markup=back_to_menu_btn())
            return
        bot.reply_to(message, res, parse_mode="HTML", reply_markup=back_to_menu_btn())
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}", reply_markup=back_to_menu_btn())

# ==== Инициализация при импорте (не только в __main__) ====
try:
    init_db()
    print("[i] БД инициализирована")
except Exception as e:
    print(f"[!] Ошибка БД: {e}")

WEBHOOK_URL = f"https://weather-bot-0v1m.onrender.com/{BOT_TOKEN}"

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return 'Forbidden', 403

@app.route('/')
def index():
    return "Bot is running!"

@app.route('/health')
def health():
    return "OK"

if __name__ == "__main__":
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
    except Exception as e:
        print(f"[!] webhook error: {e}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
