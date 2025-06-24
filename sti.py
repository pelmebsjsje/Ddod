import json
import asyncio
import re
import time
import logging
import hashlib
from typing import Dict, List
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from telegram.error import TelegramError

# Настройка логирования
logging.basicConfig(
    filename='sticker_log.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Константы
TELEGRAM_BOT_TOKEN = "7566077832:AAF7oS5iOWfSGA14NM5AjrO2u8kNUM-djws"
STOCK_CHANNEL_ID = "@sbtdrasik"
STOCK_URL = "https://www.vulcanvalues.com/grow-a-garden/stock"
STOCK_CHECK_INTERVAL = 30  # КД 30 секунд
MSG_ID_FILE = "last_messages.json"
PREV_STOCK_FILE = "prev_stock.json"
STICKER_IDS_FILE = "sticker_ids.json"
LAST_SENT_PERIODS_FILE = "last_sent_periods.json"
ADMIN_ID = "5194736461"
BOT_USERNAME = "@growagardenstock_abot"

SECTION_LIST = ["GEAR STOCK", "EGG STOCK", "SEEDS STOCK", "COSMETICS STOCK"]
SELECTABLE_SECTIONS = ["GEAR STOCK", "EGG STOCK", "SEEDS STOCK"]
SECTION_EMOJI = {"GEAR STOCK": "⚙️", "EGG STOCK": "🥚", "SEEDS STOCK": "🌱", "COSMETICS STOCK": "🧴"}
SECTION_TRANSLATE = {"GEAR STOCK": "Предметы", "EGG STOCK": "Яйца", "SEEDS STOCK": "Семена", "COSMETICS STOCK": "Косметика"}
SECTION_PERIOD = {"GEAR STOCK": 5, "SEEDS STOCK": 5, "EGG STOCK": 30, "COSMETICS STOCK": 240}

ITEM_TRANSLATE = {
    "Sugar Apple": "Сахарное яблоко",
    "Cacao": "Какао",
    "Pepper": "Перец",
    "Ember Lily": "Эмбер лилия",
    "Beanstalk": "Бобовый стебель",
    "Lightning Rod": "Громовод",
    "Mythical Egg": "Мифическое яйцо",
    "Master Sprinkler": "Мастер разбрызгиватель",
    "Mushroom": "Гриб",
    "Bug Egg": "Баг яйцо",
    "Mango": "Манго",
    "Dragon Fruit": "Питайя",
    "Friendship Pot": "Горшок дружбы",
    "Kiwi": "Киви",
    "Kiwifruit": "Киви",
    "Pineapple Fruit": "Ананас",
    "Prickly Pear": "Кактусовый инжир",
    "Loquat": "Мушмула",
    "Feijoa": "Фейхоа",
    "Tanning Mirror": "Зеркало для загара",
    "Paradise Egg": "Райское яйцо"
}

ITEM_EMOJI = {
    "Сахарное яблоко": "🍎",
    "Какао": "🍫",
    "Перец": "🌶️",
    "Эмбер лилия": "🌸",
    "Бобовый стебель": "🌱",
    "Громовод": "⚡️",
    "Мифическое яйцо": "🔴",
    "Мастер разбрызгиватель": "🏆",
    "Гриб": "🍄",
    "Баг яйцо": "🐞",
    "Манго": "🥭",
    "Питайя": "🐉",
    "Горшок дружбы": "🌼",
    "Киви": "🥝",
    "Ананас": "🍍",
    "Кактусовый инжир": "🌵",
    "Мушмула": "🍑",
    "Фейхоа": "🥭",
    "Зеркало для загара": "🪞",
    "Райское яйцо": "🟡"
}

egg_colors = {
    "Мифическое яйцо": "🟥",
    "Баг яйцо": "🟢",
    "Райское яйцо": "🟡"
}

ALLOWED_ITEMS = list(ITEM_TRANSLATE.values())
waiting_sticker = {}
last_update_time = 0
update_lock = asyncio.Lock()

def normalize_item_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r'\s*x\d+\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+', ' ', name.strip())
    name_lower = name.lower()
    for eng_name in ITEM_TRANSLATE:
        if eng_name.lower() == name_lower:
            return eng_name
    return name.title()

def normalize_stock(stocks: Dict) -> Dict:
    normalized = {}
    for section, items in stocks.items():
        normalized[section] = sorted(
            [
                {
                    "name": item["name"].strip().lower(),
                    "emoji": ''.join(sorted(item["emoji"].strip())),
                    "qty": item["qty"].strip().lower()
                }
                for item in items
            ],
            key=lambda x: x["name"]
        )
    return normalized

