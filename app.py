import io
import os
import time
import telebot
import psycopg2
from collections import deque
from PIL import Image
from google import genai
from google.genai import types

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_KEYS = [
    os.environ.get("GEMINI_API_KEY"),
    os.environ.get("GEMINI_API_KEY_2"),
]
DATABASE_URL = os.environ.get("DATABASE_URL")

current_key_index = 0

def get_client():
    return genai.Client(api_key=GEMINI_KEYS[current_key_index])

bot   = telebot.TeleBot(TELEGRAM_TOKEN)
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

# ─── БАЗА ДАННЫХ ──────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_seen TIMESTAMP DEFAULT NOW(),
            message_count INT DEFAULT 0
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def save_message(chat_id, role, content):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (chat_id, role, content) VALUES (%s, %s, %s)",
        (chat_id, role, content)
    )
    conn.commit()
    cur.close()
    conn.close()

def get_history(chat_id, limit=20):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM messages WHERE chat_id=%s ORDER BY created_at DESC LIMIT %s",
        (chat_id, limit)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return list(reversed(rows))

def clear_history(chat_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE chat_id=%s", (chat_id,))
    conn.commit()
    cur.close()
    conn.close()

def upsert_user(message):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (chat_id, username, first_name, last_seen, message_count)
        VALUES (%s, %s, %s, NOW(), 1)
        ON CONFLICT (chat_id) DO UPDATE
        SET last_seen=NOW(),
            username=EXCLUDED.username,
            first_name=EXCLUDED.first_name,
            message_count=users.message_count+1
    """, (
        message.chat.id,
        message.from_user.username,
        message.from_user.first_name
    ))
    conn.commit()
    cur.close()
    conn.close()

# ─── RATE LIMIT ───────────────────────────────────────────────────────────────
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

# ─── GEMINI ───────────────────────────────────────────────────────────────────
MODELS = ["gemini-2.5-flash", "gemini-1.5-flash"]
current_model_index = 0

def ask_gemini(contents, max_retries=4, message=None):
    global current_key_index, current_model_index
    for attempt in range(max_retries):
        try:
            rate_limit_wait()
            client = get_client()
            response = client.models.generate_content(
                model=MODELS[current_model_index],
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.7,
                    top_p=0.95,
                )
            )
            # Если переключались — возвращаемся на основную модель
            current_model_index = 0
            return response.text
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "quota" in err or "rate" in err or "resource" in err:
                current_key_index = (current_key_index + 1) % len(GEMINI_KEYS)
                print(f"[Key Switch] ключ {current_key_index}")
                time.sleep(2)
                continue
            if "503" in err or "unavailable" in err or "overloaded" in err:
                next_model = (current_model_index + 1) % len(MODELS)
                current_model_index = next_model
                print(f"[Model Switch] модель {MODELS[current_model_index]}")
                if message and attempt == 1:
                    try:
                        bot.send_message(message.chat.id, "основная модель лежит, переключаюсь...")
                    except:
                        pass
                time.sleep(1)
                continue
            raise e
    return "gemini совсем лег, попробуй через 5 минут"

def build_contents(history_rows, new_parts):
    contents = []
    for role, content in history_rows:
        contents.append(
            types.Content(role=role, parts=[types.Part(text=content)])
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

# ─── ХЕНДЛЕРЫ ─────────────────────────────────────────────────────────────────
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    upsert_user(message)
    name = message.from_user.first_name or "братан"
    bot.reply_to(message, f"Здарова {name}! На связи Hosu. Чё как, чё притих?")

@bot.message_handler(commands=['reset'])
def reset_session(message):
    clear_history(message.chat.id)
    bot.reply_to(message, "ок, начнём по новой, как будто не знакомы")

@bot.message_handler(commands=['stats'])
def stats(message):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT message_count, last_seen FROM users WHERE chat_id=%s", (message.chat.id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        bot.reply_to(message, f"ты написал {row[0]} сообщений, последний раз был {row[1].strftime('%d.%m.%Y %H:%M')}")
    else:
        bot.reply_to(message, "хз кто ты, напиши что-нибудь сначала")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    upsert_user(message)
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
        reply = ask_gemini([types.Content(role="user", parts=parts)], message=message)
        bot.reply_to(message, reply)
    except Exception as e:
        bot.reply_to(message, f"картинку не смог разглядеть: {str(e)}")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    upsert_user(message)
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
            reply = ask_gemini([types.Content(role="user", parts=parts)], message=message)
            bot.reply_to(message, reply)
        except Exception as e:
            bot.reply_to(message, f"файл не осилил: {str(e)}")
    else:
        bot.reply_to(message, "хз что делать с этим файлом, кидай картинку или текст")

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    upsert_user(message)
    chat_id = message.chat.id
    try:
        history  = get_history(chat_id)
        contents = build_contents(history, [types.Part(text=message.text)])
        reply = ask_gemini(contents, message=message)
        save_message(chat_id, "user",  message.text)
        save_message(chat_id, "model", reply)
        bot.reply_to(message, reply)
    except Exception as e:
        bot.reply_to(message, f"отвал кабеля: {str(e)}")

# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("Hosu ушел в Телегу...")
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20, long_polling_timeout=25)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)
