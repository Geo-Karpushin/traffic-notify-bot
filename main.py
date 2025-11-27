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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

nest_asyncio.apply()
load_dotenv()

YANDEX_API_KEY = os.getenv("YANDEX_MAPS_API_KEY")
TG_TOKEN = os.getenv("TG_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

USERS_FILE = "users.json"
PENDING_FILE = "pending.json" 
KNOWN_FILE = "known_users.json"

def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

USERS = load_json(USERS_FILE, [])
PENDING = load_json(PENDING_FILE, {})
KNOWN_USERS = load_json(KNOWN_FILE, {})

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

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def save_all():
    save_json(USERS_FILE, USERS)
    save_json(PENDING_FILE, PENDING)
    save_json(KNOWN_FILE, KNOWN_USERS)

def update_env_admin(chat_id: int, username: str):
    """
    Перезаписывает .env, добавляя или обновляя ADMIN_CHAT_ID.
    Сохраняет другие переменные как есть.
    """
    env_path = ".env"
    env_data = {}

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    env_data[key] = value

    env_data["ADMIN_CHAT_ID"] = str(chat_id)

    with open(env_path, "w", encoding="utf-8") as f:
        for key, value in env_data.items():
            f.write(f"{key}={value}\n")

    print(f"[ENV] ADMIN_CHAT_ID={chat_id}")

def make_yandex_link(lat, lon):
    url = f"https://yandex.ru/maps/?ll={lon},{lat}&z=17"
    return f"[{lat}, {lon}]({url})"

def latlon_to_tile(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile

def get_yandex_layer_version(layer="trfe", lang="ru_RU"):
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
            if f["properties"]["eventType"] == 1:
                lat, lon = f["geometry"]["coordinates"]
                in_bounds = (lat_min <= lat <= lat_max) and (lon_min <= lon <= lon_max)
                print(f"   * ДТП: {lat:.6f}, {lon:.6f} {'В пределах' if in_bounds else 'Вне границ'} "
                      f"(границы: lat [{lat_min:.2f}-{lat_max:.2f}], lon [{lon_min:.2f}-{lon_max:.2f}])")
                if in_bounds:
                    accidents[(lat, lon)] = f["properties"]["description"]
    except Exception as e:
        print("❌ Ошибка при обработке данных:", e)
    return accidents

def get_admin_chat_id():
    global ADMIN_CHAT_ID
    if ADMIN_CHAT_ID:
        try:
            return int(ADMIN_CHAT_ID)
        except ValueError:
            return None
    return None

def normalize_username(u: str):
    if not u:
        return ""
    return u.lstrip("@").lower()

async def cmd_set_me_as_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_CHAT_ID

    user = update.effective_user
    chat_id = user.id
    username = user.username

    if ADMIN_CHAT_ID is not None:
        await update.message.reply_text("Администратор уже назначен.")
        return

    if username is None:
        await update.message.reply_text("Для назначения администратора нужен username в Telegram.")
        return

    update_env_admin(chat_id, username)

    ADMIN_CHAT_ID = chat_id

    await update.message.reply_text(f"Теперь вы администратор ({username}).")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    username = normalize_username(user.username) or f"user_{chat_id}"

    KNOWN_USERS[username] = chat_id
    save_json(KNOWN_FILE, KNOWN_USERS)

    if chat_id in USERS:
        await update.message.reply_text("Вы уже подписаны на уведомления.")
        return

    if username in PENDING:
        await update.message.reply_text("Ваша заявка уже в очереди — ожидайте одобрения администратора.")
        return

    PENDING[username] = chat_id
    save_json(PENDING_FILE, PENDING)

    admin_chat = get_admin_chat_id()
    if admin_chat:
        text = f"Пользователь @{username} запросил доступ к уведомлениям."
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{username}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"deny:{username}"),
            ]
        ])
        try:
            await context.bot.send_message(chat_id=admin_chat, text=text, reply_markup=keyboard)
        except Exception as e:
            print("Не удалось отправить уведомление админу:", e)
    else:
        await update.message.reply_text("Заявка принята, но администратор не указан — попробуйте позже.")
        return

    await update.message.reply_text("Заявка отправлена администратору. Ожидайте решения.")

