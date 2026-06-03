"""
Dota 2 Price Bot — FastAPI backend для Telegram Mini App
Запуск: uvicorn api:app --host 0.0.0.0 --port 8000
"""

import hashlib
import hmac
import json
import time
import logging
import requests as req_lib
from urllib.parse import unquote, parse_qs

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from config import BOT_TOKEN, ADMIN_ID
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
    get_top_growth_items,
    get_daily_stats,
    get_daily_active_users,
    get_top_items_by_checks,
    get_total_users,
    get_active_users_count,
    get_premium_count,
    get_total_price_checks,
    get_watchlist_total,
    get_portfolio_total,
    get_all_users,
)
from skin_checker import get_item_price, search_items, CURRENCIES

FREE_PORTFOLIO_LIMIT = 5

# Кеш для топ/дорогих предметов (защита от Steam rate limit)
_item_cache: dict = {}
CACHE_TTL = 1800  # 30 минут

# Популярные предметы Dota 2 (берём 8 — покажем первые 5 с ценой > 0)
TOP_ITEMS = [
    "Dragonclaw Hook",
    "Genuine Dragonclaw Hook",
    "Tempest Helm of the Thundergod",
    "Sylvan Cascade",
    "Inscribed Blades of the Reaper",
    "Demon Eater",
    "Timebreaker",
    "Inscribed Fractal Horns of Inner Abysm",
]

