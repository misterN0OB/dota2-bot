import sqlite3
from datetime import datetime, timedelta

DB_PATH = "dota2bot.db"

FREE_WATCHLIST_LIMIT = 7          # базовых слотов вотчлиста
MAX_WATCHLIST_WITH_ADS = 10       # максимум с бонусами за рекламу
FREE_WEEKLY_PRICE_CHECKS = 15     # проверок цены в неделю
AD_REWARD_PRICE_CHECKS = 3        # проверок за просмотр рекламы
AD_REWARD_WATCHLIST = 1           # слот вотчлиста за рекламу


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                currency TEXT DEFAULT 'RUB',
                premium INTEGER DEFAULT 0,
                compare_count INTEGER DEFAULT 0,
                week_start TEXT DEFAULT '',
                bonus_compares INTEGER DEFAULT 0,
                bonus_watchlist INTEGER DEFAULT 0,
                bonus_price_checks INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item_name TEXT,
                threshold REAL DEFAULT 0,
                threshold_high REAL DEFAULT NULL,
                last_notified TEXT DEFAULT NULL,
                last_notified_high TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS user_activity (
                user_id INTEGER PRIMARY KEY,
                last_seen TEXT
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT,
                price REAL,
                recorded_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER UNIQUE NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item_name TEXT,
                buy_price REAL,
                added_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        # Миграции: добавляем колонки если их ещё нет
        for migration in [
            "ALTER TABLE watchlist ADD COLUMN last_notified TEXT DEFAULT NULL",
            "ALTER TABLE watchlist ADD COLUMN threshold_high REAL DEFAULT NULL",
            "ALTER TABLE watchlist ADD COLUMN last_notified_high TEXT DEFAULT NULL",
            "ALTER TABLE user_settings ADD COLUMN bonus_watchlist INTEGER DEFAULT 0",
            "ALTER TABLE user_settings ADD COLUMN bonus_price_checks INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass  # колонка уже существует


# ── user_settings ──────────────────────────────────────────────────────────────

def get_user_settings(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            return dict(row)
        conn.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,)
        )
        return {
            "user_id": user_id, "currency": "RUB", "premium": 0,
            "compare_count": 0, "week_start": "", "bonus_compares": 0,
            "bonus_watchlist": 0, "bonus_price_checks": 0,
        }


def set_user_currency(user_id: int, currency: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, currency)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET currency = excluded.currency
        """, (user_id, currency))


def is_premium(user_id: int) -> bool:
    settings = get_user_settings(user_id)
    return bool(settings.get("premium"))


def set_premium(user_id: int, value: bool = True):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, premium)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET premium = excluded.premium
        """, (user_id, int(value)))


def get_compares_left(user_id: int, free_per_week: int) -> int:
    settings = get_user_settings(user_id)
    now = datetime.utcnow()
    week_start_str = settings.get("week_start", "")
    if week_start_str:
        try:
            week_start = datetime.fromisoformat(week_start_str)
            if now - week_start >= timedelta(weeks=1):
                week_start_str = ""
        except ValueError:
            week_start_str = ""
    if not week_start_str:
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO user_settings (user_id, week_start, compare_count)
                VALUES (?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE
                    SET week_start = excluded.week_start,
                        compare_count = 0
            """, (user_id, now.isoformat()))
        settings["compare_count"] = 0
    bonus = settings.get("bonus_compares", 0)
    used = settings.get("compare_count", 0)
    return max(0, free_per_week + bonus - used)


def use_compare(user_id: int):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, compare_count)
            VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE
                SET compare_count = compare_count + 1
        """, (user_id,))


# ── watchlist ──────────────────────────────────────────────────────────────────

