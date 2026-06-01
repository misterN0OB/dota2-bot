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
from typing import Optional

from config import BOT_TOKEN
from database import (
    init_db,
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
    log_event,
    get_price_checks_this_week,
    log_user_price_check,
    get_bonus_watchlist,
    get_bonus_price_checks,
    add_bonus_price_checks,
    add_bonus_watchlist_slot,
    FREE_WATCHLIST_LIMIT,
    MAX_WATCHLIST_WITH_ADS,
    FREE_WEEKLY_PRICE_CHECKS,
    AD_REWARD_PRICE_CHECKS,
    AD_REWARD_WATCHLIST,
)
from skin_checker import get_item_price, search_items, CURRENCIES

FREE_PORTFOLIO_LIMIT = 5

# Популярные предметы Dota 2
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

@app.on_event("startup")
def on_startup():
    init_db()  # запускаем миграции при старте API

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Telegram initData validation ───────────────────────────────────────────────

def validate_init_data(init_data: str) -> dict:
    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
        hash_value = parsed.get("hash", [""])[0]
        data_check_parts = []
        for key, values in sorted(parsed.items()):
            if key != "hash":
                data_check_parts.append(f"{key}={values[0]}")
        data_check_string = "\n".join(data_check_parts)
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_hash, hash_value):
            raise HTTPException(status_code=401, detail="Invalid initData signature")
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
    threshold_high: Optional[float] = None


class WatchlistUpdateRequest(BaseModel):
    threshold: float = 0.0
    threshold_high: Optional[float] = None


class PortfolioAddRequest(BaseModel):
    item_name: str
    buy_price: float


class CurrencyRequest(BaseModel):
    currency: str


class AdRewardRequest(BaseModel):
    reward_type: str  # "price_checks" или "watchlist"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_price_checks_info(user_id: int, premium: bool) -> dict:
    if premium:
        return {"checks_this_week": 0, "checks_limit": None, "checks_left": None, "bonus_checks": 0}
    checks = get_price_checks_this_week(user_id)
    bonus = get_bonus_price_checks(user_id)
    limit = FREE_WEEKLY_PRICE_CHECKS + bonus
    return {
        "checks_this_week": checks,
        "checks_limit": limit,
        "checks_left": max(0, limit - checks),
        "bonus_checks": bonus,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/me")
def get_me(user: dict = Depends(get_current_user)):
    user_id = user["id"]
    touch_activity(user_id)
    settings = get_user_settings(user_id)
    currency = settings.get("currency", "RUB")
    cur_info = CURRENCIES.get(currency, CURRENCIES["RUB"])
    premium = is_premium(user_id)
    bonus_wl = get_bonus_watchlist(user_id)
    checks_info = _get_price_checks_info(user_id, premium)
    return {
        "user": user,
        "currency": currency,
        "currency_symbol": cur_info["symbol"],
        "currencies": [
            {"code": code, "name": info["name"], "symbol": info["symbol"]}
            for code, info in CURRENCIES.items()
        ],
        "premium": premium,
        "bonus_watchlist": bonus_wl,
        "watchlist_limit": None if premium else FREE_WATCHLIST_LIMIT + bonus_wl,
        "watchlist_max_free": MAX_WATCHLIST_WITH_ADS,
        **checks_info,
    }


@app.post("/api/settings/currency")
def set_currency(body: CurrencyRequest, user: dict = Depends(get_current_user)):
    if body.currency not in CURRENCIES:
        raise HTTPException(status_code=400, detail="Unknown currency")
    set_user_currency(user["id"], body.currency)
    return {"ok": True}


@app.get("/api/search")
def search(q: str = Query(..., min_length=2), user: dict = Depends(get_current_user)):
    results = search_items(q, count=8)
    return {"results": results}


@app.get("/api/price")
def price(item: str = Query(...), user: dict = Depends(get_current_user)):
    user_id = user["id"]
    premium = is_premium(user_id)

    if not premium:
        bonus = get_bonus_price_checks(user_id)
        limit = FREE_WEEKLY_PRICE_CHECKS + bonus
        checks = get_price_checks_this_week(user_id)
        if checks >= limit:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Лимит {limit} проверок в неделю исчерпан. "
                    f"Посмотри рекламу (+{AD_REWARD_PRICE_CHECKS} проверки) или оформи Premium."
                )
            )

    settings = get_user_settings(user_id)
    currency = settings.get("currency", "RUB")
    data = get_item_price(item, currency)
    if not data:
        raise HTTPException(status_code=404, detail="Item not found or Steam unavailable")

    log_user_price_check(user_id)
    if data["lowest"] > 0:
        record_price(item, data["lowest"])

    # Добавляем актуальный счётчик в ответ
    if not premium:
        bonus = get_bonus_price_checks(user_id)
        limit = FREE_WEEKLY_PRICE_CHECKS + bonus
        checks_now = get_price_checks_this_week(user_id)
        data["checks_left"] = max(0, limit - checks_now)
        data["checks_limit"] = limit
    return data


