"""
Dota 2 Item Price Tracker Bot
Стек: python-telegram-bot 22.7, SQLite, APScheduler (через job_queue PTB)
"""

import logging
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, ADMIN_ID
from database import (
    init_db,
    get_user_settings,
    set_user_currency,
    add_to_watchlist,
    get_watchlist,
    remove_from_watchlist,
    touch_activity,
    log_event,
    add_referral,
)
from skin_checker import (
    search_items,
    get_item_price,
    format_price_message,
    check_watchlist,
    CURRENCIES,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Состояния ConversationHandler ──────────────────────────────────────────────

# Проверить цену
PRICE_ENTER_QUERY = 1
PRICE_SELECT_ITEM = 2

# Добавить в вотчлист
WATCH_ENTER_QUERY = 10
WATCH_SELECT_ITEM = 11
WATCH_ENTER_THRESHOLD = 12

# Настройки
SETTINGS_CHOOSE = 20

# ── Главная клавиатура ─────────────────────────────────────────────────────────

# Замени на свой URL после деплоя на Vercel
MINIAPP_URL = "https://YOUR_PROJECT.vercel.app"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔍 Проверить цену"],
        ["⭐ Мой вотчлист", "⚙️ Настройки"],
        ["👥 Пригласить"],
    ],
    resize_keyboard=True,
)

MINIAPP_BUTTON = InlineKeyboardMarkup([[
    InlineKeyboardButton("🎮 Открыть трекер цен", web_app=WebAppInfo(url=MINIAPP_URL))
]])


# ── /start ─────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    touch_activity(user.id)
    log_event("start")

    # Реферальная программа: /start ref_<referrer_id>
    args = context.args
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0][4:])
            if referrer_id != user.id:
                added = add_referral(referrer_id, user.id)
                if added:
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"👥 По твоей ссылке зарегистрировался новый пользователь!\n"
                             f"+3 сравнения Steam vs DMarket начислены.",
                    )
        except (ValueError, Exception):
            pass

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я слежу за ценами предметов <b>Dota 2</b> на Steam Market.\n\n"
        "Выбери что хочешь сделать:",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )
    await update.message.reply_text(
        "Или открой полный трекер с графиками и портфелем 👇",
        reply_markup=MINIAPP_BUTTON,
    )


# ── Блок: Проверить цену ───────────────────────────────────────────────────────

async def price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_activity(update.effective_user.id)
    await update.message.reply_text(
        "🔍 Введи название предмета (можно частичное, например: <code>dragonclaw</code>):",
        parse_mode="HTML",
    )
    return PRICE_ENTER_QUERY


