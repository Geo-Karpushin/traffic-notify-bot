#!/usr/bin/env python3
import math
import os
import time
import json
import argparse
import requests
import asyncio
import threading
import nest_asyncio
from dotenv import load_dotenv
import json
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

nest_asyncio.apply()

load_dotenv()

YANDEX_API_KEY = os.getenv("YANDEX_MAPS_API_KEY")
TG_TOKEN = os.getenv("TG_API_KEY")
USERS_FILE = "users.json"
USERS = []

try:
    with open(USERS_FILE, "r") as f:
        USERS = json.load(f)
except FileNotFoundError:
    USERS = []

if not YANDEX_API_KEY:
    raise RuntimeError("Ошибка: переменная окружения YANDEX_MAPS_API_KEY не найдена в .env!")

JSON_STORAGE = "accidents.json"
if os.path.exists(JSON_STORAGE):
    with open(JSON_STORAGE, "r") as f:
        CURRENT_ACCIDENTS = {tuple(map(float, k.split(","))): v for k, v in json.load(f).items()}
    print(f"Загружено старых ДТП: {len(CURRENT_ACCIDENTS)}")
else:
    CURRENT_ACCIDENTS = {}

DEFAULT_ZOOM = 11
DEFAULT_INTERVAL = 15

def latlon_to_tile(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile

def get_yandex_layer_version(layer="trfe", lang="ru_RU"):
    """
    Получает актуальную версию слоя тайлов из официального API Яндекс.Карт.
    По умолчанию — слой ДТП (trfe).
    
    Возвращает: строку версии (например "2025.11.25.22.46.00")
    или None при ошибке.
    """
    url = (
        "https://api-maps.yandex.ru/services/coverage/v2/layers_stamps"
        f"?lang={lang}&l={layer}"
    )

    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            print(f"⚠ Ошибка API версии слоёв: HTTP {resp.status_code}")
            return None

        data = resp.json()

        if layer not in data or "version" not in data[layer]:
            print(f"⚠ В ответе нет версии слоя {layer}: {data}")
            return None

        return data[layer]["version"]

    except Exception as e:
        print(f"❌ Ошибка при получении версии слоя {layer}: {e}")
        return None

def fetch_tile_json(x, y, z, version):
    url = f"https://core-road-events-renderer.maps.yandex.net/1.1/tiles?l=trje&lang=ru_RU&x={x}&y={y}&z={z}&scale=1&v={version}&apikey={YANDEX_API_KEY}&callback=x_{x}_y_{y}_z_{z}_l_trje__t"
    try:
        print(f"→ Скачиваем тайл: x={x}, y={y}, z={z}")
        resp = requests.get(url)
        text = resp.text
        start = text.find("(")
        end = text.rfind(");")
        json_text = text[start+1:end]
        data = json.loads(json_text)
        return data
    except Exception as e:
        print(f"❌ Ошибка при загрузке тайла {x},{y},{z}: {e}")
        return None

def extract_accidents(data, lat_min, lon_min, lat_max, lon_max):
    accidents = {}
    try:
        features = data.get("data", {}).get("features", [])
        print(f"   Найдено событий в тайле: {len(features)}")
        for f in features:
            if f["properties"]["eventType"] == 1:  # ДТП
                lat, lon = f["geometry"]["coordinates"]  # lon, lat!
                # ИСПРАВЛЕНИЕ: Правильный порядок сравнения
                in_bounds = (lat_min <= lat <= lat_max) and (lon_min <= lon <= lon_max)
                print(f"   * ДТП: {lat:.6f}, {lon:.6f} {'В пределах' if in_bounds else 'Вне границ'} "
                      f"(границы: lat [{lat_min:.2f}-{lat_max:.2f}], lon [{lon_min:.2f}-{lon_max:.2f}])")
                if in_bounds:
                    accidents[(lat, lon)] = f["properties"]["description"]
    except Exception as e:
        print("❌ Ошибка при обработке данных:", e)
    return accidents

def save_users():
    with open(USERS_FILE, "w") as f:
        json.dump(USERS, f)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    if user_id not in USERS:
        USERS.append(user_id)
        save_users()
    await update.message.reply_text("Привет! Ты подписан на уведомления.")

async def actual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CURRENT_ACCIDENTS:  # More Pythonic way to check empty list
        message = "Сейчас в заданной области нет ни одного ДТП"
    else:
        message = "ТЕКУЩИЕ ДТП\n\n"
        message += "\n".join(f"⚠️ {acc}" for acc in CURRENT_ACCIDENTS)
    
    await update.message.reply_text(message)

async def send_notification(app, text: str):
    for user_id in USERS:
        try:
            await app.bot.send_message(chat_id=user_id, text=text)
        except Exception as e:
            print(f"Не удалось отправить сообщение {user_id}: {e}")

async def fetch_and_notify(app, args):
    global CURRENT_ACCIDENTS
    while True:
        x1, y1 = latlon_to_tile(args.lat_min, args.lon_min, args.zoom)
        x2, y2 = latlon_to_tile(args.lat_max, args.lon_max, args.zoom)

        x_min, x_max = sorted((x1, x2))
        y_min, y_max = sorted((y1, y2))

        y_min += 2
        y_max += 2

        print(f"Вычислены тайлы: x [{x_min}, {x_max}], y [{y_min}, {y_max}]")
        print(f"Границы области: lat [{args.lat_min:.2f}-{args.lat_max:.2f}], lon [{args.lon_min:.2f}-{args.lon_max:.2f}]")

        if y_min > y_max:
            y_min, y_max = y_max, y_min
        if x_min > x_max:
            x_min, x_max = x_max, x_min

        new_accidents = {}

        version = get_yandex_layer_version()

        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                data = fetch_tile_json(x, y, args.zoom, version)
                if not data:
                    continue
                accidents = extract_accidents(data, args.lat_min, args.lon_min, args.lat_max, args.lon_max)
                new_accidents.update(accidents)

        print(f"Общее количество ДТП в текущем цикле: {len(new_accidents)}")

        appeared_accidents = []
        for acc in new_accidents:
            if acc not in CURRENT_ACCIDENTS:
                appeared_accidents.append(f"🆕 Новое ДТП: {acc}")

        resolved_accidents = []
        for acc in CURRENT_ACCIDENTS:
            if acc not in new_accidents:
                resolved_accidents.append(f"✅ ДТП разрешено: {acc}")

        if len(appeared_accidents) > 0 or len(resolved_accidents) > 0:
            print(f"Зафиксировано {len(appeared_accidents)} новых и {len(resolved_accidents)} разрешённых ДТП")
            message = "НОВЫЕ СОБЫТИЯ\n\n"
            message += "\n".join(appeared_accidents)
            if len(appeared_accidents) > 0:
                message += "\n\n"
            message += "\n".join(resolved_accidents)
            asyncio.create_task(send_notification(app, message))

        with open(JSON_STORAGE, "w") as f:
            json.dump({f"{k[0]},{k[1]}": v for k, v in new_accidents.items()}, f, indent=2)
        print(f"Актуальный словарь сохранён: {JSON_STORAGE}")

        CURRENT_ACCIDENTS = new_accidents

        print(f"Ожидание {args.interval} секунд до следующего обновления...\n")
        await asyncio.sleep(args.interval)

# Главная функция
async def main():
    parser = argparse.ArgumentParser(description="Слежение за ДТП Яндекс.Карт")
    parser.add_argument("--lat_min", type=float, default=55.55)
    parser.add_argument("--lon_min", type=float, default=37.35)
    parser.add_argument("--lat_max", type=float, default=55.91)
    parser.add_argument("--lon_max", type=float, default=37.85)
    parser.add_argument("--zoom", type=int, default=DEFAULT_ZOOM)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = parser.parse_args()

    args.lat_min, args.lat_max = sorted((args.lat_min, args.lat_max))
    args.lon_min, args.lon_max = sorted((args.lon_min, args.lon_max))

    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("actual", actual))

    print("Телеграм бот запущен...")

    # Запускаем оба таска в одном EventLoop
    async def start_fetch_loop():
        await fetch_and_notify(app, args)

    asyncio.create_task(start_fetch_loop())

    await app.run_polling()  # Должно выполняться в главном потоке

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())