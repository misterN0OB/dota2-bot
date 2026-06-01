"""
Dota 2 Price Bot — FastAPI backend для Telegram Mini App
Запуск: uvicorn api:app --host 0.0.0.0 --port 8000
"""

import hashlib
import hmac
import json
import time
import logging
from urllib.parse import unquote, parse_qs

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import BOT_TOKEN
from database import (
    get_user_settings,
    set_user_currency,
    is_premium,
    set_premium,
    add_to_watchlist,
    get_watchlist,
    remove_from_watchlist,
    update_watchlist_threshold,
    add_to_portfolio,
    get_portfolio,
    remove_from_portfolio,
    record_price,
    get_price_history,
    touch_activity,
    get_price_checks_today,
    log_user_price_check,
    get_bonus_watchlist,
)
from skin_checker import get_item_price, search_items, CURRENCIES

# Лимиты для бесплатных пользователей
FREE_WATCHLIST_LIMIT = 5
FREE_PORTFOLIO_LIMIT = 5
FREE_DAILY_PRICE_CHECKS = 20  # бесплатных проверок цены в день

# Популярные предметы Dota 2 (топ по популярности торгов)
TOP_ITEMS = [
    "Dragonclaw Hook",
    "Genuine Dragonclaw Hook",
    "Tempest Helm of the Thundergod",
    "Sylvan Cascade",
    "Inscribed Blades of the Reaper",
]

# Самые дорогие предметы Dota 2
EXPENSIVE_ITEMS = [
    "Genuine Dragonclaw Hook",
    "Dragonclaw Hook",
    "Tempest Helm of the Thundergod",
    "Genuine Resonant Virtue",
    "Inscribed Corrupted Monarch Bow",
]

logger = logging.getLogger(__name__)

app = FastAPI(title="Dota2 Price API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Telegram initData validation ───────────────────────────────────────────────

def validate_init_data(init_data: str) -> dict:
    """
    Проверяет подпись Telegram WebApp initData.
    Возвращает словарь с данными пользователя или бросает HTTPException.
    """
    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
        hash_value = parsed.get("hash", [""])[0]

        # Собираем строку для проверки (все поля кроме hash, отсортированные)
        data_check_parts = []
        for key, values in sorted(parsed.items()):
            if key != "hash":
                data_check_parts.append(f"{key}={values[0]}")
        data_check_string = "\n".join(data_check_parts)

        # Ключ подписи
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, hash_value):
            raise HTTPException(status_code=401, detail="Invalid initData signature")

        # Проверяем свежесть (не старше 24 часов)
        auth_date = int(parsed.get("auth_date", ["0"])[0])
        if time.time() - auth_date > 86400:
            raise HTTPException(status_code=401, detail="initData expired")

        user_raw = parsed.get("user", ["{}"])[0]
        user = json.loads(unquote(user_raw))
        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.warning("initData validation error: %s", e)
        raise HTTPException(status_code=401, detail="Invalid initData")


def get_current_user(init_data: str = Query(..., alias="initData")) -> dict:
    return validate_init_data(init_data)


# ── Pydantic модели ────────────────────────────────────────────────────────────

class WatchlistAddRequest(BaseModel):
    item_name: str
    threshold: float = 0.0


class PremiumActivateRequest(BaseModel):
    telegram_payment_charge_id: str


class WatchlistUpdateRequest(BaseModel):
    threshold: float


class PortfolioAddRequest(BaseModel):
    item_name: str
    buy_price: float


class CurrencyRequest(BaseModel):
    currency: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/me")
def get_me(user: dict = Depends(get_current_user)):
    """Возвращает настройки текущего пользователя."""
    user_id = user["id"]
    touch_activity(user_id)
    settings = get_user_settings(user_id)
    currency = settings.get("currency", "RUB")
    cur_info = CURRENCIES.get(currency, CURRENCIES["RUB"])
    premium = is_premium(user_id)
    checks_today = get_price_checks_today(user_id)
    bonus_wl = get_bonus_watchlist(user_id)
    return {
        "user": user,
        "currency": currency,
        "currency_symbol": cur_info["symbol"],
        "currencies": [
            {"code": code, "name": info["name"], "symbol": info["symbol"]}
            for code, info in CURRENCIES.items()
        ],
        "price_checks_today": checks_today,
        "price_checks_limit": None if premium else FREE_DAILY_PRICE_CHECKS,
        "price_checks_left": None if premium else max(0, FREE_DAILY_PRICE_CHECKS - checks_today),
        "bonus_watchlist": bonus_wl,
    }


@app.post("/api/settings/currency")
def set_currency(body: CurrencyRequest, user: dict = Depends(get_current_user)):
    if body.currency not in CURRENCIES:
        raise HTTPException(status_code=400, detail="Unknown currency")
    set_user_currency(user["id"], body.currency)
    return {"ok": True}


@app.get("/api/search")
def search(q: str = Query(..., min_length=2), user: dict = Depends(get_current_user)):
    """Поиск предметов по частичному названию. Возвращает [{name, icon}]."""
    results = search_items(q, count=8)
    return {"results": results}


@app.get("/api/price")
def price(
    item: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """Получить текущую цену предмета."""
    user_id = user["id"]
    settings = get_user_settings(user_id)

    # Лимит проверок цены для бесплатных пользователей
    if not is_premium(user_id):
        checks_today = get_price_checks_today(user_id)
        if checks_today >= FREE_DAILY_PRICE_CHECKS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Бесплатный план — максимум {FREE_DAILY_PRICE_CHECKS} проверок цены в день. "
                    f"Оформи Premium для безлимитного доступа."
                )
            )

    currency = settings.get("currency", "RUB")
    data = get_item_price(item, currency)
    if not data:
        raise HTTPException(status_code=404, detail="Item not found or Steam unavailable")

    # Логируем проверку и сохраняем в историю
    log_user_price_check(user_id)
    if data["lowest"] > 0:
        record_price(item, data["lowest"])
    return data


