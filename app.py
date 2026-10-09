import os
import csv
import telebot
import requests
from telebot import types
from flask import Flask, request

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not BOT_TOKEN:
    try:
        from config import BOT_TOKEN as CFG_TOKEN
        BOT_TOKEN = CFG_TOKEN
    except ImportError:
        raise ValueError("Нет ни TELEGRAM_TOKEN, ни config.py")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ==================== БУРОВЫЕ ====================
RELEVANCE_LABEL = {
    "A": "⭐",
    "B": "○",
    "C": "·",
}
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
    # Сортируем: A → B → C
    for city in data:
        data[city].sort(key=lambda x: RELEVANCE_ORDER.get(x["relevance"], 3))
    return data

BUR_DATA = load_bur_data()
CITIES = sorted(BUR_DATA.keys())
PER_PAGE = 5
print(f"[i] Загружено городов: {len(CITIES)}")

def count_relevance(items):
    a = sum(1 for x in items if x["relevance"] == "A")
    b = sum(1 for x in items if x["relevance"] == "B")
    c = sum(1 for x in items if x["relevance"] == "C")
    return a, b, c

# ==================== КЛАВИАТУРЫ ====================
def main_menu():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("🌤 Погода", callback_data="menu:weather"),
        types.InlineKeyboardButton("🏢 Буровые", callback_data="menu:bur"),
    )
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
    bot.edit_message_text("🌤 Напиши название города — пришлю погоду.", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "menu:bur")
def cb_menu_bur(call):
    bot.answer_callback_query(call.id)
    if not CITIES:
        bot.edit_message_text("Данные буровых ещё не загружены.", call.message.chat.id, call.message.message_id)
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
            bot.reply_to(message, f"Не могу получить погоду для '{city}'. Попробуй позже.")
            return
        bot.reply_to(message, result, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

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
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
