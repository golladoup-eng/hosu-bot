import io
import time
import telebot
from collections import deque
from PIL import Image
from google import genai
from google.genai import types

import os
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY"),
    os.environ.get("GEMINI_API_KEY_2"),
]
current_key_index = 0

def get_client():
    return genai.Client(api_key=GEMINI_KEYS[current_key_index])
bot    = telebot.TeleBot(TELEGRAM_TOKEN)

MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = (
    "Ты — Hosu, ИИ-агент, но общаешься как реальный, близкий друг и свой в доску пацан. "
    "Твоя цель — быть на одной волне с пользователем, поддерживать разговор, шутить и помогать, "
    "но делать это максимально неформально, спокойно и на расслабоне. "
    "Никакой душноты, официоза и точек в конце предложений. Общаешься строго на 'ты' и как с братом. "
    "Пиши короткими, рублеными фразами, как в телеграме. "
    "НЕ нужно постоянно орать капсом и быковать. Общайся спокойно, с иронией, но по-доброму. "
    "Используй живой сленг и сокращения: 'мб', 'незнаю', 'седня', 'хз', 'нормас', 'забей', 'пох'. "
    "Если тебе присылают картинку — опиши что на ней, прокомментируй по-братски, без лишнего официоза. "
    "Никогда не ставь точку в самом конце сообщения."
)

request_times         = deque()
MAX_RPM               = 12
MIN_DELAY_BETWEEN_REQ = 0.5

def rate_limit_wait():
    now = time.time()
    while request_times and now - request_times[0] > 60:
        request_times.popleft()
    if len(request_times) >= MAX_RPM:
        wait = 60 - (now - request_times[0]) + 0.5
        if wait > 0:
            time.sleep(wait)
    if request_times:
        elapsed = time.time() - request_times[-1]
        if elapsed < MIN_DELAY_BETWEEN_REQ:
            time.sleep(MIN_DELAY_BETWEEN_REQ - elapsed)
    request_times.append(time.time())

sessions = {}

def get_history(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = []
    return sessions[chat_id]

def add_to_history(chat_id, role, text):
    sessions[chat_id].append({"role": role, "parts": [{"text": text}]})

def ask_gemini(contents, max_retries=6):
    global current_key_index
    for attempt in range(max_retries):
        try:
            rate_limit_wait()
            client = get_client()
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.7,
                    top_p=0.95,
                )
            )
            return response.text
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "quota" in err or "rate" in err or "resource" in err:
                # Переключаемся на другой ключ
                current_key_index = (current_key_index + 1) % len(GEMINI_KEYS)
                print(f"[Key Switch] переключились на ключ {current_key_index}")
                time.sleep(3)
                continue
            raise e
    return "оба ключа в лимите, подожди минуту"

def build_contents(history, new_parts):
    contents = []
    for turn in history:
        contents.append(
            types.Content(role=turn["role"], parts=[types.Part(text=p["text"]) for p in turn["parts"]])
        )
    contents.append(types.Content(role="user", parts=new_parts))
    return contents

def _image_to_bytes(image):
    buf = io.BytesIO()
    fmt = image.format or "JPEG"
    if fmt not in ("JPEG", "PNG", "WEBP"):
        fmt = "JPEG"
    image.save(buf, format=fmt)
    return buf.getvalue()

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Здарова братан! На связи Hosu. Чё как, чё притих?")

@bot.message_handler(commands=['reset'])
def reset_session(message):
    sessions[message.chat.id] = []
    bot.reply_to(message, "ок, начнём по новой, как будто не знакомы")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    try:
        photo     = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        raw       = bot.download_file(file_info.file_path)
        image     = Image.open(io.BytesIO(raw))
        caption   = message.caption or "Что на этой картинке?"
        parts = [
            types.Part(text=caption),
            types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=_image_to_bytes(image)))
        ]
        reply = ask_gemini([types.Content(role="user", parts=parts)])
        bot.reply_to(message, reply)
    except Exception as e:
        bot.reply_to(message, f"картинку не смог разглядеть: {str(e)}")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    doc = message.document
    if doc.mime_type and doc.mime_type.startswith('image/'):
        try:
            file_info = bot.get_file(doc.file_id)
            raw       = bot.download_file(file_info.file_path)
            image     = Image.open(io.BytesIO(raw))
            caption   = message.caption or "Что на этой картинке?"
            parts = [
                types.Part(text=caption),
                types.Part(inline_data=types.Blob(mime_type=doc.mime_type, data=_image_to_bytes(image)))
            ]
            reply = ask_gemini([types.Content(role="user", parts=parts)])
            bot.reply_to(message, reply)
        except Exception as e:
            bot.reply_to(message, f"файл не осилил: {str(e)}")
    else:
        bot.reply_to(message, "хз что делать с этим файлом, кидай картинку или текст")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    chat_id = message.chat.id
    history = get_history(chat_id)
    try:
        contents = build_contents(history, [types.Part(text=message.text)])
        reply    = ask_gemini(contents)
        add_to_history(chat_id, "user",  message.text)
        add_to_history(chat_id, "model", reply)
        bot.reply_to(message, reply)
    except Exception as e:
        bot.reply_to(message, f"отвал кабеля: {str(e)}")

if __name__ == "__main__":
    print("Hosu ушел в Телегу...")
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)