@app.get("/api/watchlist")
def get_watchlist_api(user: dict = Depends(get_current_user)):
    items = get_watchlist(user["id"])
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
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
                    f"Достигнут лимит {effective_limit} предметов. "
                    f"Посмотри рекламу (+1 слот, макс {MAX_WATCHLIST_WITH_ADS}) "
                    f"или оформи Premium для безлимитного вотчлиста."
                )
            )
    thr_high = body.threshold_high if body.threshold_high and body.threshold_high > 0 else 0.0
    added = add_to_watchlist(user_id, body.item_name, body.threshold, thr_high)
    if not added:
        raise HTTPException(status_code=409, detail="Предмет уже есть в вотчлисте")
    return {"ok": True}


@app.patch("/api/watchlist/{item_id}")
def update_watchlist_api(
    item_id: int,
    body: WatchlistUpdateRequest,
    user: dict = Depends(get_current_user),
):
    thr_high = body.threshold_high if body.threshold_high and body.threshold_high > 0 else 0.0
    update_watchlist_threshold(item_id, user["id"], body.threshold, thr_high)
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
        result.append({**item, "current_price": current, "profit": profit,
                       "profit_pct": profit_pct, "symbol": symbol})
    return {
        "items": result, "total_buy": total_buy, "total_now": total_now,
        "total_profit": total_now - total_buy if total_now else None, "symbol": symbol,
    }


@app.post("/api/portfolio")
def add_portfolio_api(body: PortfolioAddRequest, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    if not is_premium(user_id):
        current = get_portfolio(user_id)
        if len(current) >= FREE_PORTFOLIO_LIMIT:
            raise HTTPException(
                status_code=403,
                detail=f"Бесплатный план — максимум {FREE_PORTFOLIO_LIMIT} предметов в портфеле. Оформи Premium."
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
    history = get_price_history(item, limit=30)
    return {
        "item_name": item,
        "history": [{"price": h["price"], "date": h["recorded_at"]} for h in reversed(history)],
    }


@app.post("/api/ad-reward")
def ad_reward(body: AdRewardRequest, user: dict = Depends(get_current_user)):
    """Начисляет награду за просмотр рекламы."""
    user_id = user["id"]
    if is_premium(user_id):
        return {"ok": True, "message": "У тебя уже Premium — реклама не нужна!"}

    if body.reward_type == "price_checks":
        add_bonus_price_checks(user_id, AD_REWARD_PRICE_CHECKS)
        log_event("ad_reward_price_checks")      # для статистики
        bonus = get_bonus_price_checks(user_id)
        limit = FREE_WEEKLY_PRICE_CHECKS + bonus
        checks = get_price_checks_this_week(user_id)
        return {
            "ok": True,
            "reward": AD_REWARD_PRICE_CHECKS,
            "checks_left": max(0, limit - checks),
            "checks_limit": limit,
            "message": f"+{AD_REWARD_PRICE_CHECKS} проверки добавлены!",
        }
    elif body.reward_type == "watchlist":
        added = add_bonus_watchlist_slot(user_id)
        log_event("ad_reward_watchlist")         # для статистики
        bonus = get_bonus_watchlist(user_id)
        if not added:
            raise HTTPException(
                status_code=400,
                detail=f"Достигнут максимум {MAX_WATCHLIST_WITH_ADS} слотов вотчлиста. Оформи Premium для безлимита."
            )
        return {
            "ok": True,
            "reward": AD_REWARD_WATCHLIST,
            "watchlist_limit": FREE_WATCHLIST_LIMIT + bonus,
            "watchlist_max": MAX_WATCHLIST_WITH_ADS,
            "message": f"+1 слот в вотчлист! Теперь {FREE_WATCHLIST_LIMIT + bonus}/{MAX_WATCHLIST_WITH_ADS}",
        }
    else:
        raise HTTPException(status_code=400, detail="Unknown reward_type")


def _fetch_item_list(item_names: list, currency: str, symbol: str) -> list:
    result = []
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
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    return {"items": _fetch_item_list(TOP_ITEMS, currency, symbol)}


@app.get("/api/expensive-items")
def get_expensive_items(user: dict = Depends(get_current_user)):
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    return {"items": _fetch_item_list(EXPENSIVE_ITEMS, currency, symbol)}


@app.get("/api/premium")
def get_premium_status(user: dict = Depends(get_current_user)):
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
