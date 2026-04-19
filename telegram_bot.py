import asyncio
import logging
import os
import re
import sys

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

import monitor
from checkers import get_checker, all_checkers, StockResult
from checkers.smarticket import SmartTicketEvent
from checkers.flights import FlightDeparture
from config import TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS
from url_parser import parse_product_url
import claude_integration

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("start", "Welcome message"),
    BotCommand("help", "Show available commands"),
    BotCommand("stock", "Check current stock status"),
    BotCommand("stock_toggle", "Pause or resume monitoring"),
    BotCommand("filter_toggle", "Toggle toddler age filter for events"),
    BotCommand("filters", "Adjust filter settings (age, sources, terms)"),
    BotCommand("flights", "Flight monitor settings (TLV departures)"),
    BotCommand("flights_toggle", "Pause/resume flight monitoring"),
    BotCommand("subscribe", "Subscribe to a product URL"),
    BotCommand("unsubscribe", "Remove a monitored item"),
    BotCommand("list", "Show all monitored items"),
    BotCommand("approve", "Approve Claude's pending plan"),
    BotCommand("reject", "Reject Claude's pending plan"),
    BotCommand("restart", "Restart the bot"),
    BotCommand("claude", "Send a prompt to Claude Code"),
]

# Claude integration state
_claude_busy = False
_claude_pending_prompt: str | None = None
_claude_pending_url: str | None = None

_AIRLINE_DISPLAY = {
    "LY": "El Al", "IZ": "Arkia", "6H": "Israir",
    "5C": "CAL", "7L": "Silk Way", "U8": "TUS Airways",
    "W6": "Wizz Air", "FR": "Ryanair", "U2": "EasyJet",
    "PC": "Pegasus", "5F": "Fly One", "E2": "Eurowings",
    "3F": "FlyOne Armenia", "BZ": "Blue Bird", "FP": "FlyPop",
    "WZ": "Red Wings", "HH": "FlyHiSky", "A9": "Georgian Airways",
    "OE": "Overland Airways", "RD": "Rotana Jet", "HU": "Hainan Airlines",
}


