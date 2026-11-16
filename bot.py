#!/usr/bin/env python3
"""Ранкова зведення для Миколаєва в Telegram: погода, вітер, тиск, магнітні бурі.

Дані: Open-Meteo (погода, якість повітря) і NOAA SWPC (індекс Kp).
Лише стандартна бібліотека Python 3.9+, нічого встановлювати не треба.

Змінні середовища:
  TG_TOKEN     токен бота від @BotFather
  TG_CHAT_ID   id чату (можна кілька через кому: батьки, ви)
  PARENT_GPV   черга відключень світла (за замовчуванням 2.1), також PARENT_GAV, PARENT_SGAV
  NO_LIGHT=1   не додавати графік відключень світла
  FORCE=1      надіслати одразу, не чекаючи 07:00 за Києвом

Запускати можна хоч щохвилини: зведення піде один раз на день, з 07:00 до 12:00,
дата надсилання зберігається у weather_sent.txt.
  DRY_RUN=1    лише показати текст, нічого не надсилати

Перегляд без токена і без мережі:  python bot.py --demo
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CITY = "Миколаїв"
LAT, LON = 46.975, 31.9946
TZ = ZoneInfo("Europe/Kyiv")
SEND_HOUR = 7   # з цієї години (за Києвом) зведення можна надсилати
LAST_HOUR = 12  # якщо до 12:00 так і не вдалося, того дня вже не надсилаємо
SENT_FILE = os.getenv("SENT_FILE") or "weather_sent.txt"  # дата останнього надсилання
HPA_TO_MMHG = 0.750062

WEEKDAYS = ["Понеділок", "Вівторок", "Середа", "Четвер", "П’ятниця", "Субота", "Неділя"]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня",
          "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
COMPASS = ["північний", "північно-східний", "східний", "південно-східний",
           "південний", "південно-західний", "західний", "північно-західний"]

WEATHER = {
    0: ("☀️", "ясно"), 1: ("🌤", "переважно ясно"), 2: ("⛅", "мінлива хмарність"),
    3: ("☁️", "хмарно"), 45: ("🌫", "туман"), 48: ("🌫", "паморозний туман"),
    51: ("🌦", "легка мряка"), 53: ("🌦", "мряка"), 55: ("🌧", "густа мряка"),
    56: ("🧊", "крижана мряка"), 57: ("🧊", "сильна крижана мряка"),
    61: ("🌧", "невеликий дощ"), 63: ("🌧", "дощ"), 65: ("🌧", "сильний дощ"),
    66: ("🧊", "крижаний дощ"), 67: ("🧊", "сильний крижаний дощ"),
    71: ("🌨", "невеликий сніг"), 73: ("❄️", "сніг"), 75: ("❄️", "сильний сніг"),
    77: ("🌨", "снігові зерна"), 80: ("🌦", "короткочасні зливи"),
    81: ("🌧", "зливи"), 82: ("⛈", "сильні зливи"),
    85: ("🌨", "снігопад"), 86: ("❄️", "сильний снігопад"),
    95: ("⛈", "гроза"), 96: ("⛈", "гроза з градом"), 99: ("⛈", "сильна гроза з градом"),
}


# ---------- завантаження даних ----------

def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "family-weather-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:  # мережа може смикнутись, пробуємо ще
            last = e
            time.sleep(2 * (i + 1))
    raise last


def fetch_weather():
    q = {
        "latitude": LAT, "longitude": LON,
        "current": "temperature_2m,apparent_temperature,pressure_msl,wind_speed_10m,"
                   "wind_gusts_10m,wind_direction_10m,weather_code",
        "hourly": "pressure_msl",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,wind_gusts_10m_max,uv_index_max,sunrise,sunset",
        "timezone": "Europe/Kyiv", "wind_speed_unit": "ms",
        "past_days": 1, "forecast_days": 2,
    }
    return get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q))


def fetch_air():
    q = {"latitude": LAT, "longitude": LON, "current": "european_aqi", "timezone": "Europe/Kyiv"}
    return get_json("https://air-quality-api.open-meteo.com/v1/air-quality?" + urllib.parse.urlencode(q))


def fetch_kp():
    data = get_json("https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json")
    rows = []
    for row in data:
        if isinstance(row, dict):
            t, kp = row.get("time_tag"), row.get("kp")
        else:
            t, kp = row[0], row[1]
        try:
            ts = datetime.fromisoformat(str(t).replace("Z", "")).replace(tzinfo=timezone.utc)
            rows.append((ts, float(kp)))
        except (ValueError, TypeError):
            continue  # рядок із заголовками
    rows.sort()
    return rows


def fetch_light():
    """Графік відключень світла з off.energy.mk.ua (той самий код, що й outage_bot.py)."""
    import outage_bot
    return outage_bot.fetch_all()


def light_info(data, now):
    """Повертає (рядки для повідомлення, чи є сьогодні відключення)."""
    import outage_bot as ob
    watched = ob.watched_queues(data["queues"])
    if not watched:
        return [], False
    days = ob.build_days(data["active"], data["timeseries"], watched)
    today = days.get(now.strftime("%Y-%m-%d"))
    lines, has_off = [], False
    for w in watched:
        if w["group"] != "ГПВ":
            if w["enabled"]:
                lines.append(f"🚨 💡 Зараз діє відключення · {w['label']}")
                has_off = True
            continue
        head = f"💡 <b>Світло</b> (черга {w['label'].split()[-1]})"
        if today is None:
            lines.append(f"{head}: графік на сьогодні ще не опубліковано")
            continue
        ints = today.get(w["label"], [])
        if not ints:
            lines.append(f"🟢 {head}: відключень не заплановано")
            continue
        lines.append(f"{head}:")
        for typ in ("SURE_OFF", "OFF", "PROBABLY_OFF"):
            part = [(s, e) for s, e, t in ints if t == typ]
            if part:
                icon, name = ob.LABELS[typ]
                lines.append(f"      {icon} {name}: "
                             + ", ".join(f"{ob.fmt_time(s)}–{ob.fmt_time(e)}" for s, e in part))
        total = sum(e - s for s, e, t in ints if t in ob.OFF_TYPES)
        if total:
            has_off = True
            lines.append(f"      ⏱ Без світла приблизно {ob.fmt_dur(total)}")
        if w["enabled"]:
            lines.append("      🔴 Відключення діє просто зараз")
            has_off = True
    return lines, has_off


# ---------- допоміжне форматування ----------

def temp(x):
    v = round(x)
    if v > 0:
        return f"+{v}°"
    if v < 0:
        return f"−{abs(v)}°"
    return "0°"


def mmhg(hpa):
    return round(hpa * HPA_TO_MMHG)


def level(value, limits):
    """Повертає кольоровий кружечок: limits = [межа_зелений, межа_жовтий, межа_помаранчевий]."""
    icons = ["🟢", "🟡", "🟠", "🔴"]
    for icon, lim in zip(icons, limits):
        if value < lim:
            return icon
    return icons[3]


def kp_info(kp):
    if kp < 4:
        return "🟢", "спокійне"
    if kp < 5:
        return "🟡", "трохи збурене"
    if kp < 6:
        return "🟠", "слабка магнітна буря (G1)"
    if kp < 7:
        return "🟠", "помірна магнітна буря (G2)"
    if kp < 8:
        return "🔴", "сильна магнітна буря (G3)"
    return "🔴", "дуже сильна магнітна буря (G4+)"


def aqi_info(aqi):
    if aqi < 20:
        return "🟢", "відмінне"
    if aqi < 40:
        return "🟢", "добре"
    if aqi < 60:
        return "🟡", "помірне"
    if aqi < 80:
        return "🟠", "погане"
    if aqi < 100:
        return "🔴", "дуже погане"
    return "🔴", "небезпечне"


def kp_summary(rows, now_utc):
    if not rows:
        return None
    past = [r for r in rows if r[0] <= now_utc]
    cur = past[-1][1] if past else rows[0][1]
    soon = [r[1] for r in rows if now_utc <= r[0] <= now_utc + timedelta(hours=24)]
    peak = max(soon + [cur])
    return cur, peak


# ---------- побудова повідомлення ----------

def build_message(w, air, kp_rows, now, light=None):
    cur, d, h = w["current"], w["daily"], w["hourly"]
    i = d["time"].index(now.strftime("%Y-%m-%d"))
    tips = []

    icon, desc = WEATHER.get(cur["weather_code"], ("🌡", "змінна погода"))
    t_now, t_feel = cur["temperature_2m"], cur["apparent_temperature"]
    t_max, t_min = d["temperature_2m_max"][i], d["temperature_2m_min"][i]
    rain_p = d["precipitation_probability_max"][i] or 0
    rain_mm = d["precipitation_sum"][i] or 0

    # вітер
    wind, gust = cur["wind_speed_10m"], cur["wind_gusts_10m"]
    wind_icon = level(gust, [8, 12, 17])
    wind_dir = COMPASS[round(cur["wind_direction_10m"] / 45) % 8]

    # тиск і його зміна за добу
    p_now = cur["pressure_msl"]
    key = cur["time"][:13]
    idx = next((k for k, tm in enumerate(h["time"]) if tm.startswith(key)), None)
    p_icon, p_line = "🟢", f"{mmhg(p_now)} мм рт. ст."
    dp = None
    if idx is not None and idx >= 24:
        dp = (p_now - h["pressure_msl"][idx - 24]) * HPA_TO_MMHG
        arrow = "↓" if dp <= -1 else ("↑" if dp >= 1 else "→")
        p_icon = level(abs(dp), [3, 6, 9])
        p_line += f" ({arrow} {abs(round(dp))} за добу)"

    lines = []
    lines.append("🌅 <b>Доброго ранку!</b>")
    lines.append(f"📅 {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} · 📍 {CITY}")
    lines.append("┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
    lines.append(f"{icon} <b>Зараз {temp(t_now)}</b>, {desc}")
    lines.append(f"🌡 Відчувається як {temp(t_feel)} · за день від {temp(t_min)} до {temp(t_max)}")
    rain_txt = f"{round(rain_p)}%" + (f" (≈{rain_mm:.1f} мм)" if rain_mm >= 0.1 else "")
    lines.append(f"☔ Імовірність опадів: {rain_txt}")
    lines.append(f"{wind_icon} 💨 Вітер {wind:.0f} м/с, {wind_dir}, пориви до {gust:.0f} м/с")
    lines.append(f"{p_icon} 🧭 Тиск: {p_line}")

    kp = kp_summary(kp_rows, datetime.now(timezone.utc)) if kp_rows else None
    if kp:
        kcur, kpeak = kp
        ic, txt = kp_info(kcur)
        line = f"{ic} 🧲 Магнітне поле: {txt} (Kp {kcur:.0f})"
        if kpeak >= kcur + 1 or kpeak >= 5:
            pic, ptxt = kp_info(kpeak)
            line += f"\n      ↗ найближчі 24 год: до Kp {kpeak:.0f} — {ptxt}"
        lines.append(line)
        if kpeak >= 5:
            tips.append("Магнітна буря: метеочутливим можливе погіршення самопочуття. "
                        "Більше води, спокійний темп, не забудьте про ліки.")
    else:
        lines.append("🧲 Магнітне поле: дані зараз недоступні")

    aqi = (air or {}).get("current", {}).get("european_aqi")
    if aqi is not None:
        ic, txt = aqi_info(aqi)
        lines.append(f"{ic} 🌫 Повітря: {txt}")
        if aqi >= 60:
            tips.append("Повітря забруднене: прогулянку краще скоротити.")

    uv = d["uv_index_max"][i]
    if uv is not None:
        lines.append(f"{level(uv, [3, 6, 8])} ☀️ УФ-індекс: {uv:.0f}")
        if uv >= 6:
            tips.append("Високий УФ: капелюх, окуляри, крем.")

    sunrise, sunset = d["sunrise"][i][11:16], d["sunset"][i][11:16]
    lines.append(f"🌇 Схід {sunrise} · захід {sunset}")

    if light is not None:
        try:
            l_lines, l_off = light_info(light, now)
        except Exception as e:  # змінився формат сайту: погоду все одно надсилаємо
            print("Графік світла не розібрано:", e, file=sys.stderr)
            l_lines, l_off = ["💡 Графік відключень світла зараз недоступний"], False
        if l_lines:
            lines.append("┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
            lines.extend(l_lines)
        if l_off:
            tips.append("Будуть відключення світла: зарядіть телефон і павербанк заздалегідь 🔋")
    elif not os.getenv("NO_LIGHT"):
        lines.append("💡 Графік відключень світла зараз недоступний")

    # поради
    if rain_p >= 50 or rain_mm >= 1:
        tips.append("Візьміть парасольку ☂️")
    if t_max <= 0 or (t_min <= 0 and rain_p >= 40):
        tips.append("Слизько, можлива ожеледиця. Обережно на сходах і тротуарах.")
    if t_max >= 30:
        tips.append("Спека: пийте воду, уникайте вулиці з 12:00 до 17:00.")
    if t_min <= -10:
        tips.append("Сильний мороз: тепло одягайтеся, шапка і рукавиці обов’язкові.")
    if gust >= 15 or d["wind_gusts_10m_max"][i] >= 17:
        tips.append("Сильні пориви вітру: обережно біля дерев і вивісок.")
    if dp is not None and abs(dp) >= 6:
        tips.append("Різкий перепад тиску: не перевантажуйтесь, пийте воду, прислухайтесь до себе.")

    lines.append("┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
    if tips:
        lines.append("💡 <b>Порада дня</b>")
        lines.extend(f"• {t}" for t in tips[:4])
    else:
        lines.append("💡 Сьогодні все спокійно, гарна нагода для прогулянки 🚶")

    if i + 1 < len(d["time"]):
        j = i + 1
        ticon, tdesc = WEATHER.get(d["weather_code"][j], ("🌡", "змінна погода"))
        lines.append("")
        lines.append(f"🔮 <b>Завтра:</b> {ticon} {tdesc}, від {temp(d['temperature_2m_min'][j])} "
                     f"до {temp(d['temperature_2m_max'][j])}, опади {round(d['precipitation_probability_max'][j] or 0)}%")

    lines.append("")
    lines.append("💙💛 Гарного дня і міцного здоров’я!")
    return "\n".join(lines)


# ---------- надсилання ----------

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# ---------- демо-дані (для перегляду без мережі) ----------

def demo_data(now):
    day0 = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    times = [(day0 + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M") for k in range(72)]
    press = [1016 - 0.3 * k for k in range(72)]
    idx = 24 + now.hour
    dates = [(day0 + timedelta(days=k)).strftime("%Y-%m-%d") for k in range(3)]
    w = {
        "current": {"time": times[idx], "temperature_2m": 3.4, "apparent_temperature": 0.1,
                    "pressure_msl": press[idx], "wind_speed_10m": 7.2, "wind_gusts_10m": 13.8,
                    "wind_direction_10m": 315, "weather_code": 61},
        "hourly": {"time": times, "pressure_msl": press},
        "daily": {"time": dates, "weather_code": [3, 61, 3],
                  "temperature_2m_max": [6, 6.8, 8], "temperature_2m_min": [2, 2.4, 1],
                  "precipitation_probability_max": [20, 70, 30], "precipitation_sum": [0, 2.1, 0.3],
                  "wind_gusts_10m_max": [9, 14, 10], "uv_index_max": [0.8, 0.9, 1],
                  "sunrise": [d + "T07:05" for d in dates], "sunset": [d + "T16:48" for d in dates]},
    }
    air = {"current": {"european_aqi": 34}}
    now_utc = datetime.now(timezone.utc)
    pattern = [2, 2.33, 3, 3.67, 5.33, 4.33, 3, 2.67]
    kp = [(now_utc + timedelta(hours=3 * k), pattern[k % 8]) for k in range(-4, 17)]
    return w, air, kp


# ---------- запуск ----------

def last_sent():
    try:
        with open(SENT_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def main():
    now = datetime.now(TZ)
    demo = "--demo" in sys.argv

    if demo:
        w, air, kp = demo_data(now)
        import outage_bot
        light = outage_bot.demo_data("A")
        for x in light["active"]:  # демо-графік переносимо на сьогодні
            x["from"] = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    else:
        if not os.getenv("FORCE"):
            if not SEND_HOUR <= now.hour < LAST_HOUR:
                return 0
            if last_sent() == now.strftime("%Y-%m-%d"):
                return 0
        w = fetch_weather()
        try:
            air = fetch_air()
        except Exception as e:
            print("Повітря недоступне:", e)
            air = None
        try:
            kp = fetch_kp()
        except Exception as e:
            print("Kp недоступний:", e)
            kp = None
        light = None
        if not os.getenv("NO_LIGHT"):
            try:
                light = fetch_light()
            except Exception as e:
                print("Графік світла недоступний:", e)

    msg = build_message(w, air, kp, now, light)

    if demo or os.getenv("DRY_RUN"):
        print(msg)
        return 0

    token = os.environ["TG_TOKEN"]
    chats = [c.strip() for c in os.environ["TG_CHAT_ID"].split(",") if c.strip()]
    for n, chat in enumerate(chats, 1):
        send_telegram(token, chat, msg)
        print(f"Надіслано отримувачу {n} з {len(chats)}")  # id не друкуємо: логи публічні
    if not os.getenv("FORCE"):
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            f.write(now.strftime("%Y-%m-%d") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
