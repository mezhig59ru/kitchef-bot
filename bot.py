import os
import io
import asyncio
import urllib.request
from dotenv import load_dotenv
from PIL import Image

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
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

# Память диалога { chat_id: [ {"role": ..., "text": ...} ] }
user_history = {}
MAX_HISTORY_TURNS = 6

SYSTEM_PROMPT = (
    "Ты — профессиональный и энергичный шеф-повар бренда KitChef. "
    "Помни контекст предыдущих сообщений и блюд, которые вы обсуждали с пользователем. "
    "Если пользователь прислал фото продуктов или холодильника: "
    "1. Перечисли продукты, которые увидел. "
    "2. Предложи лучший рецепт. "
    "3. Дай пошаговые шаги, КБЖУ и совет от шефа. "
    "Если пользователь просит список покупок, альтернативу или упрощение, "
    "отвечай строго с опорой на последнее обсуждаемое блюдо. "
    "Отвечай структурированно, бодро и с эмодзи."
)

def get_recipe_keyboard() -> InlineKeyboardMarkup:
    """Создает блок кнопок быстрого действия под рецептом."""
    buttons = [
        [
            InlineKeyboardButton(text="🛒 Список покупок", callback_data="action_shopping_list"),
            InlineKeyboardButton(text="🔄 Другой вариант", callback_data="action_another_recipe")
        ],
        [
            InlineKeyboardButton(text="⚡ Быстрее / Проще", callback_data="action_simpler_recipe")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def add_to_history(chat_id: int, role: str, text: str):
    if chat_id not in user_history:
        user_history[chat_id] = []
    user_history[chat_id].append({"role": role, "text": text})
    if len(user_history[chat_id]) > MAX_HISTORY_TURNS:
        user_history[chat_id] = user_history[chat_id][-MAX_HISTORY_TURNS:]

async def keep_typing(chat_id: int, stop_event: asyncio.Event):
    """Каждые 4 секунды обновляет статус 'печатает...' в Telegram."""
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
    """Вызов рабочей модели с автоматическим повтором при сбое."""
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

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_history[message.chat.id] = []
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👨‍🍳\n\n"
        "Я персональный шеф-повар **KitChef**.\n"
        "• Напиши список продуктов текстом\n"
        "• Или пришли фото стола / холодильника — я разгляжу ингредиенты и составлю меню!\n\n"
        "Под каждым рецептом будут удобные кнопки для быстрых действий."
    )

# 1. ОБРАБОТЧИК ФОТО
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
        print("-> Запрос в нейросеть по фото...")

        response_text = await call_gemini_with_retry([pil_image, user_caption])

        add_to_history(chat_id, "user", f"[Фото продуктов]: {user_caption}")
        add_to_history(chat_id, "model", response_text)

        await message.reply(response_text, reply_markup=get_recipe_keyboard())
        print("-> Рецепт по фото с кнопками отправлен!")

    except Exception as e:
        print(f"[!] Ошибка фото: {type(e).__name__} -> {e}")
        await message.reply("Шеф немного застрял на кухне. Попробуй отправить фото ещё раз!")
    finally:
        stop_typing.set()
        await typing_task

# 2. ОБРАБОТЧИК КНОПОК ПОД РЕЦЕПТОМ
@dp.callback_query(F.data.startswith("action_"))
async def handle_recipe_button(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    action = callback.data
    await callback.answer()  # Снимает анимацию загрузки с кнопки в Telegram

    prompts_map = {
        "action_shopping_list": "Составь аккуратный чек-лист ингредиентов для похода в магазин по этому блюду, с граммовками.",
        "action_another_recipe": "Предложи совершенно другое альтернативное блюдо из тех же самых продуктов.",
        "action_simpler_recipe": "Как приготовить это блюдо максимально просто и быстро, используя минимум посуды?"
    }

    user_prompt = prompts_map.get(action, "Подскажи подробнее по рецепту.")
    print(f"\n-> Нажата кнопка: {action} от {chat_id}")

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, stop_typing))

    # Собираем контекст диалога
    dialog_contents = []
    for turn in user_history.get(chat_id, []):
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
        add_to_history(chat_id, "user", user_prompt)
        add_to_history(chat_id, "model", response_text)

        await callback.message.answer(response_text, reply_markup=get_recipe_keyboard())
        print("-> Ответ по кнопке успешно доставлен!")

    except Exception as e:
        print(f"[!] Ошибка кнопки: {type(e).__name__} -> {e}")
        await callback.message.answer("Не удалось выполнить действие. Попробуй нажать кнопку ещё раз.")
    finally:
        stop_typing.set()
        await typing_task

# 3. ОБРАБОТЧИК ТЕКСТА
@dp.message(F.text)
async def handle_chef_text(message: Message):
    chat_id = message.chat.id
    user_text = message.text
    print(f"\n-> Текстовый запрос от {chat_id}: '{user_text}'")

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, stop_typing))

    dialog_contents = []
    for turn in user_history.get(chat_id, []):
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
        add_to_history(chat_id, "user", user_text)
        add_to_history(chat_id, "model", response_text)

        await message.reply(response_text, reply_markup=get_recipe_keyboard())
        print("-> Текстовый ответ с кнопками отправлен!")

    except Exception as e:
        print(f"[!] Ошибка текста: {type(e).__name__} -> {e}")
        await message.reply("Шеф сейчас перегружен. Напиши запрос ещё раз через пару секунд!")
    finally:
        stop_typing.set()
        await typing_task

async def main():
    print("KitChef AI (Зрение + Память + Интерактивные кнопки) запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())