def _esc(text: str) -> str:
    """Escape Telegram Markdown special characters."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _is_allowed(update: Update) -> bool:
    """Check if the user is in the allowlist."""
    return update.effective_chat.id in ALLOWED_CHAT_IDS


async def broadcast(app: Application, text: str, **kwargs):
    """Send a message to all registered users."""
    for chat_id in monitor.get_registered_users():
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except Exception:
            log.exception("Failed to send to chat_id %s", chat_id)


# ── Alert senders (called by scheduler) ──────────────────────────────

async def send_alert(app: Application, item: dict, result: StockResult):
    price_str = f"{result.price} ₪" if result.price else "N/A"
    text = (
        f"🟢 *In Stock!*\n\n"
        f"*{_esc(result.name or item['item_id'])}*\n"
        f"Price: {price_str}\n"
        f"Source: {item['source'].upper()}\n\n"
        f"[Buy now]({result.url})"
    )
    await broadcast(app, text, parse_mode="Markdown", disable_web_page_preview=True)


async def send_smarticket_alert(app: Application, events: list[SmartTicketEvent], source: str = "Smarticket"):
    lines = [f"🎟 *New {source} events available!*\n"]
    for e in events:
        date_str = ""
        if e.date:
            from datetime import date as _date
            d = _date.fromisoformat(e.date)
            date_str = f"📅 {d.strftime('%a %d/%m')}  "
        lines.append(
            f"• *{_esc(e.name)}*\n"
            f"  {date_str}🕐 {e.time}  📍 {_esc(e.venue)}\n"
            f"  [Order tickets]({e.url})"
        )
    await broadcast(
        app, "\n\n".join(lines),
        parse_mode="Markdown", disable_web_page_preview=True,
    )


async def send_flight_alert(app: Application, flights: list[FlightDeparture]):
    from datetime import datetime
    lines = ["✈️  *New TLV departure detected!*\n"]
    for f in flights:
        dep_str = datetime.fromtimestamp(f.scheduled_ts).strftime("%a %d/%m  %H:%M")
        lines.append(
            f"• *{_esc(f.flight_number)} — {_esc(f.airline_name)}*\n"
            f"  📅 {dep_str}  ✈️  TLV → {_esc(f.destination_city)} ({f.destination_iata})\n"
            f"  Status: {_esc(f.status)}\n"
            f"  [FR24]({f.fr24_url}) · [{_esc(f.airline_name)}]({f.airline_url}) · [Book (Kayak)]({f.booking_url()})"
        )
    await broadcast(
        app, "\n\n".join(lines),
        parse_mode="Markdown", disable_web_page_preview=True,
    )


async def send_daily_summary(app: Application):
    state = monitor.load_state()

    lines = ["📋 *Daily Summary*\n"]

    if state["items"]:
        for item in state["items"]:
            checker = get_checker(item["source"])
            if not checker:
                continue
            try:
                result = checker.check(item["item_id"])
                status = "🟢 In stock" if result.in_stock else "🔴 Out of stock"
                price_str = f" — {result.price} ₪" if result.price else ""
                name = result.name or item["item_id"]
                lines.append(f"• *{name}*{price_str}\n  {status}")
            except Exception:
                lines.append(f"• {item['source']}/{item['item_id']} — ❌ error")

    paused = state.get("paused", False)
    lines.append(f"\nMonitoring: {'⏸ Paused' if paused else '▶️ Active'}")

    flights_enabled = monitor.is_flights_enabled()
    flight_settings = monitor.get_flight_filter_settings()
    airlines = flight_settings.get("airlines", {})
    airlines_str = ", ".join(k for k, v in airlines.items() if v) if airlines else "All"
    min_h = flight_settings.get("min_hour", 0)
    max_h = flight_settings.get("max_hour", 23)
    lines.append(
        f"\n✈️  Flights: {'✅' if flights_enabled else '⏸'} {airlines_str} from TLV ({min_h:02d}:00-{max_h:02d}:59)"
    )

    await broadcast(app, "\n".join(lines), parse_mode="Markdown")


# ── Command handlers ─────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        chat_id = update.effective_chat.id
        log.info("Unauthorized /start from chat_id=%s", chat_id)
        await update.message.reply_text(
            f"⛔ Not authorized.\n\nYour chat ID: `{chat_id}`\n"
            "Share this with the bot admin to get access.",
            parse_mode="Markdown",
        )
        return

    chat_id = update.effective_chat.id
    is_new = monitor.register_user(chat_id)

    text = (
        "👋 *Welcome to HaBot!*\n\n"
        "I monitor product availability and community events, "
        "alerting you the moment something new appears.\n\n"
    )

    # Event sources status
    settings = monitor.get_filter_settings()
    toddler_on = monitor.is_toddler_filter_on()
    text += "🎟 *Event sources:*\n"
    for src, enabled in settings["sources"].items():
        icon = "✅" if enabled else "❌"
        text += f"• {icon} {src.capitalize()}\n"
    if toddler_on:
        text += f"• 🧒 Toddler filter ON (max age: {settings['max_age']})\n"

    # Flight monitor status
    flights_enabled = monitor.is_flights_enabled()
    flight_settings = monitor.get_flight_filter_settings()
    airlines_on = [k for k, v in flight_settings["airlines"].items() if v]
    text += "\n✈️  *Flight monitoring (TLV):*\n"
    icon = "✅" if flights_enabled else "⏸"
    text += f"• {icon} {'Active' if flights_enabled else 'Paused'}\n"
    text += f"• Airlines: {', '.join(airlines_on) if airlines_on else 'All'}\n"
    text += "• Use /flights to configure\n"

    # Stock items
    state = monitor.load_state()
    text += "\n📡 *Stock monitoring:*\n"
    if state["items"]:
        for item in state["items"]:
            checker = get_checker(item["source"])
            if checker:
                try:
                    result = checker.check(item["item_id"])
                    name = result.name or item["item_id"]
                    text += f"• {name} ({item['source'].upper()})\n"
                except Exception:
                    text += f"• {item['item_id']} ({item['source'].upper()})\n"
    else:
        text += "• No items yet\n"

    text += (
        "\n🔔 *How it works:*\n"
        "• Events checked every 5 minutes (Smarticket + Kehilatayim)\n"
        "• Stock checked every 5 minutes\n"
        "• Alerts on new available events & stock transitions\n"
        "• Daily summary at 19:00\n\n"
        "Use /filters to adjust age & source settings.\n"
        "Use /help to see all commands."
    )
    if is_new:
        text += f"\n\n✅ Registered for notifications (chat ID: `{chat_id}`)"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *HaBot Commands*\n\n"
        "*Events & Stock*\n"
        "/stock — Live status (Smarticket + Kehilatayim + stock)\n"
        "/stock\\_toggle — Pause/resume monitoring\n"
        "/filters — Adjust age, sources & include terms\n"
        "/filter\\_toggle — Quick toggle toddler filter on/off\n"
        "/flights — Flight monitor settings (TLV departures)\n"
        "/flights\\_toggle — Pause/resume flight monitoring\n\n"
        "*Subscriptions*\n"
        "/subscribe `<url>` — Monitor a product URL\n"
        "/unsubscribe — Remove a monitored item\n"
        "/list — Show all items with actions\n\n"
        "*Claude & System*\n"
        "/claude `<text>` — Ask Claude Code anything\n"
        "/approve — Approve Claude's pending plan\n"
        "/reject — Reject Claude's pending plan\n"
        "/restart — Restart the bot\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = monitor.load_state()
    if not state["items"]:
        await update.message.reply_text("No items being monitored. 🤷")
        return

    paused = state.get("paused", False)
    lines = [f"📊 *Stock Status* {'⏸ PAUSED' if paused else '▶️ Active'}\n"]
    for item in state["items"]:
        checker = get_checker(item["source"])
        if not checker:
            lines.append(f"• {item['source']}/{item['item_id']} — ⚠️ no checker")
            continue
        try:
            result = checker.check(item["item_id"])
            status = "🟢 In stock" if result.in_stock else "🔴 Out of stock"
            price_str = f" — {result.price} ₪" if result.price else ""
            name = result.name or item["item_id"]
            lines.append(f"• *{_esc(name)}*{price_str}\n  {status} ({item['source'].upper()})")
        except Exception as e:
            lines.append(f"• {item['source']}/{item['item_id']} — ❌ error")

    # Send stock status first
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # Event sources — display both Smarticket and Kehilatayim
    from checkers.smarticket import fetch_events_range, is_relevant_for_toddler
    from checkers.kehilatayim import fetch_events as fetch_kehilatayim_events
    from datetime import date as _date

    toddler_on = monitor.is_toddler_filter_on()

    async def _send_events_section(source_name: str, raw_events: list, filter_fn):
        all_events = [e for e in raw_events if filter_fn(e)] if toddler_on else raw_events
        by_date: dict[str, list] = {}
        for e in all_events:
            by_date.setdefault(e.date, []).append(e)

        if not all_events:
            await update.message.reply_text(
                f"🎟 *{source_name}*\nNo events found", parse_mode="Markdown"
            )
            return

        msg_lines = [f"🎟 *{source_name}*"]
        for event_date in sorted(by_date.keys()):
            d = _date.fromisoformat(event_date)
            day_label = d.strftime("%a %d/%m")
            day_events = by_date[event_date]
            available = [e for e in day_events if e.available]
            sold_out_count = len(day_events) - len(available)

            day_lines = [f"\n*{day_label}* ({len(available)} available, {sold_out_count} sold out)"]
            for e in available:
                day_lines.append(f"• 🟢 {e.time} — *{_esc(e.name)}*")
            if not available:
                day_lines.append("  All sold out")

            candidate = "\n".join(msg_lines + day_lines)
            if len(candidate) > 3800 and len(msg_lines) > 1:
                await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")
                msg_lines = [f"🎟 *{source_name} (cont.)*"]

            msg_lines.extend(day_lines)

        if msg_lines:
            await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")

    # Smarticket
    if monitor.is_source_enabled("smarticket"):
        try:
            smarticket_events = fetch_events_range()
            await _send_events_section(
                "Smarticket — next 7 days", smarticket_events,
                lambda e: is_relevant_for_toddler(e.name),
            )
        except Exception:
            await update.message.reply_text("🎟 ❌ Error fetching Smarticket events")

    # Kehilatayim
    if monitor.is_source_enabled("kehilatayim"):
        try:
            kehilatayim_events = fetch_kehilatayim_events()
            await _send_events_section(
                "Kehilatayim (Givatayim)", kehilatayim_events,
                lambda e: is_relevant_for_toddler(e.name),
            )
        except Exception:
            await update.message.reply_text("🎟 ❌ Error fetching Kehilatayim events")


async def cmd_stock_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    paused = monitor.toggle_pause()
    status = "⏸ Monitoring paused" if paused else "▶️ Monitoring resumed"
    await update.message.reply_text(status)


async def cmd_filter_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled = monitor.toggle_toddler_filter()
    if enabled:
        await update.message.reply_text("🧒 Toddler filter ON — showing only events for ages ~1.5–3")
    else:
        await update.message.reply_text("🔓 Toddler filter OFF — showing all events")


def _build_filters_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    """Build the filters display text and inline keyboard."""
    settings = monitor.get_filter_settings()
    toddler_on = monitor.is_toddler_filter_on()

    lines = ["⚙️ *Filter Settings*\n"]

    # Main toggle
    lines.append(f"🧒 Toddler filter: *{'ON' if toddler_on else 'OFF'}*")
    lines.append(f"📏 Max age: *{settings['max_age']}*\n")

    # Sources
    lines.append("*Event sources:*")
    for src, enabled in settings["sources"].items():
        icon = "✅" if enabled else "❌"
        lines.append(f"  {icon} {src.capitalize()}")

    # Include terms
    lines.append("\n*Include terms:*")
    for term, enabled in settings["include_terms"].items():
        icon = "✅" if enabled else "❌"
        lines.append(f"  {icon} {term}")

    # Build keyboard
    buttons = []

    # Toddler filter toggle
    btn_label = "🧒 Filter: OFF →" if toddler_on else "🧒 Filter: ON →"
    buttons.append([InlineKeyboardButton(btn_label, callback_data="filter:toggle")])

    # Max age buttons
    age_row = []
    for age in [2.0, 2.5, 3.0, 3.5]:
        label = f"{'✓ ' if settings['max_age'] == age else ''}{age}y"
        age_row.append(InlineKeyboardButton(label, callback_data=f"filter:age:{age}"))
    buttons.append(age_row)

    # Source toggles
    src_row = []
    for src, enabled in settings["sources"].items():
        icon = "✅" if enabled else "❌"
        src_row.append(InlineKeyboardButton(
            f"{icon} {src.capitalize()}", callback_data=f"filter:source:{src}"
        ))
    buttons.append(src_row)

    # Include term toggles
    for term, enabled in settings["include_terms"].items():
        icon = "✅" if enabled else "❌"
        buttons.append([InlineKeyboardButton(
            f"{icon} {term}", callback_data=f"filter:term:{term}"
        )])

    buttons.append([InlineKeyboardButton("✅ Done", callback_data="filter:done")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_flights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = _build_flights_keyboard()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def cmd_flights_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled = monitor.toggle_flights()
    status = "✅ Flight monitoring resumed" if enabled else "⏸ Flight monitoring paused"
    await update.message.reply_text(status)


def _build_flights_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    """Build the /flights settings display and inline keyboard."""
    from config import FLIGHT_CHECK_INTERVAL
    enabled = monitor.is_flights_enabled()
    settings = monitor.get_flight_filter_settings()

    lines = ["✈️  *Flight Monitor (TLV Departures)*\n"]
    lines.append(f"Status: {'✅ Active' if enabled else '⏸ Paused'}")
    lines.append(f"Polling: every {FLIGHT_CHECK_INTERVAL}s\n")

    airlines = settings.get("airlines", {})
    if airlines:
        lines.append("*Airlines:*")
        for iata, on in airlines.items():
            name = _AIRLINE_DISPLAY.get(iata, iata)
            icon = "✅" if on else "❌"
            lines.append(f"  {icon} {iata} ({name})")
    else:
        lines.append("*Airlines:* All")

    dests = settings.get("destinations", {})
    if dests:
        lines.append("\n*Destinations:*")
        for dest, on in dests.items():
            icon = "✅" if on else "❌"
            lines.append(f"  {icon} {dest}")
    else:
        lines.append("*Destinations:* All")

    min_h = settings.get("min_hour", 0)
    max_h = settings.get("max_hour", 23)
    lines.append(f"*Hours:* {min_h:02d}:00 - {max_h:02d}:59")

    buttons = []

    toggle_label = "⏸ Pause monitoring" if enabled else "▶️  Resume monitoring"
    buttons.append([InlineKeyboardButton(toggle_label, callback_data="flights:toggle")])

    # Dynamic airline toggles from recently seen flights
    state = monitor.load_state()
    seen_iatas = set()
    for key in state.get("flights", {}).get("known_flight_ids", {}):
        parts = key.split("_")
        if parts:
            seen_iatas.add(parts[0])
    for iata in airlines:
        seen_iatas.add(iata)

    if seen_iatas:
        airline_row = []
        for iata in sorted(seen_iatas):
            excluded = airlines.get(iata) is False
            icon = "❌" if excluded else "✅"
            airline_row.append(
                InlineKeyboardButton(f"{icon} {iata}", callback_data=f"flights:airline:{iata}")
            )
            if len(airline_row) == 4:
                buttons.append(airline_row)
                airline_row = []
        if airline_row:
            buttons.append(airline_row)

    # Hour range presets
    hour_presets = [(1, 6, "01-06"), (6, 12, "06-12"), (12, 18, "12-18"), (22, 6, "22-06"), (0, 23, "All")]
    hour_row = []
    for lo, hi, label in hour_presets:
        active = min_h == lo and max_h == hi
        prefix = "-> " if active else ""
        hour_row.append(
            InlineKeyboardButton(f"{prefix}{label}", callback_data=f"flights:hours:{lo}:{hi}")
        )
    buttons.append(hour_row)

    buttons.append([InlineKeyboardButton("✅ Done", callback_data="flights:done")])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = _build_filters_keyboard()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Subscription management ──────────────────────────────────────────

async def _subscribe_url(update: Update, url: str):
    """Handle subscription for a URL — known or unknown source."""
    global _claude_busy, _claude_pending_prompt, _claude_pending_url

    parsed = parse_product_url(url)
    if parsed:
        source, item_id = parsed
        _, is_new = monitor.add_item(source, item_id)

        # Try to get current status
        checker = get_checker(source)
        name = item_id
        status_line = ""
        if checker:
            try:
                result = checker.check(item_id)
                name = result.name or item_id
                status = "🟢 In stock" if result.in_stock else "🔴 Out of stock"
                price_str = f" — {result.price} ₪" if result.price else ""
                status_line = f"\n{status}{price_str}"
            except Exception:
                pass

        if is_new:
            await update.message.reply_text(
                f"✅ Subscribed to *{name}* ({source.upper()})\n"
                f"Item ID: `{item_id}`{status_line}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"Already monitoring *{name}* ({source.upper()}){status_line}\n\n"
                f"Use /list to manage your subscriptions.",
                parse_mode="Markdown",
            )
        return

    # Unknown source — trigger Claude analysis
    if _claude_busy:
        await update.message.reply_text("⏳ Claude is already working on something. Please wait.")
        return

    _claude_busy = True
    _claude_pending_url = url
    await update.message.reply_text(
        f"🔍 Unknown source. Asking Claude to analyze...\n`{url}`",
        parse_mode="Markdown",
    )

    try:
        prompt = (
            f"Analyze this product/availability URL and propose a checker for it:\n"
            f"{url}\n\n"
            f"1. Visit the URL and understand what product/service it tracks\n"
            f"2. Figure out how to check availability (API, scraping, etc.)\n"
            f"3. Propose a plan for creating a checker in checkers/{{source}}.py\n"
            f"4. Include: source_name, how to parse the item_id from the URL, "
            f"how to check availability, what data to extract\n\n"
            f"DO NOT create any files yet. Just propose the plan."
        )
        response = await asyncio.to_thread(
            claude_integration.run_claude, prompt, allow_edits=False
        )
        _claude_pending_prompt = (
            f"Execute the plan to create a checker for {url}.\n\n"
            f"Create the checker file in checkers/ following the Checker ABC pattern. "
            f"Also update url_parser.py with the URL pattern for this source. "
            f"Register the checker at module level.\n\n"
            f"Previous analysis:\n{response}"
        )

        # Truncate response for Telegram (4096 char limit)
        display = response[:3500] if len(response) > 3500 else response
        await update.message.reply_text(
            f"🧠 *Claude's Analysis:*\n\n{display}\n\n"
            f"👆 Use /approve to execute or /reject to cancel.",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.exception("Claude analysis failed")
        await update.message.reply_text(f"❌ Claude analysis failed: {e}")
        _claude_pending_prompt = None
        _claude_pending_url = None
    finally:
        _claude_busy = False


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /subscribe `<url>`\n"
            "Example: /subscribe https://ksp.co.il/web/item/12345",
            parse_mode="Markdown",
        )
        return

    url = context.args[0]
    await _subscribe_url(update, url)


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = monitor.get_items()
    if not items:
        await update.message.reply_text("No items to unsubscribe from. 🤷")
        return

    buttons = []
    for item in items:
        label = f"{item['source'].upper()}: {item['item_id']}"
        # Try to get name
        checker = get_checker(item["source"])
        if checker:
            try:
                result = checker.check(item["item_id"])
                if result.name:
                    label = f"{result.name} ({item['source'].upper()})"
            except Exception:
                pass
        callback = f"unsub:{item['source']}:{item['item_id']}"
        buttons.append([InlineKeyboardButton(f"🗑 {label}", callback_data=callback)])

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="unsub:cancel")])

    await update.message.reply_text(
        "Select an item to unsubscribe:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = monitor.get_items()
    if not items:
        await update.message.reply_text("No items being monitored. Use /subscribe to add one.")
        return

    lines = ["📊 *Monitored Items*\n"]
    buttons = []

    for item in items:
        checker = get_checker(item["source"])
        name = item["item_id"]
        status_line = ""

        if checker:
            try:
                result = checker.check(item["item_id"])
                name = result.name or item["item_id"]
                status = "🟢 In stock" if result.in_stock else "🔴 Out of stock"
                price_str = f" — {result.price} ₪" if result.price else ""
                status_line = f"\n  {status}{price_str} ({item['source'].upper()})"
            except Exception:
                status_line = f"\n  ⚠️ Check failed ({item['source'].upper()})"
        else:
            status_line = f"\n  ⚠️ No checker ({item['source'].upper()})"

        lines.append(f"• *{name}*{status_line}")

        row = [
            InlineKeyboardButton("🔍 Check", callback_data=f"check:{item['source']}:{item['item_id']}"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"unsub:{item['source']}:{item['item_id']}"),
        ]
        buttons.append(row)

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


# ── Claude commands ──────────────────────────────────────────────────

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _claude_busy, _claude_pending_prompt, _claude_pending_url

    if not _claude_pending_prompt:
        await update.message.reply_text("No pending plan to approve.")
        return

    if _claude_busy:
        await update.message.reply_text("⏳ Claude is already working. Please wait.")
        return

    _claude_busy = True
    await update.message.reply_text("✅ Approved! Claude is implementing the checker...")

    try:
        response = await asyncio.to_thread(
            claude_integration.run_claude, _claude_pending_prompt, allow_edits=True
        )
        display = response[:3500] if len(response) > 3500 else response
        await update.message.reply_text(
            f"🧠 *Done!*\n\n{display}\n\n"
            f"🔄 Restarting to pick up new checker...",
            parse_mode="Markdown",
        )
        _claude_pending_prompt = None
        _claude_pending_url = None
        # Restart to pick up new checker
        _do_restart()
    except Exception as e:
        log.exception("Claude edit failed")
        await update.message.reply_text(f"❌ Claude edit failed: {e}")
    finally:
        _claude_busy = False


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _claude_pending_prompt, _claude_pending_url

    if not _claude_pending_prompt:
        await update.message.reply_text("No pending plan to reject.")
        return

    _claude_pending_prompt = None
    _claude_pending_url = None
    await update.message.reply_text("❌ Plan rejected.")


async def cmd_claude(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _claude_busy

    if not context.args:
        await update.message.reply_text(
            "Usage: /claude `<your question>`",
            parse_mode="Markdown",
        )
        return

    if _claude_busy:
        await update.message.reply_text("⏳ Claude is already working. Please wait.")
        return

    _claude_busy = True
    prompt = " ".join(context.args)
    await update.message.reply_text("🧠 Thinking...")

    try:
        ctx = _build_context()
        enriched_prompt = f"{ctx}\n\nUser message: {prompt}"
        response = await asyncio.to_thread(
            claude_integration.run_claude, enriched_prompt, allow_edits=False
        )
        display = response[:3500] if len(response) > 3500 else response
        await update.message.reply_text(display or "No response from Claude.")
    except Exception as e:
        log.exception("Claude request failed")
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        _claude_busy = False


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Restarting...")
    _do_restart()


def _do_restart():
    """Restart the bot process by replacing it with a fresh one."""
    log.info("Restarting HaBot...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def _handle_filter_callback(query, data: str):
    """Handle filter settings inline keyboard callbacks."""
    if data == "filter:toggle":
        monitor.toggle_toddler_filter()
    elif data == "filter:done":
        await query.edit_message_text("✅ Filter settings saved.")
        return
    elif data.startswith("filter:age:"):
        age = data.split(":", 2)[2]
        monitor.update_filter_setting("max_age", float(age))
    elif data.startswith("filter:source:"):
        source = data.split(":", 2)[2]
        settings = monitor.get_filter_settings()
        current = settings["sources"].get(source, True)
        monitor.update_filter_setting(f"source:{source}", not current)
    elif data.startswith("filter:term:"):
        term = data.split(":", 2)[2]
        settings = monitor.get_filter_settings()
        current = settings["include_terms"].get(term, True)
        monitor.update_filter_setting(f"term:{term}", not current)

    # Refresh the message with updated settings
    text, keyboard = _build_filters_keyboard()
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def _handle_flights_callback(query, data: str):
    """Handle /flights inline keyboard callbacks."""
    if data == "flights:toggle":
        monitor.toggle_flights()
    elif data == "flights:done":
        await query.edit_message_text("✅ Flight settings saved.")
        return
    elif data.startswith("flights:airline:"):
        iata = data.split(":", 2)[2]
        settings = monitor.get_flight_filter_settings()
        current = settings["airlines"].get(iata, True)
        monitor.update_flight_filter_setting(f"airline:{iata}", not current)
    elif data.startswith("flights:dest:"):
        dest = data.split(":", 2)[2]
        settings = monitor.get_flight_filter_settings()
        current = settings.get("destinations", {}).get(dest, True)
        monitor.update_flight_filter_setting(f"dest:{dest}", not current)
    elif data.startswith("flights:hours:"):
        parts = data.split(":")
        monitor.update_flight_filter_setting("min_hour", int(parts[2]))
        monitor.update_flight_filter_setting("max_hour", int(parts[3]))

    text, keyboard = _build_flights_keyboard()
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Inline keyboard callback handler ─────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Flight settings callbacks ──
    if data.startswith("flights:"):
        await _handle_flights_callback(query, data)
        return

    # ── Filter settings callbacks ──
    if data.startswith("filter:"):
        await _handle_filter_callback(query, data)
        return

    if data == "unsub:cancel":
        await query.edit_message_text("Cancelled.")
        return

    if data.startswith("unsub:"):
        _, source, item_id = data.split(":", 2)
        removed = monitor.remove_item(source, item_id)
        if removed:
            await query.edit_message_text(f"✅ Removed {source.upper()} item `{item_id}`.", parse_mode="Markdown")
        else:
            await query.edit_message_text("Item not found.")
        return

    if data.startswith("check:"):
        _, source, item_id = data.split(":", 2)
        checker = get_checker(source)
        if not checker:
            await query.edit_message_text(f"⚠️ No checker for {source}")
            return
        try:
            result = checker.check(item_id)
            status = "🟢 In stock" if result.in_stock else "🔴 Out of stock"
            price_str = f" — {result.price} ₪" if result.price else ""
            name = result.name or item_id
            await query.edit_message_text(
                f"*{name}*{price_str}\n{status} ({source.upper()})",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Check failed: {e}")


# ── Free-text message handler ────────────────────────────────────────

_URL_PATTERN = re.compile(r"https?://\S+")


def _build_context() -> str:
    """Build a context string about current monitoring state for Claude."""
    items = monitor.get_items()
    paused = monitor.is_paused()

    lines = [
        "You are HaBot, a Telegram bot that monitors product availability.",
        f"Monitoring is currently {'PAUSED' if paused else 'active'}.",
        f"Currently monitoring {len(items)} item(s):",
    ]
    for item in items:
        checker = get_checker(item["source"])
        name = item["item_id"]
        if checker:
            try:
                result = checker.check(item["item_id"])
                name = result.name or item["item_id"]
                status = "in stock" if result.in_stock else "out of stock"
                price = f", {result.price} ₪" if result.price else ""
                lines.append(f"  - {name} (source: {item['source']}, id: {item['item_id']}, {status}{price})")
                continue
            except Exception:
                pass
        lines.append(f"  - {item['source']}/{item['item_id']}")

    if not items:
        lines.append("  (none)")

    lines.append("")
    lines.append("Answer the user's message. Be concise and helpful. If they ask to add/remove items, tell them to use /subscribe or /unsubscribe.")
    return "\n".join(lines)


async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages: URLs → subscribe, else → Claude with context."""
    text = update.message.text or ""

    # Check for URLs
    urls = _URL_PATTERN.findall(text)
    if urls:
        await _subscribe_url(update, urls[0])
        return

    # Route to Claude with monitoring context
    global _claude_busy
    if _claude_busy:
        await update.message.reply_text("⏳ Claude is already working. Please wait.")
        return

    _claude_busy = True
    await update.message.reply_text("🧠 Thinking...")

    try:
        ctx = _build_context()
        enriched_prompt = f"{ctx}\n\nUser message: {text}"
        response = await asyncio.to_thread(
            claude_integration.run_claude, enriched_prompt, allow_edits=False
        )
        display = response[:3500] if len(response) > 3500 else response
        await update.message.reply_text(display or "No response from Claude.")
    except Exception as e:
        log.exception("Claude request failed")
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        _claude_busy = False


# ── Setup ─────────────────────────────────────────────────────────────

def setup(app: Application):
    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("stock_toggle", cmd_stock_toggle))
    app.add_handler(CommandHandler("filter_toggle", cmd_filter_toggle))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("flights", cmd_flights))
    app.add_handler(CommandHandler("flights_toggle", cmd_flights_toggle))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("claude", cmd_claude))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Free-text messages (lowest priority)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))
