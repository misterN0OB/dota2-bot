import re
import time
import logging
import requests
from urllib.parse import quote

logger = logging.getLogger(__name__)

STEAM_PRICE_URL = "https://steamcommunity.com/market/priceoverview/"
STEAM_SEARCH_URL = "https://steamcommunity.com/market/search/render/"
DOTA2_APPID = 570

CURRENCIES = {
    "RUB": {"code": 5,  "symbol": "руб.", "name": "Рубли"},
    "USD": {"code": 1,  "symbol": "$",    "name": "Доллары"},
    "EUR": {"code": 3,  "symbol": "€",    "name": "Евро"},
    "UAH": {"code": 18, "symbol": "₴",    "name": "Гривны"},
    "KZT": {"code": 37, "symbol": "₸",    "name": "Тенге"},
}

_price_cache: dict = {}  # {(item_name, currency): (price_data, timestamp)}
CACHE_TTL = 1800  # 30 минут

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def parse_price_value(price_str: str) -> float:
    """
    Парсит цену из строки Steam Market.
    Примеры: "2 993,73 руб." -> 2993.73
             "$12.34" -> 12.34
             "1.234,56 руб." -> 1234.56
    """
    if not price_str:
        return 0.0
    # Оставляем только цифры, запятые и точки; убираем точку из "руб."
    cleaned = re.sub(r"[^\d,.]", "", price_str).strip(".,")
    if not cleaned:
        return 0.0
    if "," in cleaned and "." in cleaned:
        # Последний разделитель — десятичный
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def get_item_price(item_name: str, currency: str = "RUB", retries: int = 3, retry_delay: float = 2.0, use_cache: bool = True) -> dict | None:
    """
    Возвращает словарь с ценами или None если предмет не найден / ошибка API.
    retries=3, retry_delay=2.0 — для пользовательских запросов (ждём и повторяем)
    retries=1, retry_delay=0   — для фоновых запросов (не ждём)
    """
    cache_key = (item_name, currency)
    if use_cache and cache_key in _price_cache:
        cached_data, cached_time = _price_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL:
            logger.debug("Cache hit for '%s' (%s)", item_name, currency)
            return cached_data

    cur = CURRENCIES.get(currency, CURRENCIES["RUB"])
    params = {
        "appid": DOTA2_APPID,
        "currency": cur["code"],
        "market_hash_name": item_name,
    }
    for attempt in range(retries):
        try:
            if attempt > 0:
                time.sleep(retry_delay)
            resp = requests.get(
                STEAM_PRICE_URL, params=params, headers=HEADERS, timeout=10
            )
            if resp.status_code == 429:
                logger.warning("Steam rate limit for '%s', attempt %d/%d", item_name, attempt + 1, retries)
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                return None
            lowest = parse_price_value(data.get("lowest_price", ""))
            median = parse_price_value(data.get("median_price", ""))
            volume_raw = data.get("volume", "0").replace(",", "").replace(" ", "")
            result = {
                "lowest": lowest,
                "median": median,
                "volume": int(volume_raw) if volume_raw.isdigit() else 0,
                "symbol": cur["symbol"],
                "currency": currency,
                "item_name": item_name,
            }
            _price_cache[cache_key] = (result, time.time())
            return result
        except Exception as e:
            logger.warning("get_item_price error for '%s' attempt %d: %s", item_name, attempt + 1, e)
    return None