def get_stock_hash(stocks: Dict) -> str:
    norm_stocks = normalize_stock(stocks)
    stock_str = json.dumps(norm_stocks, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(stock_str.encode('utf-8')).hexdigest()

def get_period_block(section: str) -> str:
    now = datetime.now()
    period_min = SECTION_PERIOD.get(section, 30)
    block_start = now.replace(second=0, microsecond=0)
    if period_min >= 30:
        minute = (block_start.minute // period_min) * period_min
        block_start = block_start.replace(minute=minute)
    elif period_min == 5:
        minute = (block_start.minute // 5) * 5
        block_start = block_start.replace(minute=minute)
    block_end = block_start + timedelta(minutes=period_min)
    period_str = f"{block_start.strftime('%H:%M')}-{block_end.strftime('%H:%M')}"
    logger.debug(f"Сгенерирован период для {section}: {period_str}")
    return period_str

def get_stock() -> Dict:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            scraper = cloudscraper.create_scraper()
            response = scraper.get(STOCK_URL, timeout=15, headers={'Cache-Control': 'no-cache'})
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            logger.debug(f"Сырой HTML: {response.text[:500]}...")
            stocks = {section: [] for section in SECTION_LIST}
            for h2 in soup.find_all("h2"):
                section_name = h2.get_text(strip=True).upper()
                if section_name not in SECTION_LIST:
                    logger.debug(f"Пропущена секция: {section_name}")
                    continue
                ul = h2.find_next(lambda tag: tag.name == "ul")
                if not ul:
                    logger.debug(f"Список не найден для секции: {section_name}")
                    continue
                formatted_items = []
                for li in ul.find_all("li"):
                    text = li.get_text(strip=True, separator=" ")
                    logger.debug(f"Обрабатываем элемент списка: {text}")
                    qty_match = re.search(r'\s*x(\d+)\s*$', text, re.IGNORECASE)
                    qty = qty_match.group(1) if qty_match else ""
                    name = re.sub(r'\s*x\d+\s*$', '', text, flags=re.IGNORECASE).strip()
                    if not name:
                        logger.debug("Имя предмета пустое, пропуск")
                        continue
                    spans = li.find_all("span")
                    if len(spans) >= 1:
                        name = spans[0].get_text(strip=True)
                        logger.debug(f"Имя из span: {name}")
                        if len(spans) == 2:
                            qty = spans[1].get_text(strip=True).replace("x", "").strip()
                            logger.debug(f"Количество из span: {qty}")
                    normalized_name = normalize_item_name(name)
                    logger.debug(f"Нормализованное имя: {normalized_name}")
                    translated_name = ITEM_TRANSLATE.get(normalized_name, normalized_name)
                    logger.debug(f"Переведённое имя: {translated_name}")
                    if translated_name not in ALLOWED_ITEMS:
                        logger.debug(f"Предмет {translated_name} не в ALLOWED_ITEMS, пропуск")
                        continue
                    emoji = ITEM_EMOJI.get(translated_name, SECTION_EMOJI.get(section_name, ""))
                    color_emoji = egg_colors.get(translated_name, "")
                    if qty:
                        formatted_items.append({"name": translated_name, "emoji": f"{emoji}{color_emoji}", "qty": f"x{qty}"})
                    else:
                        formatted_items.append({"name": translated_name, "emoji": f"{emoji}{color_emoji}", "qty": ""})
                    logger.debug(f"Добавлен предмет: {translated_name}, эмодзи: {emoji}{color_emoji}, количество: {qty}")
                stocks[section_name] = formatted_items
            if not any(stocks[section] for section in SELECTABLE_SECTIONS):
                logger.warning(f"Пустой сток на попытке {attempt + 1}, повтор через 5 сек")
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                logger.error("Все попытки дали пустой сток")
                return {section: [] for section in SECTION_LIST}
            logger.debug(f"Получен сток: {json.dumps(stocks, ensure_ascii=False)}")
            return stocks
        except Exception as e:
            logger.warning(f"Ошибка получения стока на попытке {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            logger.error(f"Не удалось получить сток после {max_retries} попыток: {str(e)}")
            return {section: [] for section in SECTION_LIST}

def load_json_file(path: str, default: Dict) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            logger.debug(f"Загружен файл {path}: {data}")
            return data
    except Exception as e:
        logger.warning(f"Ошибка загрузки {path}: {str(e)}, используется значение по умолчанию")
        return default

def save_json_file(path: str, data: Dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"Сохранён файл {path}: {data}")
    except Exception as e:
        logger.error(f"Ошибка сохранения {path}: {str(e)}")

async def check_bot_permissions(bot: Bot, chat_id: str) -> bool:
    try:
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        logger.debug(f"Права бота в {chat_id}: {bot_member}")
        if bot_member.status not in ['administrator', 'creator']:
            logger.error(f"Бот не админ в {chat_id}")
            await bot.send_message(ADMIN_ID, f"⚠️ Бот не админ в {chat_id}")
            return False
        if not bot_member.can_post_messages or not bot_member.can_delete_messages:
            logger.error(f"Недостаточно прав в {chat_id}")
            await bot.send_message(ADMIN_ID, f"⚠️ Недостаточно прав в {chat_id}")
            return False
        return True
    except TelegramError as e:
        logger.error(f"Ошибка проверки прав в {chat_id}: {str(e)}")
        await bot.send_message(ADMIN_ID, f"⚠️ Ошибка проверки прав в {chat_id}: {str(e)}")
        return False

async def send_sticker_stock(bot: Bot, chat_id: str, stocks: Dict, last_msgs: Dict, last_sent_periods: Dict) -> Dict:
    sticker_ids = load_json_file(STICKER_IDS_FILE, {item: "" for item in ALLOWED_ITEMS})
    logger.debug(f"Загружены стикеры: {sticker_ids}")
    
    # Проверка актуальности периодов
    current_time = datetime.now()
    for section in SELECTABLE_SECTIONS:
        last_period = last_sent_periods.get(section, "")
        if last_period:
            try:
                end_time_str = last_period.split("-")[1]
                end_time = datetime.strptime(end_time_str, "%H:%M").replace(
                    year=current_time.year, month=current_time.month, day=current_time.day
                )
                if end_time < current_time - timedelta(days=1):
                    end_time += timedelta(days=1)
                if current_time > end_time:
                    logger.debug(f"Период {last_period} для {section} завершён, очищаем")
                    last_sent_periods[section] = ""
            except ValueError as e:
                logger.error(f"Ошибка парсинга периода {last_period} для {section}: {e}")
                last_sent_periods[section] = ""  # Очищаем при ошибке

    for section in SELECTABLE_SECTIONS:
        items = stocks.get(section, [])
        if not items:
            logger.debug(f"Нет предметов в секции {section}")
            continue
        current_period = get_period_block(section)
        last_sent = last_sent_periods.get(section, "")
        logger.debug(f"Секция {section}: текущий период {current_period}, последний отправленный {last_sent}")
        
        if last_sent == current_period:
            logger.info(f"Секция {section} уже отправлена в период {current_period}, пропуск")
            continue
        
        section_key = section.lower().replace(" ", "_")
        if section_key in last_msgs and last_msgs[section_key]:
            for attempt in range(5):
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=last_msgs[section_key])
                    logger.debug(f"Удалено сообщение секции {section} в {chat_id}, ID: {last_msgs[section_key]}")
                    break
                except TelegramError as e:
                    logger.warning(f"Попытка {attempt + 1} удаления секции {section} в {chat_id} не удалась: {str(e)}")
                    if attempt == 4:
                        logger.error(f"Не удалось удалить сообщение секции {section}: {str(e)}")
                    await asyncio.sleep(1)
        
        try:
            message_ids = []
            for item in items:
                sticker_id = sticker_ids.get(item['name'], "")
                if sticker_id:
                    try:
                        msg = await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
                        message_ids.append(msg.message_id)
                        logger.debug(f"Отправлен стикер для {item['name']} в {chat_id}, ID: {msg.message_id}")
                    except TelegramError as e:
                        logger.error(f"Ошибка отправки стикера для {item['name']}: {str(e)}")
                else:
                    logger.debug(f"Нет стикера для {item['name']}, пропущено")
            if message_ids:
                last_msgs[section_key] = message_ids[0]
                last_sent_periods[section] = current_period
                logger.info(f"Отправлены стикеры для секции {section} в {chat_id}, первый ID: {message_ids[0]}, период: {current_period}")
            else:
                logger.warning(f"Не отправлено ни одного стикера для секции {section} в {chat_id}")
        except TelegramError as e:
            logger.error(f"Ошибка отправки секции {section} в {chat_id}: {str(e)}")
            await bot.send_message(ADMIN_ID, f"⚠️ Ошибка отправки секции {section} в {chat_id}: {str(e)}")
    
    # Сохраняем обновлённые данные
    save_json_file(LAST_SENT_PERIODS_FILE, last_sent_periods)
    return last_msgs

async def start(update: Update, context):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    command = update.message.text
    logger.debug(f"Получена команда {command} от {user_id} в чате {chat_id}, ADMIN_ID: {ADMIN_ID}")
    if command not in [f"/start", f"/start{BOT_USERNAME}"]:
        logger.debug(f"Игнорируем неподдерживаемую команду: {command}")
        return
    if user_id != ADMIN_ID:
        logger.debug(f"Отклонено: user_id ({user_id}) не совпадает с ADMIN_ID ({ADMIN_ID})")
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text(
        f"👋 Привет! Ты в чате {chat_id}. Используй:\n/add — добавить стикеры\n/change — заменить стикеры\n/check_stickers — проверить стикеры"
    )

async def add_sticker(update: Update, context):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    command = update.message.text
    logger.debug(f"Получена команда {command} от {user_id} в чате {chat_id}")
    if command not in [f"/add", f"/add{BOT_USERNAME}"]:
        logger.debug(f"Игнорируем неподдерживаемую команду: {command}")
        return
    if user_id != ADMIN_ID:
        logger.debug(f"Отклонено: user_id ({user_id}) не совпадает с ADMIN_ID ({ADMIN_ID})")
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"sticker_{name}")]
        for name in sorted(ALLOWED_ITEMS)
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📌 Выбери предмет:", reply_markup=reply_markup)

async def change_sticker(update: Update, context):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    command = update.message.text
    logger.debug(f"Получена команда {command} от {user_id} в чате {chat_id}")
    if command not in [f"/change", f"/change{BOT_USERNAME}"]:
        logger.debug(f"Игнорируем неподдерживаемую команду: {command}")
        return
    if user_id != ADMIN_ID:
        logger.debug(f"Отклонено: user_id ({user_id}) не совпадает с ADMIN_ID ({ADMIN_ID})")
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"sticker_{name}")]
        for name in sorted(ALLOWED_ITEMS)
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📌 Выбери предмет для замены:", reply_markup=reply_markup)

