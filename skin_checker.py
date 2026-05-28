import re
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


def get_item_price(item_name: str, currency: str = "RUB") -> dict | None:
    """
    Возвращает словарь с ценами или None если предмет не найден / ошибка API.
    """
    cur = CURRENCIES.get(currency, CURRENCIES["RUB"])
    params = {
        "appid": DOTA2_APPID,
        "currency": cur["code"],
        "market_hash_name": item_name,
    }
    try:
        resp = requests.get(
            STEAM_PRICE_URL, params=params, headers=HEADERS, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            return None

        lowest = parse_price_value(data.get("lowest_price", ""))
        median = parse_price_value(data.get("median_price", ""))
        volume_raw = data.get("volume", "0").replace(",", "").replace(" ", "")

        return {
            "lowest": lowest,
            "median": median,
            "volume": int(volume_raw) if volume_raw.isdigit() else 0,
            "symbol": cur["symbol"],
            "currency": currency,
            "item_name": item_name,
        }
    except Exception as e:
        logger.warning("get_item_price error for '%s': %s", item_name, e)
        return None


def search_items(query: str, count: int = 5) -> list[str]:
    """
    Ищет предметы по частичному названию.
    Возвращает список точных market_hash_name.

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
        names = []
        for item in results:
            name = item.get("hash_name") or item.get("name")
            if name:
                names.append(name)
        return names
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

async def check_watchlist(context):
    """
    Вызывается APScheduler каждые 30 минут.
    Проверяет все записи вотчлиста и отправляет уведомление
    если цена упала до порога или ниже.
    """
    from database import get_all_watchlist_items, get_user_settings

    items = get_all_watchlist_items()
    notified: dict[tuple, bool] = {}  # (user_id, item_name) → уже уведомили

    for entry in items:
        user_id = entry["user_id"]
        item_name = entry["item_name"]
        threshold = entry["threshold"]
        key = (user_id, item_name)

        if key in notified:
            continue

        settings = get_user_settings(user_id)
        currency = settings.get("currency", "RUB")

        price_data = get_item_price(item_name, currency)
        if not price_data:
            continue

        lowest = price_data["lowest"]
        if threshold > 0 and lowest <= threshold:
            sym = price_data["symbol"]
            text = (
                f"🔔 <b>Уведомление вотчлиста</b>\n\n"
                f"🎮 {item_name}\n"
                f"💰 Цена упала до <b>{lowest:,.2f} {sym}</b>\n"
                f"🎯 Твой порог: {threshold:,.2f} {sym}"
            )
            try:
                await context.bot.send_message(
                    chat_id=user_id, text=text, parse_mode="HTML"
                )
                notified[key] = True
                logger.info("Watchlist alert sent: user=%s item=%s", user_id, item_name)
            except Exception as e:
                logger.warning("Failed to notify user %s: %s", user_id, e)
