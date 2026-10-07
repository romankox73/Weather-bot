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

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, f"Привет, {message.from_user.first_name}! Напиши мне название города, и я скажу погоду. 🌤️")

@bot.message_handler(func=lambda message: True)
def get_weather(message):
    city = message.text.strip()
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=ru"
    try:
        geo_resp = requests.get(geo_url, timeout=10).json()
    except Exception:
        bot.reply_to(message, "Не могу подключиться к сервису погоды.")
        return

    if "results" not in geo_resp or not geo_resp["results"]:
        bot.reply_to(message, f"Город '{city}' не найден.")
        return

    loc = geo_resp["results"][0]
    lat, lon = loc["latitude"], loc["longitude"]
    country = loc.get("country", "")

    weather_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
        f"&timezone=auto"
    )
    try:
        w_resp = requests.get(weather_url, timeout=10).json()
    except Exception:
        bot.reply_to(message, "Не могу получить данные о погоде.")
        return

    cur = w_resp["current"]
    temp = cur["temperature_2m"]
    hum = cur["relative_humidity_2m"]
    wind = cur["wind_speed_10m"]
    code = cur["weather_code"]

    weather_desc = {
        0: "Ясно ☀️", 1: "В основном ясно 🌤️", 2: "Переменная облачность ⛅",
        3: "Пасмурно ☁️", 45: "Туман 🌫️", 48: "Иней 🌫️",
        51: "Морось 🌦️", 53: "Морось 🌦️", 55: "Морось 🌦️",
        61: "Дождь 🌧️", 63: "Дождь 🌧️", 65: "Ливень 🌧️",
        71: "Снег ❄️", 73: "Снег ❄️", 75: "Сильный снег ❄️",
        80: "Ливень 🌦️", 81: "Ливень 🌦️", 82: "Сильный ливень 🌦️",
        95: "Гроза ⛈️", 96: "Гроза с градом ⛈️", 99: "Гроза с градом ⛈️",
    }
    desc = weather_desc.get(code, "Неизвестно")

    text = (
        f"📍 <b>{loc['name']}</b>, {country}\n"
        f"Погода: {desc}\n"
        f"🌡️ Температура: <b>{temp}°C</b>\n"
        f"💧 Влажность: {hum}%\n"
        f"💨 Ветер: {wind} км/ч"
    )
    bot.reply_to(message, text, parse_mode="HTML")

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