def add_to_watchlist(user_id: int, item_name: str,
                     threshold: float = 0.0, threshold_high: float = 0.0) -> bool:
    """Добавляет предмет в вотчлист. Возвращает False если уже есть."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM watchlist WHERE user_id = ? AND item_name = ?",
            (user_id, item_name)
        ).fetchone()
        if existing:
            return False
        thr_high = threshold_high if threshold_high and threshold_high > 0 else None
        conn.execute(
            "INSERT INTO watchlist (user_id, item_name, threshold, threshold_high) VALUES (?, ?, ?, ?)",
            (user_id, item_name, threshold, thr_high)
        )
        return True


def get_watchlist(user_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watchlist WHERE user_id = ? ORDER BY id",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def remove_from_watchlist(watchlist_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM watchlist WHERE id = ? AND user_id = ?",
            (watchlist_id, user_id)
        )
        return cur.rowcount > 0


def get_all_watchlist_items() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM watchlist").fetchall()
        return [dict(r) for r in rows]


def update_watchlist_notified(watchlist_id: int, high: bool = False):
    col = "last_notified_high" if high else "last_notified"
    with get_conn() as conn:
        conn.execute(
            f"UPDATE watchlist SET {col} = datetime('now') WHERE id = ?",
            (watchlist_id,)
        )


def update_watchlist_threshold(watchlist_id: int, user_id: int,
                                threshold: float, threshold_high: float = 0.0):
    thr_high = threshold_high if threshold_high and threshold_high > 0 else None
    with get_conn() as conn:
        conn.execute(
            "UPDATE watchlist SET threshold = ?, threshold_high = ? WHERE id = ? AND user_id = ?",
            (threshold, thr_high, watchlist_id, user_id)
        )


# ── price_history ──────────────────────────────────────────────────────────────

def record_price(item_name: str, price: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO price_history (item_name, price) VALUES (?, ?)",
            (item_name, price)
        )


def get_price_history(item_name: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT price, recorded_at FROM price_history "
            "WHERE item_name = ? ORDER BY recorded_at DESC LIMIT ?",
            (item_name, limit)
        ).fetchall()
        return [dict(r) for r in rows]


# ── referrals ──────────────────────────────────────────────────────────────────

def add_referral(referrer_id: int, referred_id: int) -> bool:
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
                (referrer_id, referred_id)
            )
            # Даём рефереру +3 бонусных слота в вотчлисте
            conn.execute("""
                INSERT INTO user_settings (user_id, bonus_watchlist)
                VALUES (?, 3)
                ON CONFLICT(user_id) DO UPDATE
                    SET bonus_watchlist = MIN(bonus_watchlist + 3,
                        ? - ?)
            """, (referrer_id, MAX_WATCHLIST_WITH_ADS, FREE_WATCHLIST_LIMIT))
            return True
        except sqlite3.IntegrityError:
            return False


def get_referral_count(referrer_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = ?",
            (referrer_id,)
        ).fetchone()
        return row["cnt"] if row else 0


# ── portfolio ──────────────────────────────────────────────────────────────────

def add_to_portfolio(user_id: int, item_name: str, buy_price: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO portfolio (user_id, item_name, buy_price) VALUES (?, ?, ?)",
            (user_id, item_name, buy_price)
        )


def get_portfolio(user_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def remove_from_portfolio(portfolio_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM portfolio WHERE id = ? AND user_id = ?",
            (portfolio_id, user_id)
        )
        return cur.rowcount > 0


# ── user_activity ──────────────────────────────────────────────────────────────

def touch_activity(user_id: int):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_activity (user_id, last_seen)
            VALUES (?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen
        """, (user_id,))


# ── daily_events ───────────────────────────────────────────────────────────────

def log_event(event_type: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_events (event_type) VALUES (?)", (event_type,)
        )


def get_event_count(event_type: str, since_hours: int = 24) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM daily_events "
            "WHERE event_type = ? AND created_at >= datetime('now', ?)",
            (event_type, f"-{since_hours} hours")
        ).fetchone()
        return row["cnt"] if row else 0


# ── price check rate-limit (weekly) ───────────────────────────────────────────

def get_price_checks_this_week(user_id: int) -> int:
    """Количество проверок цены за последние 7 дней."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM daily_events "
            "WHERE event_type = ? AND created_at >= datetime('now', '-7 days')",
            (f"pc_{user_id}",)
        ).fetchone()
        return row["cnt"] if row else 0


def log_user_price_check(user_id: int):
    log_event(f"pc_{user_id}")


# ── ad rewards ────────────────────────────────────────────────────────────────

def get_bonus_price_checks(user_id: int) -> int:
    settings = get_user_settings(user_id)
    return settings.get("bonus_price_checks", 0)


def add_bonus_price_checks(user_id: int, count: int = AD_REWARD_PRICE_CHECKS):
    """Добавляет бонусные проверки цены (за просмотр рекламы)."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, bonus_price_checks)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE
                SET bonus_price_checks = bonus_price_checks + ?
        """, (user_id, count, count))


def add_bonus_watchlist_slot(user_id: int) -> bool:
    """Добавляет +1 слот в вотчлист за рекламу. Возвращает False если достигнут лимит."""
    settings = get_user_settings(user_id)
    current_bonus = settings.get("bonus_watchlist", 0)
    max_bonus = MAX_WATCHLIST_WITH_ADS - FREE_WATCHLIST_LIMIT  # = 3
    if current_bonus >= max_bonus:
        return False
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, bonus_watchlist)
            VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE
                SET bonus_watchlist = MIN(bonus_watchlist + 1, ?)
        """, (user_id, max_bonus))
    return True


def get_bonus_watchlist(user_id: int) -> int:
    settings = get_user_settings(user_id)
    return settings.get("bonus_watchlist", 0)
