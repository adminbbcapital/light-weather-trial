#!/usr/bin/env python3
"""Моніторинг відключень світла (АТ «Миколаївобленерго») -> Telegram.

Бере дані з відкритого API сайту off.energy.mk.ua і пише в Telegram, коли:
  * опубліковано або змінено графік для вашої черги;
  * для черги вмикається або завершується відключення (стан на сайті).

Лише стандартна бібліотека Python 3.9+.

Змінні середовища:
  TG_TOKEN          токен бота
  TG_CHAT_ID        chat id отримувачів через кому (батьки, ви)
  TG_ADMIN_CHAT_ID  (необов'язково) куди слати технічні попередження, напр. «сайт недоступний»
  PARENT_GPV        черга ГПВ, за замовчуванням 2.1
  PARENT_GAV        група ГАВ, за замовчуванням 10(Р); кілька через кому, напр. 10,10(Р)
  PARENT_SGAV       (необов'язково) група СГАВ, напр. 2
  STATE_FILE        файл стану, за замовчуванням state.json
  SEND_STATUS=1     додатково надіслати поточний стан (для ручної перевірки)
  DRY_RUN=1         лише показати текст, нічого не надсилати і не зберігати

Режими запуску:
  python outage_bot.py                  одна перевірка (для планувальника / GitHub Actions)
  python outage_bot.py --loop 120       безперервно, перевірка кожні 120 с (для ПК, Raspberry Pi, VPS)
  python outage_bot.py --status         показати поточний стан із сайту (без надсилання)
  python outage_bot.py --list-queues    показати всі черги й групи сайту
  python outage_bot.py --demo           показати вигляд повідомлень на тестових даних
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = "https://off.energy.mk.ua"
TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = os.getenv("STATE_FILE") or "state.json"
GROUP_NAMES = {3: "ГПВ", 1: "ГАВ", 2: "СГАВ"}  # type_id на сайті -> назва режиму

# Позначення станів клітинок розкладу. Якщо значення на сайті трактуються інакше,
# змініть лише цей словник.
LABELS = {
    "SURE_OFF": ("⛔", "Точно без світла"),
    "OFF": ("🔴", "Відключення за графіком"),
    "PROBABLY_OFF": ("🟡", "Можливе відключення"),
}
OFF_TYPES = ("OFF", "SURE_OFF")

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": BASE + "/",
}


# ---------- мережа ----------

def get_json(path, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Не вдалося отримати {path}: {last}")


def fetch_all():
    return {
        "queues": {t: get_json(f"/api/outage-queue/by-type/{t}") for t in (1, 2, 3)},
        "active": get_json("/api/v2/schedule/active"),
        "timeseries": get_json("/api/schedule/time-series"),
    }


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def chat_list(var):
    return [c.strip() for c in (os.getenv(var) or "").split(",") if c.strip()]


def notify(texts, chats):
    token = os.getenv("TG_TOKEN")
    for text in texts:
        if os.getenv("DRY_RUN") or not token:
            print(text, "\n" + "-" * 40)
            continue
        for n, chat in enumerate(chats, 1):
            send_telegram(token, chat, text)
            print(f"Надіслано отримувачу {n} з {len(chats)}")  # id не друкуємо: логи публічні


# ---------- розбір даних ----------

def norm(name):
    """Латинські P/p у (Р) приводимо до кириличних, щоб не було плутанини."""
    return name.strip().replace("P", "Р").replace("p", "р")


def parse_names(value, default=""):
    value = value or default
    return [norm(x) for x in value.split(",") if x.strip()]


def watched_queues(queues):
    cfg = {
        3: parse_names(os.getenv("PARENT_GPV"), "2.1"),
        1: parse_names(os.getenv("PARENT_GAV"), "10(Р)"),
        2: parse_names(os.getenv("PARENT_SGAV")),
    }
    out = []
    for t, names in cfg.items():
        for n in names:
            q = next((q for q in queues[t] if norm(q["name"]) == n and not q.get("deleted")), None)
            if q is None:
                print(f"Увага: черги {GROUP_NAMES[t]} {n} на сайті немає", file=sys.stderr)
                continue
            out.append({"id": q["id"], "label": f"{GROUP_NAMES[t]} {n}",
                        "group": GROUP_NAMES[t], "enabled": int(q.get("enabled") or 0)})
    return out


def mins(s):
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def build_ts(timeseries):
    out = {}
    for t in timeseries:
        s, e = mins(t["start"]), mins(t["end"])
        if e <= s:
            e = 1440
        out[t["id"]] = (s, e)
    return out


def merge(slots, ts):
    items = sorted((ts[i][0], ts[i][1], t) for i, t in slots.items() if i in ts)
    out = []
    for s, e, t in items:
        if out and out[-1][2] == t and out[-1][1] == s:
            out[-1][1] = e
        else:
            out.append([s, e, t])
    return out


def build_days(active, timeseries, watched):
    ts = build_ts(timeseries)
    days = {}
    for sch in sorted(active, key=lambda s: s["id"]):
        start = datetime.fromisoformat(sch["from"].replace("Z", "+00:00")).astimezone(TZ)
        by_q = {}
        for x in sch["series"]:
            by_q.setdefault(x["outage_queue_id"], {})[x["time_series_id"]] = x["type"]
        days[start.strftime("%Y-%m-%d")] = {
            w["label"]: merge(by_q.get(w["id"], {}), ts) for w in watched
        }
    return days


# ---------- форматування ----------

def fmt_time(m):
    return "24:00" if m >= 1440 else f"{m // 60:02d}:{m % 60:02d}"


def fmt_dur(m):
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h} год {mm} хв"
    return f"{h} год" if h else f"{mm} хв"


def off_blocks(ints):
    """Зливає сусідні інтервали відключень (OFF і SURE_OFF) в один блок."""
    blocks = []
    for s, e, t in ints:
        if t not in OFF_TYPES:
            continue
        if blocks and blocks[-1][1] == s:
            blocks[-1][1] = e
        else:
            blocks.append([s, e])
    return blocks


def blocks_text(blocks):
    return ", ".join(f"{fmt_time(s)}–{fmt_time(e)}" for s, e in blocks)


def day_block(date_str, label, ints):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    lines = [f"📅 <b>{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}</b> · черга {label}"]
    if not ints:
        lines.append("🟢 Відключень не заплановано")
        return "\n".join(lines)
    for typ in ("SURE_OFF", "OFF", "PROBABLY_OFF"):
        part = [(s, e) for s, e, t in ints if t == typ]
        if part:
            icon, name = LABELS[typ]
            lines.append(f"{icon} {name}: " + ", ".join(f"{fmt_time(s)}–{fmt_time(e)}" for s, e in part))
    total = sum(e - s for s, e, t in ints if t in OFF_TYPES)
    if total:
        lines.append(f"⏱ Без світла приблизно {fmt_dur(total)}")
    return "\n".join(lines)


def current_end(ints, now):
    """До котрої години триває відключення, яке йде зараз за розкладом."""
    nm = now.hour * 60 + now.minute
    for s, e in off_blocks(ints):
        if s <= nm < e:
            return e
    return None


# ---------- логіка змін ----------

def status_message(watched, days, today, title):
    parts = [title]
    for w in watched:
        mark = "🔴 відключення активне" if w["enabled"] else "🟢 відключення немає"
        parts.append(f"{w['label']}: {mark} (за даними сайту)")
    parts = ["\n".join(parts)]
    for date in sorted(d for d in days if d >= today):
        for w in watched:
            ints = days[date].get(w["label"], [])
            if not ints and w["group"] != "ГПВ":
                continue  # ГАВ/СГАВ не мають розкладу за годинами, лише стан «увімкнено»
            parts.append(day_block(date, w["label"], ints))
    return "\n\n".join(parts)


def process(data, state, now):
    """Повертає (список повідомлень, новий стан)."""
    watched = watched_queues(data["queues"])
    if not watched:
        raise RuntimeError("Жодної черги зі списку PARENT_* не знайдено на сайті")
    days = {d: v for d, v in build_days(data["active"], data["timeseries"], watched).items()}
    enabled = {w["label"]: w["enabled"] for w in watched}
    today = now.strftime("%Y-%m-%d")
    days = {d: v for d, v in days.items() if d >= today}
    msgs = []

    first = not state or "days" not in state
    if first:
        msgs.append(status_message(
            watched, days, today,
            "✅ <b>Моніторинг відключень світла увімкнено</b>\n"
            "Напишу одразу, коли зміниться графік або вмикатиметься відключення."))
    else:
        old_days = state.get("days", {})
        # 1) зміни графіка
        for date in sorted(days):
            for w in watched:
                label = w["label"]
                new = days[date].get(label, [])
                old = old_days.get(date, {}).get(label)
                if old == new or (old is None and not new and w["group"] != "ГПВ"):
                    continue
                if old is None:
                    head = "📅 <b>Опубліковано графік</b>"
                    prev = ""
                else:
                    head = "🔄 <b>Графік змінено</b>"
                    ob = off_blocks(old)
                    prev = "\n↩️ Було: " + (blocks_text(ob) if ob else "відключень не було")
                msgs.append(head + "\n" + day_block(date, label, new) + prev)
        # 2) увімкнення / завершення відключення
        old_enabled = state.get("enabled", {})
        for w in watched:
            label, was, now_on = w["label"], old_enabled.get(w["label"]), w["enabled"]
            if was is None or was == now_on:
                continue
            if now_on:
                icon = "🚨" if w["group"] != "ГПВ" else "🔴"
                text = f"{icon} <b>Увімкнено відключення</b> · черга {label}"
                end = current_end(days.get(today, {}).get(label, []), now)
                if end:
                    text += f"\n⏳ Орієнтовно до {fmt_time(end)}"
                msgs.append(text)
            else:
                msgs.append(f"🟢 <b>Відключення завершено</b> · черга {label}\n"
                            "Світло має з’явитися найближчим часом.")

    new_state = {"init": True, "days": days, "enabled": enabled}
    return msgs, new_state


# ---------- стан ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(st):
    new = json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            if f.read() == new:
                return
    except FileNotFoundError:
        pass
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(new)


# ---------- запуск ----------

def run_once():
    now = datetime.now(TZ)
    state = load_state()
    chats = chat_list("TG_CHAT_ID")
    admin = chat_list("TG_ADMIN_CHAT_ID")
    dry = bool(os.getenv("DRY_RUN"))

    try:
        data = fetch_all()
    except Exception as e:
        print("ПОМИЛКА:", e, file=sys.stderr)
        print("Якщо це 403 або тайм-аут із GitHub Actions, сайт може не пускати закордонні IP. "
              "Запустіть бота на комп’ютері в Україні (див. README-outage.md).", file=sys.stderr)
        if os.getenv("SEND_STATUS"):
            return 1  # ручний тест: показуємо помилку червоним
        if "down_since" not in state:
            state["down_since"] = now.isoformat()
        elif not state.get("down_notified"):
            since = datetime.fromisoformat(state["down_since"])
            if now - since >= timedelta(minutes=30):
                if admin:
                    notify(["⚠️ Не можу отримати дані з сайту відключень вже понад 30 хвилин. "
                            "Сайт недоступний або заблокував запити."], admin)
                state["down_notified"] = True
        if not dry:
            save_state(state)
        return 0

    msgs, new_state = process(data, state, now)

    if os.getenv("SEND_STATUS") and not any(m.startswith("✅") for m in msgs):
        watched = watched_queues(data["queues"])
        days = build_days(data["active"], data["timeseries"], watched)
        today = now.strftime("%Y-%m-%d")
        msgs.append(status_message(watched, {d: v for d, v in days.items() if d >= today},
                                   today, "ℹ️ <b>Поточний стан</b>"))

    notify(msgs, chats)

    if state.get("down_notified") and admin:
        notify(["✅ Зв’язок із сайтом відключень відновлено."], admin)
    if not dry:
        save_state(new_state)
    return 0


def demo_data(version):
    def sid(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 2 + int(m) // 30 + 1

    def slots(a, b, typ):
        return [{"time_series_id": i, "outage_queue_id": 16, "type": typ}
                for i in range(sid(a), sid(b))]

    ts = [{"id": i + 1, "start": f"{(i * 30) // 60:02d}:{(i * 30) % 60:02d}:00",
           "end": f"{((i + 1) * 30) // 60 % 24:02d}:{((i + 1) * 30) % 60:02d}:00"} for i in range(48)]
    evening = ("18:00", "20:30") if version == "A" else ("19:00", "21:30")
    series = (slots("00:00", "00:30", "PROBABLY_OFF") + slots("00:30", "03:00", "SURE_OFF")
              + slots("10:30", "11:00", "PROBABLY_OFF") + slots("11:00", "13:30", "OFF")
              + slots("13:30", "14:00", "PROBABLY_OFF") + slots(*evening, "OFF"))
    active = [{"id": 1, "from": "2026-11-15T22:00:00.000000Z",
               "to": "2026-11-16T21:59:00.000000Z", "series": series}]
    enabled = 1 if version == "B" else 0
    queues = {
        3: [{"id": 16, "name": "2.1", "enabled": enabled, "deleted": 0}],
        1: [{"id": 7, "name": "10(P)", "enabled": 1 if version == "B" else 0, "deleted": 0}],
        2: [{"id": 12, "name": "2", "enabled": 0, "deleted": 0}],
    }
    return {"queues": queues, "active": active, "timeseries": ts}


def demo():
    os.environ["PARENT_SGAV"] = "2"
    now = datetime(2026, 11, 16, 12, 10, tzinfo=TZ)
    steps = [("1) Перший запуск", "A", None),
             ("2) Графік змінено, відключення вмикається", "B", None),
             ("3) Відключення завершилось", "A", None)]
    state = None
    for title, ver, _ in steps:
        msgs, state = process(demo_data(ver), state, now)
        print(f"\n===== {title} =====")
        for m in msgs:
            print(m, "\n" + "-" * 40)


def main():
    args = sys.argv[1:]
    if "--demo" in args:
        return demo()
    if "--list-queues" in args:
        data = fetch_all()
        for t in (3, 1, 2):
            names = ", ".join(q["name"] for q in data["queues"][t] if not q.get("deleted"))
            print(f"{GROUP_NAMES[t]}: {names}")
        return 0
    if "--status" in args:
        data = fetch_all()
        now = datetime.now(TZ)
        msgs, _ = process(data, None, now)
        print("\n".join(msgs))
        return 0
    if "--loop" in args:
        interval = int(args[args.index("--loop") + 1])
        while True:
            try:
                run_once()
            except Exception as e:
                print("Помилка циклу:", e, file=sys.stderr)
            time.sleep(interval)
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