@app.get("/api/watchlist")
def get_watchlist_api(user: dict = Depends(get_current_user)):
    items = get_watchlist(user["id"])
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]

    # Добавляем текущие цены
    result = []
    for item in items:
        price_data = get_item_price(item["item_name"], currency)
        result.append({
            **item,
            "current_price": price_data["lowest"] if price_data else None,
            "symbol": symbol,
        })
    return {"items": result, "symbol": symbol}


@app.post("/api/watchlist")
def add_watchlist_api(body: WatchlistAddRequest, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    if not is_premium(user_id):
        current = get_watchlist(user_id)
        bonus = get_bonus_watchlist(user_id)
        effective_limit = FREE_WATCHLIST_LIMIT + bonus
        if len(current) >= effective_limit:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Бесплатный план — максимум {effective_limit} предметов в вотчлисте. "
                    f"Пригласи друга (+3 слота) или оформи Premium для безлимитного доступа."
                )
            )
    added = add_to_watchlist(user_id, body.item_name, body.threshold)
    if not added:
        raise HTTPException(status_code=409, detail="Предмет уже есть в вотчлисте")
    return {"ok": True}


@app.patch("/api/watchlist/{item_id}")
def update_watchlist_api(
    item_id: int,
    body: WatchlistUpdateRequest,
    user: dict = Depends(get_current_user),
):
    update_watchlist_threshold(item_id, user["id"], body.threshold)
    return {"ok": True}


@app.delete("/api/watchlist/{item_id}")
def delete_watchlist_api(item_id: int, user: dict = Depends(get_current_user)):
    removed = remove_from_watchlist(item_id, user["id"])
    if not removed:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@app.get("/api/portfolio")
def get_portfolio_api(user: dict = Depends(get_current_user)):
    items = get_portfolio(user["id"])
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]

    total_buy = 0.0
    total_now = 0.0
    result = []

    for item in items:
        price_data = get_item_price(item["item_name"], currency)
        current = price_data["lowest"] if price_data else None
        buy = item["buy_price"]
        total_buy += buy
        if current:
            total_now += current
            profit = current - buy
            profit_pct = (profit / buy * 100) if buy > 0 else 0
        else:
            profit = None
            profit_pct = None

        result.append({
            **item,
            "current_price": current,
            "profit": profit,
            "profit_pct": profit_pct,
            "symbol": symbol,
        })

    return {
        "items": result,
        "total_buy": total_buy,
        "total_now": total_now,
        "total_profit": total_now - total_buy if total_now else None,
        "symbol": symbol,
    }


@app.post("/api/portfolio")
def add_portfolio_api(body: PortfolioAddRequest, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    if not is_premium(user_id):
        current = get_portfolio(user_id)
        if len(current) >= FREE_PORTFOLIO_LIMIT:
            raise HTTPException(
                status_code=403,
                detail=f"Бесплатный план — максимум {FREE_PORTFOLIO_LIMIT} предметов в портфеле. Оформи Premium для безлимитного доступа."
            )
    add_to_portfolio(user_id, body.item_name, body.buy_price)
    return {"ok": True}


@app.delete("/api/portfolio/{item_id}")
def delete_portfolio_api(item_id: int, user: dict = Depends(get_current_user)):
    removed = remove_from_portfolio(item_id, user["id"])
    if not removed:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


@app.get("/api/history")
def get_history(item: str = Query(...), user: dict = Depends(get_current_user)):
    """История цен предмета для графика."""
    history = get_price_history(item, limit=30)
    return {
        "item_name": item,
        "history": [
            {"price": h["price"], "date": h["recorded_at"]}
            for h in reversed(history)
        ],
    }


def _fetch_item_list(item_names: list, currency: str, symbol: str) -> list:
    """Вспомогательная функция: получает цены и иконки для списка предметов."""
    result = []
    # Получаем иконки через поиск (кэшированные имена)
    icon_map = {}
    for item_name in item_names:
        items = search_items(item_name, count=1)
        if items:
            icon_map[item_name] = items[0].get("icon", "")

    for item_name in item_names:
        price_data = get_item_price(item_name, currency)
        if price_data and price_data["lowest"] > 0:
            result.append({
                "item_name": item_name,
                "lowest": price_data["lowest"],
                "symbol": symbol,
                "icon": icon_map.get(item_name, ""),
            })
    return result


@app.get("/api/top-items")
def get_top_items(user: dict = Depends(get_current_user)):
    """Популярные предметы с текущими ценами."""
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    result = _fetch_item_list(TOP_ITEMS, currency, symbol)
    return {"items": result}


@app.get("/api/expensive-items")
def get_expensive_items(user: dict = Depends(get_current_user)):
    """Самые дорогие предметы с текущими ценами."""
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    result = _fetch_item_list(EXPENSIVE_ITEMS, currency, symbol)
    return {"items": result}


@app.get("/api/premium")
def get_premium_status(user: dict = Depends(get_current_user)):
    """Статус премиума пользователя."""
    user_id = user["id"]
    premium = is_premium(user_id)
    return {
        "premium": premium,
        "watchlist_limit": None if premium else FREE_WATCHLIST_LIMIT,
        "portfolio_limit": None if premium else FREE_PORTFOLIO_LIMIT,
    }


@app.get("/health")
def health():
    return {"status": "ok"}
