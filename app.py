import os
import telebot
import requests
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
    "Light drizzle": "Слабая морось 🌦️",
    "Moderate rain": "Дождь 🌧️",
    "Heavy rain": "Сильный дождь 🌧️",
    "Patchy light drizzle": "Местами морось 🌦️",
    "Light snow": "Небольшой снег ❄️",
    "Moderate snow": "Снег ❄️", "Heavy snow": "Сильный снег ❄️",
    "Blizzard": "Метель ❄️", "Sleet": "Мокрый снег 🌨️",
    "Thundery outbreaks possible": "Возможны грозы ⛈️",
    "Patchy light rain with thunder": "Местами дождь с грозой ⛈️",
    "Moderate or heavy rain with thunder": "Сильный дождь с грозой ⛈️",
    "Light rain shower": "Небольшой ливень 🌦️",
    "Moderate or heavy rain shower": "Сильный ливень 🌧️",
    "Patchy light rain": "Местами небольшой дождь 🌦️",
    "Moderate or heavy rain shower": "Сильный ливень 🌧️",
    "Light freezing rain": "Слабый ледяной дождь 🌧️",
    "Moderate or heavy freezing rain": "Сильный ледяной дождь 🌧️",
}

COUNTRY_RU = {
    "Russia": "Россия", "Kazakhstan": "Казахстан", "Ukraine": "Украина",
    "Belarus": "Беларусь", "Norway": "Норвегия", "Sweden": "Швеция",
    "Finland": "Финляндия", "Denmark": "Дания", "Iceland": "Исландия",
    "Germany": "Германия", "France": "Франция", "Italy": "Италия",
    "Spain": "Испания", "Portugal": "Португалия", "Poland": "Польша",
    "Netherlands": "Нидерланды", "Belgium": "Бельгия",
    "Switzerland": "Швейцария", "Austria": "Австрия",
    "Czech Republic": "Чехия", "Czechia": "Чехия", "Slovakia": "Словакия",
    "Hungary": "Венгрия", "Romania": "Румыния", "Bulgaria": "Болгария",
    "Greece": "Греция", "Turkey": "Турция", "Serbia": "Сербия",
    "Croatia": "Хорватия", "Slovenia": "Словения",
    "United Kingdom": "Великобритания", "Ireland": "Ирландия",
    "USA": "США", "United States of America": "США",
    "Canada": "Канада", "Mexico": "Мексика", "Brazil": "Бразилия",
    "Argentina": "Аргентина", "Chile": "Чили", "Peru": "Перу",
    "China": "Китай", "Japan": "Япония", "South Korea": "Южная Корея",
    "Korea": "Корея", "India": "Индия", "Thailand": "Таиланд",
    "Vietnam": "Вьетнам", "Indonesia": "Индонезия",
    "Malaysia": "Малайзия", "Singapore": "Сингапур",
    "Philippines": "Филиппины", "Australia": "Австралия",
    "New Zealand": "Новая Зеландия", "Egypt": "Египет",
    "Morocco": "Марокко", "Tunisia": "Тунис", "Algeria": "Алжир",
    "South Africa": "ЮАР", "Kenya": "Кения", "Tanzania": "Танзания",
    "Ethiopia": "Эфиопия", "Nigeria": "Нигерия", "Ghana": "Гана",
    "Israel": "Израиль", "Saudi Arabia": "Саудовская Аравия",
    "United Arab Emirates": "ОАЭ", "UAE": "ОАЭ", "Qatar": "Катар",
    "Iran": "Иран", "Iraq": "Ирак", "Pakistan": "Пакистан",
    "Afghanistan": "Афганистан", "Georgia": "Грузия",
    "Armenia": "Армения", "Azerbaijan": "Азербайджан",
    "Uzbekistan": "Узбекистан", "Turkmenistan": "Туркменистан",
    "Kyrgyzstan": "Кыргызстан", "Tajikistan": "Таджикистан",
    "Mongolia": "Монголия", "Nepal": "Непал", "Cuba": "Куба",
    "Moldova": "Молдова", "Latvia": "Латвия", "Lithuania": "Литва",
    "Estonia": "Эстония", "Cyprus": "Кипр", "Malta": "Мальта",
    "Luxembourg": "Люксембург", "Monaco": "Монако",
}

def try_open_meteo(city):
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=ru"
        geo_resp = requests.get(geo_url, timeout=10).json()
        if "results" not in geo_resp or not geo_resp["results"]:
            return "NOT_FOUND"

        loc = geo_resp["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        country = loc.get("country", "")
        country = COUNTRY_RU.get(country, country)

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
        temp = cur["temperature_2m"]
        hum = cur["relative_humidity_2m"]
        wind = cur["wind_speed_10m"]
        code = cur["weather_code"]
        desc = WEATHER_DESC.get(code, "Неизвестно")

        return (
            f"📍 <b>{loc['name']}</b>, {country}\n"
            f"Погода: {desc}\n"
            f"🌡️ Температура: <b>{temp}°C</b>\n"
            f"💧 Влажность: {hum}%\n"
            f"💨 Ветер: {wind} км/ч"
        )
    except Exception as e:
        print(f"open-meteo error: {e}")
        return None

def try_wttr(city):
    try:
        url = f"https://wttr.in/{city}"
        params = {"format": "j1", "lang": "ru"}
        resp = requests.get(url, params=params, timeout=15).json()
        if "current_condition" not in resp:
            return None

        cur = resp["current_condition"][0]
        temp = cur.get("temp_C", "?")
        hum = cur.get("humidity", "?")
        wind = cur.get("windspeedKmph", "?")

        desc = "Неизвестно"
        if "lang_ru" in cur and cur["lang_ru"]:
            desc = cur["lang_ru"][0].get("value", desc)
        elif "weatherDesc" in cur and cur["weatherDesc"]:
            desc = cur["weatherDesc"][0].get("value", desc)

        desc = WTTR_TRANSLATIONS.get(desc, desc)

        city_name = city.capitalize()
        country = ""
        if "nearest_area" in resp and resp["nearest_area"]:
            area = resp["nearest_area"][0]
            if "country" in area and area["country"]:
                country = area["country"][0].get("value", "")
                country = COUNTRY_RU.get(country, country)

        return (
            f"📍 <b>{city_name}</b>, {country}\n"
            f"Погода: {desc}\n"
            f"🌡️ Температура: <b>{temp}°C</b>\n"
            f"💧 Влажность: {hum}%\n"
            f"💨 Ветер: {wind} км/ч"
        )
    except Exception as e:
        print(f"wttr.in error: {e}")
        return None

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, f"Привет, {message.from_user.first_name}! Напиши мне название города, и я скажу погоду. 🌤️")

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

WEBHOOK_URL = f"https://weather-bot-0v1m.onrender.com/{BOT_TOKEN}"

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
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
