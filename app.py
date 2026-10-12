import os
import csv
import telebot
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from telebot import types
from flask import Flask, request
from datetime import datetime

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not BOT_TOKEN:
    try:
        from config import BOT_TOKEN as CFG_TOKEN
        BOT_TOKEN = CFG_TOKEN
    except ImportError:
        raise ValueError("Нет ни TELEGRAM_TOKEN, ни config.py")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ==================== БАЗА ДАННЫХ ====================
STATUS_LABELS = {
    "not_called": "⚪ Не звонили",
    "called": "📞 Дозвонились",
    "kp_sent": "📨 КП отправлено",
    "thinking": "🤔 Думает",
    "client": "✅ Клиент",
    "refused": "❌ Отказ",
}

def db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with db() as conn:
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
            count = cur.fetchone()[0]
            if count == 0:
                print("[i] Заливаю компании из CSV в базу...")
                load_csv_to_db(cur)
                print(f"[i] Загружено компаний: {cur.rowcount}")
        conn.commit()

def load_csv_to_db(cur):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_cities.csv")
    if not os.path.exists(path):
        print(f"[!] all_cities.csv не найден: {path}")
        return
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
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
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM clients GROUP BY status;")
            rows = cur.fetchall()
            stats = {r[0]: r[1] for r in rows}
            cur.execute("SELECT COUNT(*) FROM clients;")
            total = cur.fetchone()[0]
    return total, stats

