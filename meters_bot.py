#!/usr/bin/env python3
"""Нагадування передати показники лічильників (комуналка) -> Telegram.

З 2 по 4 число кожного місяця о 17:00 і о 20:30 за Києвом усім отримувачам
приходить нагадування з кнопкою «✅ Передали». Щойно хтось один натисне кнопку,
нагадування цього місяця припиняються для всіх.

Лише стандартна бібліотека Python 3.9+.

Змінні середовища:
  TG_TOKEN      токен бота
  TG_CHAT_ID    chat id отримувачів через кому
  METERS_STATE  файл стану, за замовчуванням meters_state.json
  DRY_RUN=1     лише показати текст, нічого не надсилати і не зберігати

Запуск:
  python meters_bot.py               перевірити, чи час нагадати, і обробити натискання кнопки
  python meters_bot.py --listen 110  те саме, а потім ще 110 с чекати натискань (для циклу)
  python meters_bot.py --demo        показати текст нагадування
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = os.getenv("METERS_STATE") or "meters_state.json"
DAYS = (2, 3, 4)                 # числа місяця
SLOTS = ((17, 0), (20, 30))      # години нагадувань за Києвом
SLOT_WINDOW = 90                 # якщо цикл запізнився, нагадуємо протягом 90 хв після часу
MONTHS = ["січень", "лютий", "березень", "квітень", "травень", "червень",
          "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]

TEXT = ("📋 <b>Почався новий місяць!</b>\n"
        "Серед усіх справ не забудьте передати показники лічильників за комуналку:\n"
        "⚡ світло · 🔥 газ · 💧 вода\n\n"
        "Коли передасте, натисніть кнопку нижче 👇")
BUTTON = "✅ Передали"


# ---------- Telegram ----------

def tg(method, **params):
    token = os.environ["TG_TOKEN"]
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    timeout = params.get("timeout", 0) + 25
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r).get("result")


def chats():
    return [c.strip() for c in (os.getenv("TG_CHAT_ID") or "").split(",") if c.strip()]


# ---------- стан ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(st):
    if os.getenv("DRY_RUN"):
        return
    new = json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            if f.read() == new:
                return
    except FileNotFoundError:
        pass
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(new)


# ---------- логіка ----------

def due_slot(now, st):
    """Повертає ключ слота, якщо зараз час надіслати нагадування, інакше None."""
    month = now.strftime("%Y-%m")
    if now.day not in DAYS or st.get("done") == month:
        return None
    nm = now.hour * 60 + now.minute
    for h, m in SLOTS:
        start = h * 60 + m
        key = f"{now:%Y-%m-%d} {h:02d}:{m:02d}"
        if start <= nm < start + SLOT_WINDOW and key not in st.get("sent", []):
            return key
    return None


def send_reminder(now, st, key):
    month = now.strftime("%Y-%m")
    st["sent"] = [k for k in st.get("sent", []) if k.startswith(month)] + [key]
    if st.get("messages_month") != month:
        st["messages_month"], st["messages"] = month, []
    markup = {"inline_keyboard": [[{"text": BUTTON, "callback_data": f"meters:{month}"}]]}
    if os.getenv("DRY_RUN") or not os.getenv("TG_TOKEN"):
        print(TEXT, f"\n[кнопка: {BUTTON}]")
        return
    targets = chats()
    for n, chat in enumerate(targets, 1):
        try:
            msg = tg("sendMessage", chat_id=chat, text=TEXT, parse_mode="HTML",
                     reply_markup=markup)
            st["messages"].append([chat, msg["message_id"]])
            print(f"Нагадування надіслано отримувачу {n} з {len(targets)}")
        except Exception as e:  # id не друкуємо: логи публічні
            print(f"Не вдалося надіслати отримувачу {n}: {e}", file=sys.stderr)


def mark_done(st, month, who):
    """Хтось натиснув «Передали»: прибрати кнопки і повідомити всіх."""
    st["done"] = month
    for chat, mid in st.get("messages", []) if st.get("messages_month") == month else []:
        try:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=mid,
               reply_markup={"inline_keyboard": []})
        except Exception:
            pass  # старе або вже змінене повідомлення
    name = MONTHS[int(month[5:]) - 1]
    text = (f"✅ <b>Показники за {name} передано.</b> Відмітку зробив(-ла): {who}.\n"
            "Цього місяця нагадувань більше не буде. Дякуємо! 🙏")
    for chat in chats():
        try:
            tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML")
        except Exception as e:
            print("Не вдалося надіслати підтвердження:", e, file=sys.stderr)
    print(f"Показники за {month} відмічено як передані")


def handle_updates(st, wait=0):
    """Забирає нові події від Telegram і обробляє натискання кнопки."""
    allowed = set(chats())
    updates = tg("getUpdates", offset=st.get("offset", 0), timeout=wait,
                 allowed_updates=["callback_query", "message"]) or []
    for u in updates:
        st["offset"] = u["update_id"] + 1
        q = u.get("callback_query")
        if not q or not str(q.get("data", "")).startswith("meters:"):
            continue
        month = q["data"].split(":", 1)[1]
        chat = str(q.get("message", {}).get("chat", {}).get("id", ""))
        if chat not in allowed:
            continue  # кнопку натиснув хтось сторонній
        already = st.get("done") == month
        try:
            tg("answerCallbackQuery", callback_query_id=q["id"],
               text="Вже відмічено, дякуємо!" if already else "Дякуємо! Нагадувань цього місяця більше не буде.")
        except Exception:
            pass  # запит застарів: не страшно
        if not already:
            who = q.get("from", {}).get("first_name") or "хтось із родини"
            mark_done(st, month, who)
    return bool(updates)


def run(listen=0):
    st = load_state()
    now = datetime.now(TZ)
    if os.getenv("TG_TOKEN") and not os.getenv("DRY_RUN"):
        try:
            handle_updates(st)
        except Exception as e:
            print("getUpdates:", e, file=sys.stderr)
    key = due_slot(now, st)
    if key:
        send_reminder(now, st, key)
    save_state(st)

    end = time.monotonic() + listen
    while listen and os.getenv("TG_TOKEN") and time.monotonic() < end - 5:
        wait = int(min(50, end - time.monotonic() - 5))
        try:
            if handle_updates(st, wait=max(wait, 1)):
                save_state(st)
        except Exception as e:
            print("getUpdates:", e, file=sys.stderr)
            time.sleep(10)
    return 0


def main():
    args = sys.argv[1:]
    if "--demo" in args:
        print(TEXT, f"\n[кнопка: {BUTTON}]")
        return 0
    listen = int(args[args.index("--listen") + 1]) if "--listen" in args else 0
    return run(listen)


if __name__ == "__main__":
    sys.exit(main())