async def price_enter_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("Пожалуйста, введи название предмета.")
        return PRICE_ENTER_QUERY

    await update.message.reply_text("🔎 Ищу на Steam Market...")

    results = search_items(query_text, count=5)

    if not results:
        steam_url = f"https://steamcommunity.com/market/search?appid=570&q={query_text}"
        await update.message.reply_text(
            f"😕 По запросу «{query_text}» ничего не найдено.\n\n"
            "Попробуй написать точнее или скопировать название из Steam Market.\n\n"
            f'🔗 <a href="{steam_url}">Поиск на Steam Market</a>',
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return PRICE_ENTER_QUERY

    # Показываем inline-кнопки с найденными предметами
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"price:{name}")]
        for name in results
    ]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="price:cancel")])

    await update.message.reply_text(
        f"Нашёл {len(results)} предмет(ов). Выбери нужный:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return PRICE_SELECT_ITEM


async def price_select_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # "price:<item_name>" или "price:cancel"
    if data == "price:cancel":
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    item_name = data[len("price:"):]
    user_id = query.from_user.id
    settings = get_user_settings(user_id)
    currency = settings.get("currency", "RUB")

    await query.edit_message_text(f"⏳ Получаю цену для «{item_name}»...")

    price_data = get_item_price(item_name, currency)
    if not price_data:
        await query.edit_message_text(
            f"⚠️ Не удалось получить цену для «{item_name}».\n"
            "Steam Market мог не ответить, попробуй позже."
        )
        return ConversationHandler.END

    log_event("price_check")
    msg = format_price_message(price_data)
    await query.edit_message_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    return ConversationHandler.END


async def price_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ── Блок: Вотчлист ────────────────────────────────────────────────────────────

async def watchlist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    touch_activity(user_id)
    items = get_watchlist(user_id)

    if not items:
        text = "⭐ Твой вотчлист пуст.\n\nДобавь предметы чтобы получать уведомления об изменении цены."
    else:
        settings = get_user_settings(user_id)
        sym = CURRENCIES.get(settings.get("currency", "RUB"), {}).get("symbol", "руб.")
        lines = ["⭐ <b>Мой вотчлист:</b>\n"]
        for item in items:
            lines.append(
                f"• {item['item_name']}\n"
                f"  Порог: {item['threshold']:,.2f} {sym}"
            )
        text = "\n".join(lines)

    buttons = [
        [InlineKeyboardButton("➕ Добавить предмет", callback_data="watch_add")],
    ]
    if items:
        buttons.append(
            [InlineKeyboardButton("🗑 Удалить предмет", callback_data="watch_delete_menu")]
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def watchlist_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔍 Введи название предмета для добавления в вотчлист\n"
        "(можно частичное название):"
    )
    return WATCH_ENTER_QUERY


async def watch_enter_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("Пожалуйста, введи название.")
        return WATCH_ENTER_QUERY

    await update.message.reply_text("🔎 Ищу на Steam Market...")
    results = search_items(query_text, count=5)

    if not results:
        steam_url = f"https://steamcommunity.com/market/search?appid=570&q={query_text}"
        await update.message.reply_text(
            f"😕 По запросу «{query_text}» ничего не найдено.\n\n"
            f'🔗 <a href="{steam_url}">Поиск на Steam Market</a>',
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return WATCH_ENTER_QUERY

    buttons = [
        [InlineKeyboardButton(name, callback_data=f"watch_item:{name}")]
        for name in results
    ]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="watch_item:cancel")])

    await update.message.reply_text(
        f"Нашёл {len(results)} предмет(ов). Выбери нужный:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return WATCH_SELECT_ITEM


async def watch_select_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "watch_item:cancel":
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    item_name = data[len("watch_item:"):]
    context.user_data["watch_item_name"] = item_name

    user_id = query.from_user.id
    settings = get_user_settings(user_id)
    sym = CURRENCIES.get(settings.get("currency", "RUB"), {}).get("symbol", "руб.")

    await query.edit_message_text(
        f"✅ Выбран: <b>{item_name}</b>\n\n"
        f"💰 Введи пороговую цену ({sym}) — я уведомлю тебя когда цена упадёт до этого значения.\n\n"
        "Введи <code>0</code> чтобы отслеживать без порога (только добавить в список):",
        parse_mode="HTML",
    )
    return WATCH_ENTER_THRESHOLD


async def watch_enter_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        threshold = float(text)
        if threshold < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Введи число, например: <code>500</code> или <code>12.50</code>",
            parse_mode="HTML",
        )
        return WATCH_ENTER_THRESHOLD

    user_id = update.effective_user.id
    item_name = context.user_data.get("watch_item_name", "")

    if not item_name:
        await update.message.reply_text("Что-то пошло не так, попробуй снова.")
        return ConversationHandler.END

    added = add_to_watchlist(user_id, item_name, threshold)
    if added:
        settings = get_user_settings(user_id)
        sym = CURRENCIES.get(settings.get("currency", "RUB"), {}).get("symbol", "руб.")
        threshold_text = (
            f"порог: {threshold:,.2f} {sym}"
            if threshold > 0
            else "без порога уведомлений"
        )
        await update.message.reply_text(
            f"✅ <b>{item_name}</b> добавлен в вотчлист\n({threshold_text})",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        log_event("watchlist_add")
    else:
        await update.message.reply_text(
            f"⚠️ «{item_name}» уже есть в твоём вотчлисте.",
            reply_markup=MAIN_KEYBOARD,
        )
    return ConversationHandler.END


async def watchlist_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    items = get_watchlist(user_id)

    if not items:
        await query.edit_message_text("Вотчлист пуст.")
        return

    buttons = [
        [InlineKeyboardButton(
            f"🗑 {item['item_name']}",
            callback_data=f"watch_del:{item['id']}"
        )]
        for item in items
    ]
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="watch_del:cancel")])

    await query.edit_message_text(
        "Выбери предмет для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def watchlist_delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "watch_del:cancel":
        await query.edit_message_text("Отменено.")
        return

    try:
        item_id = int(data[len("watch_del:"):])
    except ValueError:
        await query.edit_message_text("Ошибка.")
        return

    user_id = query.from_user.id
    removed = remove_from_watchlist(item_id, user_id)
    if removed:
        await query.edit_message_text("✅ Предмет удалён из вотчлиста.")
    else:
        await query.edit_message_text("⚠️ Предмет не найден.")


# ── Блок: Настройки ───────────────────────────────────────────────────────────

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    touch_activity(user_id)
    settings = get_user_settings(user_id)
    current = settings.get("currency", "RUB")
    cur_info = CURRENCIES.get(current, {})

    buttons = []
    for code, info in CURRENCIES.items():
        label = f"{'✅ ' if code == current else ''}{info['name']} ({info['symbol']})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"currency:{code}")])

    await update.message.reply_text(
        f"⚙️ <b>Настройки</b>\n\n"
        f"Текущая валюта: <b>{cur_info.get('name', current)} ({cur_info.get('symbol', '')})</b>\n\n"
        "Выбери валюту для отображения цен:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def settings_set_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    currency = query.data[len("currency:"):]
    if currency not in CURRENCIES:
        await query.edit_message_text("Неизвестная валюта.")
        return

    user_id = query.from_user.id
    set_user_currency(user_id, currency)
    info = CURRENCIES[currency]

    await query.edit_message_text(
        f"✅ Валюта изменена на <b>{info['name']} ({info['symbol']})</b>",
        parse_mode="HTML",
    )


# ── Блок: Пригласить ──────────────────────────────────────────────────────────

async def share_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    touch_activity(user_id)

    bot_username = context.bot.username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    from database import get_referral_count
    count = get_referral_count(user_id)

    await update.message.reply_text(
        f"👥 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей и получай <b>+3 сравнения</b> Steam vs DMarket за каждого!\n\n"
        f"Твоя ссылка:\n<code>{ref_link}</code>\n\n"
        f"Приглашено друзей: <b>{count}</b>",
        parse_mode="HTML",
    )


# ── Общий обработчик неизвестных сообщений ────────────────────────────────────

async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выбери действие из меню 👇",
        reply_markup=MAIN_KEYBOARD,
    )


# ── Сборка и запуск ───────────────────────────────────────────────────────────

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler: Проверить цену
    price_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔍 Проверить цену$"), price_start),
            CommandHandler("price", price_start),
        ],
        states={
            PRICE_ENTER_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, price_enter_query)
            ],
            PRICE_SELECT_ITEM: [
                CallbackQueryHandler(price_select_item, pattern=r"^price:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", price_cancel),
            MessageHandler(filters.Regex("^❌"), price_cancel),
        ],
        per_message=False,
    )

    # ConversationHandler: Добавить в вотчлист (запускается из inline-кнопки)
    watch_add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(watchlist_add_start, pattern="^watch_add$"),
        ],
        states={
            WATCH_ENTER_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, watch_enter_query)
            ],
            WATCH_SELECT_ITEM: [
                CallbackQueryHandler(watch_select_item, pattern=r"^watch_item:"),
            ],
            WATCH_ENTER_THRESHOLD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, watch_enter_threshold)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", price_cancel),
        ],
        per_message=False,
    )

    # Регистрируем handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", watchlist_menu_cmd))
    app.add_handler(price_conv)
    app.add_handler(watch_add_conv)

    # Вотчлист — просмотр и удаление
    app.add_handler(MessageHandler(filters.Regex("^⭐ Мой вотчлист$"), watchlist_menu))
    app.add_handler(
        CallbackQueryHandler(watchlist_delete_menu, pattern="^watch_delete_menu$")
    )
    app.add_handler(
        CallbackQueryHandler(watchlist_delete_item, pattern=r"^watch_del:")
    )

    # Настройки
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Настройки$"), settings_menu))
    app.add_handler(
        CallbackQueryHandler(settings_set_currency, pattern=r"^currency:")
    )

    # Пригласить
    app.add_handler(MessageHandler(filters.Regex("^👥 Пригласить$"), share_referral))
    app.add_handler(CommandHandler("share", share_referral))

    # Fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    # Фоновая проверка вотчлиста каждые 30 минут
    job_queue = app.job_queue
    job_queue.run_repeating(check_watchlist, interval=1800, first=60)

    logger.info("Бот запущен.")
    app.run_polling(drop_pending_updates=True)


# Алиас для команды /list
async def watchlist_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await watchlist_menu(update, context)


if __name__ == "__main__":
    main()