async def check_stickers(update: Update, context):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    command = update.message.text
    logger.debug(f"Получена команда {command} от {user_id} в чате {chat_id}")
    if command not in [f"/check_stickers", f"/check_stickers{BOT_USERNAME}"]:
        logger.debug(f"Игнорируем неподдерживаемую команду: {command}")
        return
    if user_id != ADMIN_ID:
        logger.debug(f"Отклонено: user_id ({user_id}) не совпадает с ADMIN_ID ({ADMIN_ID})")
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    sticker_ids = load_json_file(STICKER_IDS_FILE, {item: "" for item in ALLOWED_ITEMS})
    await update.message.reply_text("📋 Проверка стикеров:")
    for item in sorted(ALLOWED_ITEMS):
        sticker_id = sticker_ids.get(item, "")
        if sticker_id:
            await update.message.reply_text(f"✅ {item}:")
            try:
                await update.message.reply_sticker(sticker=sticker_id)
                logger.debug(f"Показан стикер для {item} в чате {chat_id}")
            except TelegramError as e:
                await update.message.reply_text(f"❌ Ошибка показа стикера для {item}: {str(e)}")
                logger.error(f"Ошибка показа стикера для {item}: {str(e)}")
        else:
            await update.message.reply_text(f"❌ {item}: Стикер отсутствует")
            logger.debug(f"Стикер отсутствует для {item} в {chat_id}")

