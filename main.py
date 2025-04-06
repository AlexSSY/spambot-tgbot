import os
import asyncio
from pathlib import Path
from aiogram import Bot, Dispatcher, types
from aiogram.types import InputFile
from aiogram.utils import executor
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError

load_dotenv()

# Bot and Telegram API credentials
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")
SESSIONS_DIR = "sessions"

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Храним временно сообщение
messages_to_send = {}


@dp.message_handler(commands=['start'])
async def start_handler(message: types.Message):
    await message.answer("Отправь мне сообщение с картинкой, и я разошлю его во все группы всех аккаунтов.")


async def broadcast_to_groups(image_path: str, caption: str):
    for session_file in os.listdir(SESSIONS_DIR):
        try:
            print(f"Запуск сессии: {session_file}")
            session_path = os.path.join(SESSIONS_DIR, session_file)
            session_string = ""
            with open(session_path, "r") as f:
                session_string = f.read()
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.start()

            # Получаем список чатов
            dialogs = await client(GetDialogsRequest(
                offset_date=None,
                offset_id=0,
                offset_peer=InputPeerEmpty(),
                limit=100,
                hash=0
            ))

            for chat in dialogs.chats:
                if getattr(chat, "megagroup", False):  # только супергруппы
                    try:
                        print(f"Отправка в: {chat.title}")
                        await client.send_file(chat.id, image_path, caption=caption)
                    except Exception as e:
                        print(f"❌ Ошибка в {chat.title}: {e}")

            await client.disconnect()

        except Exception as e:
            print(f"❌ Не удалось запустить сессию {session_file}: {e}")

        finally:
            await client.disconnect()


async def is_session_valid(session_path):
    try:
        session_string = ""
        with open(session_path, "r") as f:
            session_string = f.read()
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            await client.disconnect()
            return True
        await client.disconnect()
    except Exception as e:
        pass
    return False


@dp.message_handler(commands=['sessions'])
async def list_sessions(message: types.Message):
    valid_sessions = []
    for session in os.listdir(SESSIONS_DIR):
        session_path = os.path.join(SESSIONS_DIR, session)
        if await is_session_valid(session_path):
            valid_sessions.append(session)
    if valid_sessions:
        await message.answer("✅ Valid sessions:\n" + "\n".join(valid_sessions))
    else:
        await message.answer("❌ No valid sessions found.")


@dp.message_handler(commands=['cleanup_sessions'])
async def cleanup_sessions(message: types.Message):
    removed = []
    for session in os.listdir(SESSIONS_DIR):
        session_path = os.path.join(SESSIONS_DIR, session)
        if not await is_session_valid(session_path):
            try:
                os.remove(session_path)
                removed.append(session)
            except Exception:
                pass
    if removed:
        await message.answer("🗑 Removed invalid sessions:\n" + "\n".join(removed))
    else:
        await message.answer("👍 All sessions are valid.")


class AddSessionStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()


@dp.message_handler(commands=['add_session'])
async def add_session_start(message: types.Message):
    await message.answer("Введите номер телефона с международным кодом (например, +1234567890):")
    await AddSessionStates.waiting_for_phone.set()


@dp.message_handler(state=AddSessionStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone=phone)

    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()

        phone_code_hash = (await client.send_code_request(phone)).phone_code_hash
        await state.update_data(client=client.session.save(), phone_code_hash=phone_code_hash)  # сохраняем session string
        await message.answer("Введите код из Telegram:")
        await AddSessionStates.waiting_for_code.set()
    except PhoneNumberInvalidError:
        await message.answer("Неверный номер телефона")


@dp.message_handler(state=AddSessionStates.waiting_for_code)
async def process_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    phone = data["phone"]
    session_str = data["client"]
    phone_code_hash = data["phone_code_hash"]

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        await state.update_data(client=client.session.save())
        await message.answer("На аккаунте включена двухэтапная аутентификация. Введите пароль:")
        await AddSessionStates.waiting_for_password.set()
        return
    except Exception as e:
        await message.answer(f"Ошибка при входе: {e}")
        await state.finish()
        return

    await save_session_and_finish(client, phone, message, state)


@dp.message_handler(state=AddSessionStates.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    phone = data["phone"]
    session_str = data["client"]

    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()

    try:
        await client.sign_in(password=password)
    except Exception as e:
        await message.answer(f"Ошибка при вводе пароля: {e}")
        await state.finish()
        return

    await save_session_and_finish(client, phone, message, state)


async def save_session_and_finish(client: TelegramClient, phone: str, message: types.Message, state: FSMContext):
    session_str = client.session.save()
    phone_safe = phone.replace("+", "").replace(" ", "")
    with open(f"{SESSIONS_DIR}/{phone_safe}.session", "w") as f:
        f.write(session_str)

    await client.disconnect()
    await message.answer(f"✅ Сессия сохранена для {phone}")
    await state.finish()


@dp.message_handler(content_types=['photo', 'text'])
async def message_handler(message: types.Message):
    if not message.photo:
        return await message.answer("Нужна картинка. Пришли сообщение с фото и подписью.")

    caption = message.caption or ""
    file_id = message.photo[-1].file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path

    # Сохраняем изображение
    await bot.download_file(file_path, "image_to_send.jpg")

    # Сохраняем текст
    messages_to_send[message.from_user.id] = caption

    await message.answer("Начинаю рассылку...")

    # Рассылка
    await broadcast_to_groups("image_to_send.jpg", caption)

    await message.answer("Готово ✅")

# Запуск бота
if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