# Самые дорогие предметы Dota 2 (берём 8 — покажем первые 5 с ценой > 0)
EXPENSIVE_ITEMS = [
    "Genuine Dragonclaw Hook",
    "Dragonclaw Hook",
    "Tempest Helm of the Thundergod",
    "Genuine Resonant Virtue",
    "Inscribed Corrupted Monarch Bow",
    "Genuine Swine of the Sunken Galley",
    "Unusual Fiery Soul of the Slayer",
    "Inscribed Fractal Horns of Inner Abysm",
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
    touch_activity(user_id, user.get("username"), user.get("first_name"))
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
    results = search_items(q, count=15)
    return {"results": results}


@app.get("/api/price-preview")
def price_preview(items: str = Query(...), user: dict = Depends(get_current_user)):
    """Последние известные цены из БД — без запросов к Steam."""
    item_list = [i.strip() for i in items.split(",") if i.strip()][:8]
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    prices = {}
    for name in item_list:
        history = get_price_history(name, limit=1)
        if history:
            prices[name] = history[0]["price"]
    return {"prices": prices, "symbol": symbol, "from_cache": True}


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
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    data = get_item_price(item, currency)
    if not data:
        # Fallback: последняя известная цена из БД
        history = get_price_history(item, limit=1)
        if history:
            data = {
                "lowest": history[0]["price"],
                "median": history[0]["price"],
                "volume": 0,
                "symbol": symbol,
                "currency": currency,
                "item_name": item,
                "from_cache": True,
            }
        else:
            raise HTTPException(status_code=404, detail="Предмет не найден или Steam недоступен. Попробуй позже.")

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
        # Пробуем из кэша/Steam, при неудаче берём последнюю цену из БД
        price_data = get_item_price(item["item_name"], currency, retries=1, retry_delay=0)
        current_price = None
        if price_data:
            current_price = price_data["lowest"]
        else:
            history = get_price_history(item["item_name"], limit=1)
            if history:
                current_price = history[0]["price"]
        result.append({
            **item,
            "current_price": current_price,
            "symbol": symbol,
            "icon": item.get("icon", ""),
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
    # Получаем иконку при добавлении (один раз)
    icon = ""
    sr = search_items(body.item_name, count=1)
    if sr:
        icon = sr[0].get("icon", "")
    added = add_to_watchlist(user_id, body.item_name, body.threshold, thr_high, icon)
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


def _fetch_item_list(item_names: list, currency: str, symbol: str, limit: int = 5) -> list:
    result = []
    icon_map = {}
    # Иконки получаем только если Steam не rate-limit (один запрос для всех)
    try:
        for item_name in item_names[:limit]:
            sr = search_items(item_name, count=1)
            if sr:
                icon_map[item_name] = sr[0].get("icon", "")
            time.sleep(0.3)
    except Exception:
        pass
    for item_name in item_names:
        if len(result) >= limit:
            break
        if result:
            time.sleep(0.5)
        price_data = get_item_price(item_name, currency, retries=1, retry_delay=0)
        if price_data and price_data["lowest"] > 0:
            result.append({
                "item_name": item_name,
                "lowest": price_data["lowest"],
                "symbol": symbol,
                "icon": icon_map.get(item_name, ""),
            })
        else:
            # Fallback: берём из price_history
            history = get_price_history(item_name, limit=1)
            if history and history[0]["price"] > 0:
                result.append({
                    "item_name": item_name,
                    "lowest": history[0]["price"],
                    "symbol": symbol,
                    "icon": icon_map.get(item_name, ""),
                    "from_cache": True,
                })
    return result


def _fetch_item_list_cached(cache_key: str, item_names: list, currency: str, symbol: str, limit: int = 5) -> list:
    """Возвращает список предметов с кешированием на 5 минут.
    Кешируем только если получили limit или больше предметов."""
    key = f"{cache_key}:{currency}"
    cached = _item_cache.get(key)
    # Используем кеш только если в нём достаточно предметов
    if cached and len(cached["data"]) >= limit and time.time() - cached["ts"] < CACHE_TTL:
        for item in cached["data"]:
            item["symbol"] = symbol
        return cached["data"]
    fresh = _fetch_item_list(item_names, currency, symbol, limit=limit)
    if len(fresh) >= limit:
        # Полный результат — кешируем
        _item_cache[key] = {"data": fresh, "ts": time.time()}
    elif fresh and (not cached or len(fresh) > len(cached.get("data", []))):
        # Частичный, но лучше чем в кеше — сохраняем без TTL (перепроверим при след. запросе)
        _item_cache[key] = {"data": fresh, "ts": 0}
    if fresh:
        return fresh
    # Возвращаем устаревший кеш если Steam не отвечает
    if cached:
        for item in cached["data"]:
            item["symbol"] = symbol
        return cached["data"]
    return []


@app.get("/api/top-items")
def get_top_items(user: dict = Depends(get_current_user)):
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    return {"items": _fetch_item_list_cached("top", TOP_ITEMS, currency, symbol)}


@app.get("/api/expensive-items")
def get_expensive_items(user: dict = Depends(get_current_user)):
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    return {"items": _fetch_item_list_cached("expensive", EXPENSIVE_ITEMS, currency, symbol)}


@app.get("/api/top-growth")
def get_top_growth(user: dict = Depends(get_current_user)):
    settings = get_user_settings(user["id"])
    currency = settings.get("currency", "RUB")
    symbol = CURRENCIES.get(currency, CURRENCIES["RUB"])["symbol"]
    rows = get_top_growth_items(limit=5)
    items = []
    for r in rows:
        growth_pct = (r["latest"] - r["prev"]) / r["prev"] * 100
        items.append({
            "item_name": r["item_name"],
            "latest": r["latest"],
            "prev": r["prev"],
            "growth_pct": round(growth_pct, 1),
            "symbol": symbol,
            "icon": "",
        })
    return {"items": items}


@app.get("/api/premium")
def get_premium_status(user: dict = Depends(get_current_user)):
    user_id = user["id"]
    premium = is_premium(user_id)
    return {
        "premium": premium,
        "watchlist_limit": None if premium else FREE_WATCHLIST_LIMIT,
        "portfolio_limit": None if premium else FREE_PORTFOLIO_LIMIT,
    }


@app.post("/api/buy-premium")
def buy_premium_endpoint(user: dict = Depends(get_current_user)):
    """Отправляет Stars-инвойс напрямую в чат пользователя через Bot API."""
    user_id = user["id"]
    if is_premium(user_id):
        return {"ok": True, "already_premium": True, "message": "У тебя уже есть Premium! ⭐"}
    try:
        resp = req_lib.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendInvoice",
            json={
                "chat_id": user_id,
                "title": "Dota 2 Tracker Premium",
                "description": (
                    "✅ Безлимитные проверки цены\n"
                    "✅ Безлимитное Избранное\n"
                    "✅ Безлимитный портфель\n"
                    "✅ Без рекламы"
                ),
                "payload": "premium_purchase",
                "currency": "XTR",
                "prices": [{"label": "Premium доступ", "amount": 200}],
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            # Отправляем сообщение с кнопкой "Вернуться в трекер"
            req_lib.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": user_id,
                    "text": "После оплаты Premium активируется автоматически ✅",
                    "reply_markup": {
                        "inline_keyboard": [[
                            {
                                "text": "🚀 Вернуться в трекер",
                                "web_app": {"url": "https://mistern0ob.github.io/dota2-bot/"}
                            }
                        ]]
                    }
                },
                timeout=5,
            )
            return {"ok": True, "message": "Инвойс отправлен! Открой чат с ботом."}
        else:
            logger.error("sendInvoice error: %s", data)
            raise HTTPException(status_code=500, detail="Не удалось создать инвойс")
    except req_lib.RequestException as e:
        logger.error("sendInvoice request error: %s", e)
        raise HTTPException(status_code=500, detail="Ошибка соединения с Telegram")


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Admin stats ────────────────────────────────────────────────────────────────

def _check_admin_token(token: str = Query(..., alias="token")):
    expected = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:24]
    if token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True


@app.get("/api/admin/stats")
def admin_stats(_: bool = Depends(_check_admin_token)):
    return {
        "summary": {
            "total_users": get_total_users(),
            "active_24h": get_active_users_count(hours=24),
            "active_7d": get_active_users_count(hours=168),
            "premium": get_premium_count(),
            "checks_today": get_total_price_checks(days=1),
            "checks_week": get_total_price_checks(days=7),
            "watchlist_total": get_watchlist_total(),
            "portfolio_total": get_portfolio_total(),
        },
        "daily": get_daily_stats(days=30),
        "daily_active": get_daily_active_users(days=30),
        "top_items": get_top_items_by_checks(limit=10),
        "users": get_all_users(),
    }


@app.get("/admin")
def admin_page(_: bool = Depends(_check_admin_token)):
    from fastapi.responses import HTMLResponse
    token = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:24]
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dota 2 Bot — Статистика</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{{--bg:#0d0d14;--surface:#16161f;--card:#1e1e2e;--border:#2a2a3e;--gold:#c89b3c;--green:#3caa6e;--red:#c84040;--text:#e8e8f0;--dim:#888899;}}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;}}
  .header{{background:linear-gradient(135deg,#0d0d14,#1a0a0a);border-bottom:1px solid var(--border);padding:20px 24px;display:flex;align-items:center;gap:12px;}}
  .header h1{{font-size:20px;font-weight:800;color:var(--gold);}}
  .header .sub{{font-size:13px;color:var(--dim);margin-left:auto;}}
  .container{{max-width:1200px;margin:0 auto;padding:24px;}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px;}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;}}
  .card-label{{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;}}
  .card-value{{font-size:28px;font-weight:800;color:var(--gold);}}
  .card-sub{{font-size:12px;color:var(--dim);margin-top:4px;}}
  .charts{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px;}}
  @media(max-width:768px){{.charts{{grid-template-columns:1fr;}}}}
  .chart-box{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;}}
  .chart-title{{font-size:13px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.6px;margin-bottom:16px;}}
  .chart-full{{grid-column:1/-1;}}
  table{{width:100%;border-collapse:collapse;}}
  th{{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px;padding:8px 12px;text-align:left;border-bottom:1px solid var(--border);}}
  td{{font-size:13px;padding:10px 12px;border-bottom:1px solid #1a1a2a;}}
  tr:last-child td{{border:none;}}
  tr:hover td{{background:rgba(255,255,255,.02);}}
  .badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;}}
  .badge-gold{{background:rgba(200,155,60,.2);color:var(--gold);}}
  .badge-green{{background:rgba(60,170,110,.2);color:var(--green);}}
  .section-title{{font-size:14px;font-weight:700;color:var(--text);margin-bottom:12px;margin-top:24px;}}
  .table-wrap{{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:24px;}}
  .refresh{{background:var(--gold);color:#000;border:none;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:700;cursor:pointer;}}
  .loading{{text-align:center;padding:60px;color:var(--dim);font-size:16px;}}
</style>
</head>
<body>
<div class="header">
  <span style="font-size:28px">🎮</span>
  <h1>Dota 2 Price Bot</h1>
  <div class="sub">
    <span id="lastUpdate">Загрузка...</span>
    &nbsp;&nbsp;
    <button class="refresh" onclick="loadData()">↻ Обновить</button>
  </div>
</div>
<div class="container">
  <div id="content" class="loading">Загружаем статистику...</div>
</div>
<script>
const TOKEN = "{token}";
const API = "/api/admin/stats?token=" + TOKEN;

async function loadData() {{
  try {{
    const r = await fetch(API);
    const d = await r.json();
    render(d);
    document.getElementById("lastUpdate").textContent = "Обновлено: " + new Date().toLocaleTimeString("ru-RU");
  }} catch(e) {{
    document.getElementById("content").innerHTML = "<p style='color:red'>Ошибка загрузки: " + e.message + "</p>";
  }}
}}

function render(d) {{
  const s = d.summary;
  const days = d.daily.map(x => x.day.slice(5));
  const newUsers = d.daily.map(x => x.new_users || 0);
  const checks = d.daily.map(x => x.price_checks || 0);
  const premium = d.daily.map(x => x.premium_sales || 0);
  const activeD = d.daily_active.map(x => x.day.slice(5));
  const activeV = d.daily_active.map(x => x.active_users || 0);

  document.getElementById("content").innerHTML = `
    <div class="cards">
      <div class="card"><div class="card-label">Всего пользователей</div><div class="card-value">${{s.total_users}}</div></div>
      <div class="card"><div class="card-label">Активны за 24ч</div><div class="card-value" style="color:var(--green)">${{s.active_24h}}</div></div>
      <div class="card"><div class="card-label">Активны за 7 дней</div><div class="card-value">${{s.active_7d}}</div></div>
      <div class="card"><div class="card-label">Premium</div><div class="card-value" style="color:var(--gold)">${{s.premium}}</div></div>
      <div class="card"><div class="card-label">Проверок сегодня</div><div class="card-value">${{s.checks_today}}</div></div>
      <div class="card"><div class="card-label">Проверок за неделю</div><div class="card-value">${{s.checks_week}}</div></div>
      <div class="card"><div class="card-label">В Избранном</div><div class="card-value">${{s.watchlist_total}}</div><div class="card-sub">предметов</div></div>
      <div class="card"><div class="card-label">В Портфелях</div><div class="card-value">${{s.portfolio_total}}</div><div class="card-sub">предметов</div></div>
    </div>

    <div class="charts">
      <div class="chart-box">
        <div class="chart-title">📈 Новые пользователи (30 дней)</div>
        <canvas id="chartUsers"></canvas>
      </div>
      <div class="chart-box">
        <div class="chart-title">🔍 Проверки цен (30 дней)</div>
        <canvas id="chartChecks"></canvas>
      </div>
      <div class="chart-box chart-full">
        <div class="chart-title">👥 Активные пользователи (30 дней)</div>
        <canvas id="chartActive"></canvas>
      </div>
    </div>

    <div class="section-title">🔥 Топ предметов по проверкам</div>
    <div class="table-wrap">
      <table>
        <tr><th>#</th><th>Предмет</th><th>Проверок</th></tr>
        ${{d.top_items.map((x,i) => `<tr><td style="color:var(--dim)">${{i+1}}</td><td>${{x.item_name}}</td><td><span class="badge badge-green">${{x.checks}}</span></td></tr>`).join("")}}
      </table>
    </div>

    <div class="section-title">👥 Пользователи</div>
    <div class="table-wrap">
      <table>
        <tr><th>Пользователь</th><th>ID</th><th>Последняя активность</th></tr>
        ${{d.users.map(u => `<tr>
          <td>${{u.username ? "@"+u.username : u.first_name || "—"}}</td>
          <td style="color:var(--dim);font-size:12px">${{u.user_id}}</td>
          <td style="color:var(--dim)">${{(u.last_seen||"").slice(0,16).replace("T"," ")}}</td>
        </tr>`).join("")}}
      </table>
    </div>
  `;

  const cfg = (labels, data, color, label) => ({{
    type: "line",
    data: {{ labels, datasets: [{{ label, data, borderColor: color, backgroundColor: color+"22", borderWidth: 2, pointRadius: 3, fill: true, tension: 0.3 }}] }},
    options: {{ responsive: true, plugins: {{ legend: {{display:false}} }}, scales: {{ x: {{ticks:{{color:"#888899",font:{{size:10}}}},grid:{{color:"#2a2a3e"}}}}, y: {{ticks:{{color:"#888899",font:{{size:10}},stepSize:1}},grid:{{color:"#2a2a3e"}},beginAtZero:true}} }} }}
  }});

  new Chart(document.getElementById("chartUsers"), cfg(days, newUsers, "#c89b3c", "Новые пользователи"));
  new Chart(document.getElementById("chartChecks"), cfg(days, checks, "#3caa6e", "Проверки цен"));
  new Chart(document.getElementById("chartActive"), cfg(activeD, activeV, "#4a9eff", "Активные пользователи"));
}}

loadData();
setInterval(loadData, 60000);
</script>
</body>
</html>"""
    return HTMLResponse(html)