async def sticker_callback(update: Update, context):
    user_id = str(update.effective_user.id)
    logger.debug(f"Callback от {user_id}: {update.callback_query.data}")
    if user_id != ADMIN_ID:
        await update.callback_query.answer("⛔ Доступ запрещён.")
        return
    query = update.callback_query
    item_name = query.data.replace("sticker_", "")
    if item_name not in ALLOWED_ITEMS:
        await query.answer("❌ Неверный предмет.")
        return
    waiting_sticker[user_id] = item_name
    await query.message.reply_text(f"📸 Отправь стикер для '{item_name}'.")
    await query.answer()

async def handle_sticker(update: Update, context):
    user_id = str(update.effective_user.id)
    logger.debug(f"Получен стикер от {user_id}")
    if user_id != ADMIN_ID:
        return
    if user_id not in waiting_sticker:
        await update.message.reply_text("❓ Сначала выбери предмет через /add или /change.")
        return
    item_name = waiting_sticker[user_id]
    sticker = update.message.sticker
    if not sticker:
        await update.message.reply_text("❌ Это не стикер.")
        return
    sticker_id = sticker.file_id
    sticker_ids = load_json_file(STICKER_IDS_FILE, {item: "" for item in ALLOWED_ITEMS})
    sticker_ids[item_name] = sticker_id
    save_json_file(STICKER_IDS_FILE, sticker_ids)
    await update.message.reply_text(f"✅ Стикер для '{item_name}' сохранён!")
    logger.debug(f"Стикер сохранён для {item_name}: {sticker_id}")
    del waiting_sticker[user_id]