def search_items(query: str, count: int = 5) -> list[dict]:
    """
    Ищет предметы по частичному названию.
    Возвращает список dict с ключами: name, icon.

    Важно: с датацентровых IP (Oracle Cloud) currency в search/render игнорируется.
    Поэтому используем только названия из результатов, не цены.
    """
    params = {
        "appid": DOTA2_APPID,
        "query": query,
        "count": count,
        "search_descriptions": 0,
        "norender": 1,
        "currency": 5,  # запрашиваем RUB, но может вернуться USD — не важно
    }
    try:
        resp = requests.get(
            STEAM_SEARCH_URL, params=params, headers=HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        items = []
        for item in results:
            name = item.get("hash_name") or item.get("name")
            icon = item.get("asset_description", {}).get("icon_url", "")
            if name:
                items.append({"name": name, "icon": icon})
        return items
    except Exception as e:
        logger.warning("search_items error for '%s': %s", query, e)
        return []


def format_price_message(price_data: dict) -> str:
    """Форматирует ответ с ценой для Telegram."""
    sym = price_data["symbol"]
    name = price_data["item_name"]
    lowest = price_data["lowest"]
    median = price_data["median"]
    volume = price_data["volume"]

    lines = [
        f"🎮 <b>{name}</b>",
        "",
        f"💰 Минимальная цена: <b>{lowest:,.2f} {sym}</b>",
        f"📊 Медианная цена: <b>{median:,.2f} {sym}</b>",
    ]
    if volume:
        lines.append(f"📦 Продано за 24ч: <b>{volume}</b>")

    market_url = (
        "https://steamcommunity.com/market/listings/570/"
        + quote(name, safe="")
    )
    lines += ["", f'<a href="{market_url}">📎 Открыть на Steam Market</a>']
    return "\n".join(lines)


# ── Фоновая проверка вотчлиста ─────────────────────────────────────────────────

NOTIFY_COOLDOWN_HOURS = 24  # не слать уведомление чаще раза в 24 часа


async def check_watchlist(context):
    """
    Вызывается APScheduler каждые 30 минут.
    Проверяет все записи вотчлиста и отправляет уведомление:
    - если цена упала до порога или ниже (threshold)
    - если цена выросла до порога или выше (threshold_high)
    Повторное уведомление — не чаще раза в 24 часа.
    """
    from datetime import datetime, timedelta
    from database import get_all_watchlist_items, get_user_settings, update_watchlist_notified

    items = get_all_watchlist_items()

    for entry in items:
        user_id = entry["user_id"]
        item_name = entry["item_name"]
        threshold_low = entry.get("threshold") or 0
        threshold_high = entry.get("threshold_high") or 0

        # Пропускаем если оба порога не заданы
        if threshold_low <= 0 and threshold_high <= 0:
            continue

        settings = get_user_settings(user_id)
        currency = settings.get("currency", "RUB")
        price_data = get_item_price(item_name, currency)
        if not price_data:
            continue

        lowest = price_data["lowest"]
        sym = price_data["symbol"]
        now = datetime.utcnow()

        # ── Уведомление о снижении цены ──────────────────────────────────────
        if threshold_low > 0 and lowest <= threshold_low:
            last_notified = entry.get("last_notified")
            can_notify = True
            if last_notified:
                try:
                    if now - datetime.fromisoformat(last_notified) < timedelta(hours=NOTIFY_COOLDOWN_HOURS):
                        can_notify = False
                except ValueError:
                    pass
            if can_notify:
                text = (
                    f"📉 <b>Цена снизилась!</b>\n\n"
                    f"🎮 {item_name}\n"
                    f"💰 Цена: <b>{lowest:,.2f} {sym}</b>\n"
                    f"🎯 Твой порог снижения: {threshold_low:,.2f} {sym}"
                )
                try:
                    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                    update_watchlist_notified(entry["id"], high=False)
                    logger.info("Drop alert sent: user=%s item=%s price=%s", user_id, item_name, lowest)
                except Exception as e:
                    logger.warning("Failed to notify user %s: %s", user_id, e)

        # ── Уведомление о росте цены ─────────────────────────────────────────
        if threshold_high > 0 and lowest >= threshold_high:
            last_notified_high = entry.get("last_notified_high")
            can_notify = True
            if last_notified_high:
                try:
                    if now - datetime.fromisoformat(last_notified_high) < timedelta(hours=NOTIFY_COOLDOWN_HOURS):
                        can_notify = False
                except ValueError:
                    pass
            if can_notify:
                text = (
                    f"📈 <b>Цена выросла!</b>\n\n"
                    f"🎮 {item_name}\n"
                    f"💰 Цена: <b>{lowest:,.2f} {sym}</b>\n"
                    f"🎯 Твой порог роста: {threshold_high:,.2f} {sym}"
                )
                try:
                    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                    update_watchlist_notified(entry["id"], high=True)
                    logger.info("Rise alert sent: user=%s item=%s price=%s", user_id, item_name, lowest)
                except Exception as e:
                    logger.warning("Failed to notify user %s: %s", user_id, e)
