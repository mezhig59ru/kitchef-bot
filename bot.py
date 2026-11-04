import os
import io
import re
import asyncio
import sqlite3
import urllib.request
from dotenv import load_dotenv
from PIL import Image

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from google import genai
from google.genai import types

# Прокси для стабильного соединения через Hiddify
system_proxies = urllib.request.getproxies()
if "https" in system_proxies and "HTTPS_PROXY" not in os.environ:
    os.environ["HTTPS_PROXY"] = system_proxies["https"]
if "http" in system_proxies and "HTTP_PROXY" not in os.environ:
    os.environ["HTTP_PROXY"] = system_proxies["http"]

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

DB_NAME = "kitchef.db"

# --- РАБОТА С БАЗОЙ ДАННЫХ SQLITE ---

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                title TEXT,
                recipe_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def save_history(chat_id: int, role: str, content: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO history (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, content)
        )
        conn.commit()

def get_recent_history(chat_id: int, limit: int = 6):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)
        )
        rows = cursor.fetchall()
        return [{"role": r[0], "text": r[1]} for r in reversed(rows)]

def save_favorite(chat_id: int, title: str, recipe_text: str) -> bool:
    """Сохраняет рецепт. Возвращает False, если такой рецепт уже был сохранён."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # Проверка на дубликат по названию
        cursor.execute(
            "SELECT id FROM favorites WHERE chat_id = ? AND title = ?",
            (chat_id, title)
        )
        if cursor.fetchone():
            return False

        cursor.execute(
            "INSERT INTO favorites (chat_id, title, recipe_text) VALUES (?, ?, ?)",
            (chat_id, title, recipe_text)
        )
        conn.commit()
        return True

def delete_favorite(fav_id: int, chat_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM favorites WHERE id = ? AND chat_id = ?", (fav_id, chat_id))
        conn.commit()

def get_favorites(chat_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title FROM favorites WHERE chat_id = ? ORDER BY id DESC LIMIT 15",
            (chat_id,)
        )
        return cursor.fetchall()

def get_favorite_by_id(fav_id: int, chat_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT title, recipe_text FROM favorites WHERE id = ? AND chat_id = ?",
            (fav_id, chat_id)
        )
        return cursor.fetchone()

def extract_recipe_title(text: str) -> str:
    """Аккуратно извлекает красивое название блюда без лишнего текста."""
    # 1. Ищем строки вида: Рецепт: "Название" или Рецепт: Название
    recipe_match = re.search(r"(?:рецепт|блюдо)[:\s]+[«\"']?([^«\"'\n\*\#]+)[»\"']?", text, re.IGNORECASE)
    if recipe_match:
        found = recipe_match.group(1).strip()
        if len(found) > 3:
            return found[:30]

    # 2. Ищем выделенные жирным фразы в кавычках: **«Блюдо»**
    quotes_match = re.search(r"«([^»\n]+)»", text)
    if quotes_match:
        found = quotes_match.group(1).strip()
        if len(found) > 3:
            return found[:30]

    # 3. Резервный поиск первой жирной строки
    bold_match = re.search(r"\*\*([^\*\n]+)\*\*", text)
    if bold_match:
        candidate = bold_match.group(1).replace("Рецепт:", "").strip()
        if len(candidate) > 3 and not candidate.startswith("Что на фото"):
            return candidate[:30]

    return "Фирменное блюдо KitChef"


# --- КЛАВИАТУРЫ ---

def get_recipe_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="🛒 Список покупок", callback_data="action_shopping_list"),
            InlineKeyboardButton(text="🔄 Другой вариант", callback_data="action_another_recipe")
        ],
        [
            InlineKeyboardButton(text="⚡ Быстрее / Проще", callback_data="action_simpler_recipe"),
            InlineKeyboardButton(text="⭐ В избранное", callback_data="action_save_fav")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_fav_view_keyboard(fav_id: int) -> InlineKeyboardMarkup:
    """Кнопки под открытым рецептом из избранного (с кнопкой удаления)."""
    buttons = [
        [
            InlineKeyboardButton(text="🛒 Список покупок", callback_data="action_shopping_list"),
            InlineKeyboardButton(text="⚡ Быстрее / Проще", callback_data="action_simpler_recipe")
        ],
        [
            InlineKeyboardButton(text="🗑️ Удалить из избранного", callback_data=f"delete_fav_{fav_id}"),
            InlineKeyboardButton(text="📖 К списку рецептов", callback_data="open_fav_list")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- ПРОМПТ И ВЫЗОВ ИИ ---

SYSTEM_PROMPT = (
    "Ты — профессиональный и энергичный шеф-повар бренда KitChef. "
    "Помни контекст предыдущих сообщений и блюд, которые вы обсуждали с пользователем. "
    "В каждом рецепте обязательно пиши чёткое название блюда в формате: **Рецепт: «Название блюда»**. "
    "Если пользователь прислал фото продуктов или холодильника: "
    "1. Перечисли продукты, которые увидел. "
    "2. Предложи лучший рецепт с названием в кавычках. "
    "3. Дай пошаговые шаги, КБЖУ и совет от шефа. "
    "Если пользователь просит список покупок, альтернативу или упрощение, "
    "отвечай строго с опорой на последнее обсуждаемое блюдо. "
    "Отвечай структурированно, бодро и с эмодзи."
)

async def keep_typing(chat_id: int, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass

async def call_gemini_with_retry(contents, retries=2, timeout_sec=45.0) -> str:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model="gemini-flash-lite-latest",
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    )
                ),
                timeout=timeout_sec
            )
            if response.text:
                return response.text
        except Exception as e:
            last_err = e
            print(f"   [!] Попытка {attempt}/{retries} сбой: {type(e).__name__} -> {e}")
            if attempt < retries:
                await asyncio.sleep(2)
    raise last_err


# --- ОБРАБОТЧИКИ КОМАНД И СПИСКА ИЗБРАННОГО ---

async def show_favorites_menu(chat_id: int, target_message: Message = None):
    favs = get_favorites(chat_id)
    if not favs:
        text = (
            "Твоя кулинарная книга пока пуста 📖\n"
            "Нажми кнопку **«⭐ В избранное»** под любым рецептом шефа, чтобы сохранить его сюда!"
        )
        if target_message:
            await target_message.answer(text)
        return

    keyboard_buttons = []
    for fav_id, title in favs:
        keyboard_buttons.append([
            InlineKeyboardButton(text=f"🍽️ {title}", callback_data=f"show_fav_{fav_id}")
        ])

    text = "📖 **Твоя книга избранных рецептов KitChef:**\nНажми на блюдо, чтобы открыть полный рецепт:"
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    if target_message:
        await target_message.answer(text, reply_markup=reply_markup)
    else:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👨‍🍳\n\n"
        "Я твой шеф-повар **KitChef** с постоянной памятью рецептов.\n"
        "• Отправь список продуктов текстом\n"
        "• Или пришли фото стола / холодильника — я составлю меню!\n\n"
        "⭐ Понравившиеся рецепты сохраняй кнопкой «В избранное».\n"
        "📖 Посмотреть сохранённые рецепты: команда /favorites или напиши «мои рецепты»."
    )

@dp.message(Command("favorites"))
async def cmd_favorites(message: Message):
    await show_favorites_menu(message.chat.id, message)

# Перехват обычных сообщений с просьбой показать избранное
@dp.message(F.text.lower().in_({"избранное", "мои рецепты", "книга рецептов", "сохраненные рецепты", "какие у меня избранные рецепты"}))
async def handle_text_favorites_request(message: Message):
    await show_favorites_menu(message.chat.id, message)


# --- ОБРАБОТЧИКИ НАЖАТИЙ НА КНОПКИ ---

@dp.callback_query(F.data == "open_fav_list")
async def cb_open_fav_list(callback: CallbackQuery):
    await callback.answer()
    await show_favorites_menu(callback.message.chat.id, callback.message)

@dp.callback_query(F.data == "action_save_fav")
async def handle_save_fav(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    recipe_text = callback.message.text or callback.message.caption or ""

    if not recipe_text:
        await callback.answer("Не удалось найти рецепт для сохранения.", show_alert=True)
        return

    title = extract_recipe_title(recipe_text)
    saved = save_favorite(chat_id, title, recipe_text)
    
    if saved:
        await callback.answer(f"«{title}» сохранено в избранное! ⭐", show_alert=True)
    else:
        await callback.answer(f"«{title}» уже есть в твоем избранном! 👍", show_alert=True)

@dp.callback_query(F.data.startswith("show_fav_"))
async def handle_show_fav(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    fav_id = int(callback.data.split("_")[-1])
    fav = get_favorite_by_id(fav_id, chat_id)

    if fav:
        title, recipe_text = fav
        # Добавляем в оперативную память шефа, чтобы можно было дальше задавать вопросы по этому блюду
        save_history(chat_id, "model", recipe_text)
        await callback.message.answer(
            f"📖 **{title}**\n\n{recipe_text}",
            reply_markup=get_fav_view_keyboard(fav_id)
        )
    else:
        await callback.answer("Рецепт не найден в базе данных.", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_fav_"))
async def handle_delete_fav(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    fav_id = int(callback.data.split("_")[-1])
    delete_favorite(fav_id, chat_id)
    await callback.answer("Рецепт удалён из избранного! 🗑️", show_alert=True)
    await show_favorites_menu(chat_id, callback.message)

@dp.callback_query(F.data.startswith("action_"))
async def handle_recipe_button(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    action = callback.data
    await callback.answer()

    prompts_map = {
        "action_shopping_list": "Составь аккуратный чек-лист ингредиентов для похода в магазин по этому блюду, с граммовками.",
        "action_another_recipe": "Предложи совершенно другое альтернативное блюдо из тех же самых продуктов.",
        "action_simpler_recipe": "Как приготовить это блюдо максимально просто и быстро, используя минимум посуды?"
    }

    user_prompt = prompts_map.get(action, "Подскажи подробнее по рецепту.")
    print(f"\n-> Нажата кнопка: {action} от {chat_id}")

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, stop_typing))

    dialog_contents = []
    for turn in get_recent_history(chat_id, limit=6):
        dialog_contents.append(
            types.Content(
                role=turn["role"],
                parts=[types.Part.from_text(text=turn["text"])]
            )
        )
    dialog_contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_prompt)]
        )
    )

    try:
        response_text = await call_gemini_with_retry(dialog_contents)
        save_history(chat_id, "user", user_prompt)
        save_history(chat_id, "model", response_text)

        await callback.message.answer(response_text, reply_markup=get_recipe_keyboard())
        print("-> Ответ по кнопке доставлен!")

    except Exception as e:
        print(f"[!] Ошибка кнопки: {type(e).__name__} -> {e}")
        await callback.message.answer("Не удалось выполнить действие. Попробуй ещё раз.")
    finally:
        stop_typing.set()
        await typing_task


# --- ОБРАБОТЧИКИ СООБЩЕНИЙ (ФОТО И ТЕКСТ) ---

@dp.message(F.photo)
async def handle_food_photo(message: Message):
    chat_id = message.chat.id
    print(f"\n-> Получено фото от {chat_id}. Обработка...")

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, stop_typing))

    try:
        photo_info = message.photo[-1]
        photo_bytes_io = io.BytesIO()
        await bot.download(photo_info, destination=photo_bytes_io)
        photo_bytes_io.seek(0)

        pil_image = Image.open(photo_bytes_io)
        pil_image.thumbnail((1024, 1024))

        user_caption = message.caption or "Посмотри, что есть на фото, и предложи лучший рецепт!"
        print("-> Отправка фото в Gemini Vision...")

        response_text = await call_gemini_with_retry([pil_image, user_caption])

        save_history(chat_id, "user", f"[Фото продуктов]: {user_caption}")
        save_history(chat_id, "model", response_text)

        await message.reply(response_text, reply_markup=get_recipe_keyboard())
        print("-> Рецепт по фото отправлен!")

    except Exception as e:
        print(f"[!] Ошибка фото: {type(e).__name__} -> {e}")
        await message.reply("Шеф немного застрял на кухне. Попробуй отправить фото ещё раз!")
    finally:
        stop_typing.set()
        await typing_task

@dp.message(F.text)
async def handle_chef_text(message: Message):
    chat_id = message.chat.id
    user_text = message.text
    print(f"\n-> Текстовый запрос от {chat_id}: '{user_text}'")

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, stop_typing))

    dialog_contents = []
    for turn in get_recent_history(chat_id, limit=6):
        dialog_contents.append(
            types.Content(
                role=turn["role"],
                parts=[types.Part.from_text(text=turn["text"])]
            )
        )
    dialog_contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_text)]
        )
    )

    try:
        response_text = await call_gemini_with_retry(dialog_contents)
        save_history(chat_id, "user", user_text)
        save_history(chat_id, "model", response_text)

        await message.reply(response_text, reply_markup=get_recipe_keyboard())
        print("-> Текстовый ответ с кнопками отправлен!")

    except Exception as e:
        print(f"[!] Ошибка текста: {type(e).__name__} -> {e}")
        await message.reply("Шеф сейчас перегружен. Напиши запрос ещё раз через пару секунд!")
    finally:
        stop_typing.set()
        await typing_task

async def main():
    init_db()
    print("База данных SQLite подключена. KitChef AI слушает Telegram...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())