async def debug_update(update: Update, context):
    logger.debug(f"Получено обновление: {update.to_dict()}")

async def update_stock(app: Application):
    global last_update_time
    async with update_lock:
        current_time = time.time()
        if current_time - last_update_time < STOCK_CHECK_INTERVAL - 5:
            logger.debug(f"Пропущена проверка: слишком рано (прошло {current_time - last_update_time:.1f} сек)")
            return
        last_update_time = current_time
        bot = app.bot
        start_time = time.time()
        logger.info("Проверка стока начата")
        try:
            stocks = get_stock()
            if not any(stocks[section] for section in SELECTABLE_SECTIONS):
                logger.warning("Сток пустой, пропуск публикации")
                return
            current_hash = get_stock_hash(stocks)
            prev_stock = load_json_file(PREV_STOCK_FILE, {section: [] for section in SECTION_LIST})
            prev_hash = get_stock_hash(prev_stock)
            logger.debug(f"Текущий сток (хэш: {current_hash}): {json.dumps(normalize_stock(stocks), ensure_ascii=False)}")
            logger.debug(f"Предыдущий сток (хэш: {prev_hash}): {json.dumps(normalize_stock(prev_stock), ensure_ascii=False)}")
            if current_hash == prev_hash:
                logger.info("Сток не изменился, пропуск публикации")
                return
            if await check_bot_permissions(bot, STOCK_CHANNEL_ID):
                last_msgs = load_json_file(MSG_ID_FILE, {section.lower().replace(" ", "_"): None for section in SELECTABLE_SECTIONS})
                last_sent_periods = load_json_file(LAST_SENT_PERIODS_FILE, {section: "" for section in SELECTABLE_SECTIONS})
                logger.debug(f"Загружены last_sent_periods: {last_sent_periods}")
                last_msgs = await send_sticker_stock(bot, STOCK_CHANNEL_ID, stocks, last_msgs, last_sent_periods)
                save_json_file(MSG_ID_FILE, last_msgs)
                save_json_file(LAST_SENT_PERIODS_FILE, last_sent_periods)
                save_json_file(PREV_STOCK_FILE, stocks)
                logger.info(f"Сток обновлён и отправлен за {time.time() - start_time:.2f} сек")
            else:
                logger.error("Публикация невозможна: бот не имеет прав")
        except Exception as e:
            logger.error(f"Ошибка обновления стока: {str(e)}")
            await asyncio.sleep(1)

async def main():
    logger.info("Запуск бота")
    if not TELEGRAM_BOT_TOKEN or "YOUR_STICKER_BOT_TOKEN_HERE" in TELEGRAM_BOT_TOKEN:
        logger.error("Токен не указан")
        return
    try:
        # Сбрасываем prev_stock.json и last_sent_periods.json при старте для теста
        save_json_file(PREV_STOCK_FILE, {section: [] for section in SECTION_LIST})
        save_json_file(LAST_SENT_PERIODS_FILE, {section: "" for section in SELECTABLE_SECTIONS})
        logger.info(f"Файлы {PREV_STOCK_FILE} и {LAST_SENT_PERIODS_FILE} сброшены при старте")
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start, filters=filters.COMMAND))
        app.add_handler(CommandHandler("add", add_sticker, filters=filters.COMMAND))
        app.add_handler(CommandHandler("change", change_sticker, filters=filters.COMMAND))
        app.add_handler(CommandHandler("check_stickers", check_stickers, filters=filters.COMMAND))
        app.add_handler(CallbackQueryHandler(sticker_callback, pattern="^sticker_"))
        app.add_handler(MessageHandler(filters.Sticker.ALL & filters.User(user_id=int(ADMIN_ID)), handle_sticker))
        app.add_handler(MessageHandler(filters.ALL, debug_update))
        logger.info("Обработчики добавлены")
        async with app:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Бот успешно запущен")
            while True:
                await update_stock(app)
                await asyncio.sleep(STOCK_CHECK_INTERVAL)
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {str(e)}")

if __name__ == "__main__":
    asyncio.run(main())