async def cmd_actual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat.id in USERS:
        await update.message.reply_text("Запросите доступ у администратора.")
        return

    if not CURRENT_ACCIDENTS:
        message = "Сейчас в заданной области нет ни одного ДТП"
    else:
        message = "ТЕКУЩИЕ ДТП\n\n"
        message += "\n".join(
            f"⚠️ {make_yandex_link(lat, lon)} — {desc}"
            for (lat, lon), desc in CURRENT_ACCIDENTS.items()
        )
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def cmd_access_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Показывает всех подписанных пользователей (те, кто в USERS),
    с их username и chat_id.
    """

    if update.effective_chat.id != get_admin_chat_id():
        await update.message.reply_text("Только администратор может использовать эту команду.")
        return

    try:
        with open("known_users.json", "r") as f:
            known = json.load(f)
    except FileNotFoundError:
        known = {}

    if not USERS:
        await update.message.reply_text("Нет ни одного подписанного пользователя.")
        return

    text = """📋 Список подписанных пользователей:\n\n"""

    for uid in USERS:
        username = None
        for name, cid in known.items():
            if cid == uid:
                username = name
                break

        if username:
            text += f"""• @{username} — `{uid}`\n"""
        else:
            text += f"""• (username неизвестен) — `{uid}`\n"""

    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != get_admin_chat_id():
        await update.message.reply_text("Только администратор может просматривать этот список.")
        return
    if not PENDING:
        await update.message.reply_text("Список ожидания пуст.")
        return
    
    text = "Список ожидания (pending):"
    for uname, cid in PENDING.items():
        text += f"@{uname} (chat_id={cid})"

    await update.message.reply_text(text)

async def approve_user(username: str, context: ContextTypes.DEFAULT_TYPE, by_admin: int):
    username = normalize_username(username)
    if username not in PENDING:
        return False, "Пользователь не в списке ожидания."
    chat_id = PENDING.pop(username)
    if chat_id not in USERS:
        USERS.append(chat_id)
    save_all()
    try:
        await context.bot.send_message(chat_id=chat_id, text="Ваша заявка одобрена. Вы подписаны на уведомления.")
    except Exception as e:
        print("Не удалось уведомить пользователя об одобрении:", e)
    try:
        await context.bot.send_message(chat_id=by_admin, text=f"Пользователь @{username} одобрен.")
    except Exception:
        pass
    return True, "Одобрено"

async def deny_user(username: str, context: ContextTypes.DEFAULT_TYPE, by_admin: int):
    username = normalize_username(username)
    if username not in PENDING:
        return False, "Пользователь не в списке ожидания."
    chat_id = PENDING.pop(username)
    save_all()
    try:
        await context.bot.send_message(chat_id=chat_id, text="Ваша заявка отклонена администратором.")
    except Exception:
        pass
    try:
        await context.bot.send_message(chat_id=by_admin, text=f"Пользователь @{username} отклонён.")
    except Exception:
        pass
    return True, "Отклонено"

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != get_admin_chat_id():
        await update.message.reply_text("Только администратор может использовать эту команду.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("Использование: /approve username")
        return
    username = normalize_username(context.args[0])
    ok, msg = await approve_user(username, context, by_admin=update.effective_chat.id)
    await update.message.reply_text(msg)

async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != get_admin_chat_id():
        await update.message.reply_text("Только администратор может использовать эту команду.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("Использование: /deny username")
        return
    username = normalize_username(context.args[0])
    ok, msg = await deny_user(username, context, by_admin=update.effective_chat.id)
    await update.message.reply_text(msg)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    admin_id = update.effective_chat.id
    if ":" not in data:
        await query.edit_message_text("Непонятная команда.")
        return
    cmd, username = data.split(":", 1)
    username = normalize_username(username)

    if cmd == "approve":
        ok, msg = await approve_user(username, context, by_admin=admin_id)
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.edit_message_text(f"Пользователь @{username} одобрен.")
            except Exception:
                pass
    elif cmd == "deny":
        ok, msg = await deny_user(username, context, by_admin=admin_id)
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.edit_message_text(f"Пользователь @{username} отклонён.")
            except Exception:
                pass
    else:
        await query.edit_message_text("Неизвестное действие.")

async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != get_admin_chat_id():
        await update.message.reply_text("Только администратор может использовать эту команду.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("Использование: /revoke username")
        return
    username = normalize_username(context.args[0])
    chat_id = KNOWN_USERS.get(username)
    if not chat_id:
        await update.message.reply_text("Неизвестный пользователь.")
        return
    if chat_id in USERS:
        USERS.remove(chat_id)
        save_all()
        try:
            await context.bot.send_message(chat_id=chat_id, text="Вам закрыт доступ к уведомлениям администратора.")
        except Exception:
            pass
        await update.message.reply_text(f"Доступ у @{username} отозван.")
    else:
        await update.message.reply_text("Пользователь не был в списке подписчиков.")

async def send_notification(app, text: str):
    for user_id in USERS:
        try:
            await app.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
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
                lat, lon = acc
                appeared_accidents.append(f"🆕 Новое ДТП: {make_yandex_link(lat, lon)}")

        resolved_accidents = []
        for acc in CURRENT_ACCIDENTS:
            if acc not in new_accidents:
                lat, lon = acc
                resolved_accidents.append(f"✅ ДТП разрешено: {make_yandex_link(lat, lon)}")

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

        print(f"Ожидание {args.interval} секунд до следующего обновления...")
        await asyncio.sleep(args.interval)

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

    # user commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("set_me_as_admin", cmd_set_me_as_admin))
    app.add_handler(CommandHandler("actual", cmd_actual))

    # admin management
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("access_list", cmd_access_list))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("revoke", cmd_revoke))

    # callback handler for inline buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("Телеграм бот запущен...")

    async def start_fetch_loop():
        await fetch_and_notify(app, args)

    asyncio.create_task(start_fetch_loop())

    await app.run_polling()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