def get_next_client():
    """Следующий клиент для обзвона: сначала A, потом B, потом C, только 'not_called'"""
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM clients
                WHERE status = 'not_called'
                ORDER BY
                    CASE relevance WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 ELSE 4 END,
                    id
                LIMIT 1;
            """)
            return cur.fetchone()

def get_client(client_id):
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM clients WHERE id = %s;", (client_id,))
            return cur.fetchone()

def set_status(client_id, status):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clients SET status = %s, updated_at = NOW() WHERE id = %s;",
                (status, client_id)
            )
        conn.commit()

# ==================== БУРОВЫЕ (справочник) ====================
RELEVANCE_LABEL = {"A": "⭐", "B": "○", "C": "·"}
RELEVANCE_ORDER = {"A": 0, "B": 1, "C": 2, "": 3}

def load_bur_data():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_cities.csv")
    data = {}
    if not os.path.exists(path):
        print(f"[!] all_cities.csv не найден: {path}")
        return data
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            city = (row.get("searchCity") or row.get("city") or "").strip()
            if not city:
                continue
            relevance = (row.get("relevance") or "").strip().upper()
            data.setdefault(city, []).append({
                "name": (row.get("name") or "Без названия").strip(),
                "phone": (row.get("allPhones") or row.get("phone") or "—").strip(),
                "email": (row.get("email") or "—").strip(),
                "relevance": relevance,
            })
    for city in data:
        data[city].sort(key=lambda x: RELEVANCE_ORDER.get(x["relevance"], 3))
    return data

BUR_DATA = load_bur_data()
CITIES = sorted(BUR_DATA.keys())
PER_PAGE = 5

def count_relevance(items):
    a = sum(1 for x in items if x["relevance"] == "A")
    b = sum(1 for x in items if x["relevance"] == "B")
    c = sum(1 for x in items if x["relevance"] == "C")
    return a, b, c

# ==================== APIFY BALANCE ====================
def get_apify_balance():
    if not APIFY_TOKEN:
        return "⚠️ Токен Apify не настроен."
    try:
        url = f"https://api.apify.com/v2/users/me/limits?token={APIFY_TOKEN}"
        r = requests.get(url, timeout=15).json()
        d = r.get("data", {})
        current = d.get("current", {})
        used = current.get("monthlyUsageUsd", 0)
        limit = d.get("maxMonthlyUsageUsd", 5)
        remaining = limit - used
        cities_left = int(remaining / 0.15) if remaining > 0 else 0
        percent = int((used / limit) * 100) if limit > 0 else 0
        return (
            f"💰 <b>Баланс Apify</b>\n\n"
            f"Потрачено: <b>${used:.2f}</b> из ${limit:.2f}\n"
            f"Остаток:   <b>${remaining:.2f}</b>  ({percent}% израсходовано)\n\n"
            f"💡 Хватит ещё на <b>~{cities_left}</b> городов\n"
            f"📅 Следующие $5 придут ~7-го числа"
        )
    except Exception as e:
        print(f"apify balance error: {e}")
        return f"⚠️ Не удалось получить баланс: {e}"

# ==================== КЛАВИАТУРЫ ====================
def main_menu():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("🌤 Погода", callback_data="menu:weather"),
        types.InlineKeyboardButton("🏢 Буровые", callback_data="menu:bur"),
    )
    m.add(
        types.InlineKeyboardButton("📋 CRM", callback_data="crm:menu"),
        types.InlineKeyboardButton("💰 Баланс Apify", callback_data="menu:balance"),
    )
    return m

def back_to_menu_btn():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return m

def crm_menu():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(types.InlineKeyboardButton("📞 Начать обзвон", callback_data="crm:next"))
    m.add(types.InlineKeyboardButton("📊 Статистика", callback_data="crm:stats"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return m

def client_card_markup(client_id):
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("📞 Дозвонились", callback_data=f"crm:set:{client_id}:called"),
        types.InlineKeyboardButton("📨 КП", callback_data=f"crm:set:{client_id}:kp_sent"),
    )
    m.add(
        types.InlineKeyboardButton("🤔 Думает", callback_data=f"crm:set:{client_id}:thinking"),
        types.InlineKeyboardButton("✅ Клиент", callback_data=f"crm:set:{client_id}:client"),
    )
    m.add(
        types.InlineKeyboardButton("❌ Отказ", callback_data=f"crm:set:{client_id}:refused"),
        types.InlineKeyboardButton("⏭ Пропустить", callback_data="crm:next"),
    )
    m.add(types.InlineKeyboardButton("◀️ В CRM", callback_data="crm:menu"))
    return m

def cities_menu():
    m = types.InlineKeyboardMarkup(row_width=3)
    buttons = [types.InlineKeyboardButton(c, callback_data=f"city:{i}") for i, c in enumerate(CITIES)]
    if buttons:
        m.add(*buttons)
    m.add(types.InlineKeyboardButton("◀️ В меню", callback_data="menu:start"))
    return m

def render_companies(city_idx, page):
    city = CITIES[city_idx]
    items = BUR_DATA[city]
    total = len(items)
    a, b, c = count_relevance(items)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    start = page * PER_PAGE
    chunk = items[start:start + PER_PAGE]

    text = f"🏢 <b>{city}</b> — всего {total}\n"
    text += f"⭐ {a}  ○ {b}  · {c}\n\n"
    for i, item in enumerate(chunk, start=start + 1):
        mark = RELEVANCE_LABEL.get(item["relevance"], "")
        text += f"{mark} <b>{i}. {item['name']}</b>\n📞 {item['phone']}\n✉️ {item['email']}\n\n"
    text += f"Страница {page + 1} из {total_pages}"

    m = types.InlineKeyboardMarkup(row_width=3)
    prev_btn = types.InlineKeyboardButton("◀️", callback_data=f"page:{city_idx}:{page-1}") if page > 0 else types.InlineKeyboardButton(" ", callback_data="noop")
    info_btn = types.InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop")
    next_btn = types.InlineKeyboardButton("▶️", callback_data=f"page:{city_idx}:{page+1}") if page < total_pages - 1 else types.InlineKeyboardButton(" ", callback_data="noop")
    m.add(prev_btn, info_btn, next_btn)
    m.add(types.InlineKeyboardButton("◀️ К городам", callback_data="menu:bur"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    return text, m

def render_client_card(client):
    if not client:
        return (
            "✅ <b>Все клиенты обработаны!</b>\n\n"
            "В базе не осталось компаний со статусом «Не звонили».\n"
            "Можешь посмотреть статистику или добавить новых через Apify.",
            crm_menu()
        )
    mark = RELEVANCE_LABEL.get(client["relevance"], "")
    status = STATUS_LABELS.get(client["status"], client["status"])
    text = (
        f"{mark} <b>{client['name']}</b>\n\n"
        f"🏙 {client['city'] or '—'}\n"
        f"📞 {client['phone'] or '—'}\n"
        f"✉️ {client['email'] or '—'}\n"
        f"📊 Статус: {status}\n\n"
        f"Выбери действие:"
    )
    return text, client_card_markup(client["id"])

# ==================== ОБРАБОТЧИКИ ====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(message.chat.id, f"Привет, {message.from_user.first_name}! Выбери раздел:", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: call.data == "menu:start")
def cb_menu_start(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text("Выбери раздел:", call.message.chat.id, call.message.message_id, reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: call.data == "menu:weather")
def cb_menu_weather(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        "🌤 Напиши название города — пришлю погоду:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=back_to_menu_btn()
    )

@bot.callback_query_handler(func=lambda call: call.data == "menu:balance")
def cb_menu_balance(call):
    bot.answer_callback_query(call.id, "Считаю баланс...")
    text = get_apify_balance()
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="menu:balance"))
    m.add(types.InlineKeyboardButton("🏠 В меню", callback_data="menu:start"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "menu:bur")
def cb_menu_bur(call):
    bot.answer_callback_query(call.id)
    if not CITIES:
        bot.edit_message_text("Данные буровых ещё не загружены.", call.message.chat.id, call.message.message_id, reply_markup=back_to_menu_btn())
        return
    bot.edit_message_text(f"🏢 Выбери город (всего {len(CITIES)}):", call.message.chat.id, call.message.message_id, reply_markup=cities_menu())

@bot.callback_query_handler(func=lambda call: call.data.startswith("city:"))
def cb_city(call):
    bot.answer_callback_query(call.id)
    idx = int(call.data.split(":")[1])
    text, markup = render_companies(idx, 0)
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("page:"))
def cb_page(call):
    bot.answer_callback_query(call.id)
    _, city_idx, page = call.data.split(":")
    text, markup = render_companies(int(city_idx), int(page))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "noop")
def cb_noop(call):
    bot.answer_callback_query(call.id)

# ==================== CRM ====================
@bot.callback_query_handler(func=lambda call: call.data == "crm:menu")
def cb_crm_menu(call):
    bot.answer_callback_query(call.id)
    total, stats = get_stats()
    text = "📋 <b>CRM — учёт клиентов</b>\n\n"
    text += f"Всего в базе: <b>{total}</b>\n\n"
    for key, label in STATUS_LABELS.items():
        text += f"{label}: <b>{stats.get(key, 0)}</b>\n"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=crm_menu(), parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "crm:stats")
def cb_crm_stats(call):
    bot.answer_callback_query(call.id)
    cb_crm_menu(call)

@bot.callback_query_handler(func=lambda call: call.data == "crm:next")
def cb_crm_next(call):
    bot.answer_callback_query(call.id)
    client = get_next_client()
    text, markup = render_client_card(client)
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("crm:set:"))
def cb_crm_set(call):
    _, _, client_id, status = call.data.split(":")
    set_status(int(client_id), status)
    bot.answer_callback_query(call.id, f"Статус: {STATUS_LABELS.get(status, status)}")
    client = get_next_client()
    text, markup = render_client_card(client)
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")

# ==================== ПОГОДА ====================
WEATHER_DESC = {
    0: "Ясно ☀️", 1: "В основном ясно 🌤️", 2: "Переменная облачность ⛅",
    3: "Пасмурно ☁️", 45: "Туман 🌫️", 48: "Иней 🌫️",
    51: "Морось 🌦️", 53: "Морось 🌦️", 55: "Морось 🌦️",
    61: "Дождь 🌧️", 63: "Дождь 🌧️", 65: "Ливень 🌧️",
    71: "Снег ❄️", 73: "Снег ❄️", 75: "Сильный снег ❄️",
    80: "Ливень 🌦️", 81: "Ливень 🌦️", 82: "Сильный ливень 🌦️",
    95: "Гроза ⛈️", 96: "Гроза с градом ⛈️", 99: "Гроза с градом ⛈️",
}

WTTR_TRANSLATIONS = {
    "Sunny": "Ясно ☀️", "Clear": "Ясно ☀️",
    "Partly cloudy": "Переменная облачность ⛅",
    "Cloudy": "Облачно ☁️", "Overcast": "Пасмурно ☁️",
    "Mist": "Дымка 🌫️", "Fog": "Туман 🌫️",
    "Patchy rain nearby": "Местами дождь 🌦️",
    "Patchy rain possible": "Возможен дождь 🌦️",
    "Light rain": "Небольшой дождь 🌧️",
    "Moderate rain": "Дождь 🌧️",
    "Heavy rain": "Сильный дождь 🌧️",
    "Light drizzle": "Слабая морось 🌦️",
    "Light snow": "Небольшой снег ❄️",
    "Moderate snow": "Снег ❄️", "Heavy snow": "Сильный снег ❄️",
    "Blizzard": "Метель ❄️", "Sleet": "Мокрый снег 🌨️",
    "Thundery outbreaks possible": "Возможны грозы ⛈️",
    "Light rain shower": "Небольшой ливень 🌦️",
    "Moderate or heavy rain shower": "Сильный ливень 🌧️",
}

COUNTRY_RU = {
    "Russia": "Россия", "Kazakhstan": "Казахстан", "Ukraine": "Украина",
    "Belarus": "Беларусь", "Norway": "Норвегия", "Sweden": "Швеция",
    "Finland": "Финляндия", "Denmark": "Дания", "Iceland": "Исландия",
    "Germany": "Германия", "France": "Франция", "Italy": "Италия",
    "Spain": "Испания", "Poland": "Польша", "Netherlands": "Нидерланды",
    "Turkey": "Турция", "China": "Китай", "Japan": "Япония",
    "USA": "США", "United States of America": "США",
    "United Kingdom": "Великобритания", "Canada": "Канада",
    "Georgia": "Грузия", "Armenia": "Армения", "Azerbaijan": "Азербайджан",
    "Uzbekistan": "Узбекистан", "Kyrgyzstan": "Кыргызстан",
    "Tajikistan": "Таджикистан", "Mongolia": "Монголия", "Egypt": "Египет",
}

def try_open_meteo(city):
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=ru"
        geo_resp = requests.get(geo_url, timeout=10).json()
        if "results" not in geo_resp or not geo_resp["results"]:
            return "NOT_FOUND"
        loc = geo_resp["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        country = COUNTRY_RU.get(loc.get("country", ""), loc.get("country", ""))
        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
            f"&timezone=auto"
        )
        w_resp = requests.get(weather_url, timeout=10).json()
        if "current" not in w_resp:
            return None
        cur = w_resp["current"]
        desc = WEATHER_DESC.get(cur["weather_code"], "Неизвестно")
        return (
            f"📍 <b>{loc['name']}</b>, {country}\n"
            f"Погода: {desc}\n"
            f"🌡️ Температура: <b>{cur['temperature_2m']}°C</b>\n"
            f"💧 Влажность: {cur['relative_humidity_2m']}%\n"
            f"💨 Ветер: {cur['wind_speed_10m']} км/ч"
        )
    except Exception as e:
        print(f"open-meteo error: {e}")
        return None

def try_wttr(city):
    try:
        url = f"https://wttr.in/{city}"
        resp = requests.get(url, params={"format": "j1", "lang": "ru"}, timeout=15).json()
        if "current_condition" not in resp:
            return None
        cur = resp["current_condition"][0]
        desc = "Неизвестно"
        if cur.get("lang_ru"):
            desc = cur["lang_ru"][0].get("value", desc)
        elif cur.get("weatherDesc"):
            desc = cur["weatherDesc"][0].get("value", desc)
        desc = WTTR_TRANSLATIONS.get(desc, desc)
        country = ""
        if resp.get("nearest_area"):
            area = resp["nearest_area"][0]
            if area.get("country"):
                raw = area["country"][0].get("value", "")
                country = COUNTRY_RU.get(raw, raw)
        return (
            f"📍 <b>{city.capitalize()}</b>, {country}\n"
            f"Погода: {desc}\n"
            f"🌡️ Температура: <b>{cur.get('temp_C', '?')}°C</b>\n"
            f"💧 Влажность: {cur.get('humidity', '?')}%\n"
            f"💨 Ветер: {cur.get('windspeedKmph', '?')} км/ч"
        )
    except Exception as e:
        print(f"wttr.in error: {e}")
        return None

@bot.message_handler(func=lambda message: True)
def get_weather(message):
    try:
        city = message.text.strip()
        result = try_open_meteo(city)
        if result is None or result == "NOT_FOUND":
            result = try_wttr(city)
        if result is None:
            bot.reply_to(message, f"Не могу получить погоду для '{city}'. Попробуй позже.", reply_markup=back_to_menu_btn())
            return
        bot.reply_to(message, result, parse_mode="HTML", reply_markup=back_to_menu_btn())
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}", reply_markup=back_to_menu_btn())

# ==================== WEBHOOK ====================
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
        init_db()
        print("[i] База данных инициализирована")
    except Exception as e:
        print(f"[!] Ошибка инициализации БД: {e}")
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
