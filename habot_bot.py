"""HaBot Telegram interface — extends TeleClaudeBot.

Sync implementation. Multi-user: ALLOWED_CHAT_IDS is the authorization set;
self.chat_id is the admin chat. Alerts broadcast to monitor.get_registered_users().
"""
import json
import logging
import re
from pathlib import Path
from typing import Optional

import requests

from teleclaude import TeleClaudeBot, ClaudeSession

import monitor
from checkers import get_checker, StockResult
from checkers.smarticket import SmartTicketEvent
from checkers.flights import FlightDeparture
from config import TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS
from url_parser import parse_product_url

log = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://\S+")

_AIRLINE_DISPLAY = {
    "LY": "El Al", "IZ": "Arkia", "6H": "Israir",
    "5C": "CAL", "7L": "Silk Way", "U8": "TUS Airways",
    "W6": "Wizz Air", "FR": "Ryanair", "U2": "EasyJet",
    "PC": "Pegasus", "5F": "Fly One", "E2": "Eurowings",
    "3F": "FlyOne Armenia", "BZ": "Blue Bird", "FP": "FlyPop",
    "WZ": "Red Wings", "HH": "FlyHiSky", "A9": "Georgian Airways",
    "OE": "Overland Airways", "RD": "Rotana Jet", "HU": "Hainan Airlines",
}


def _esc_md(text: str) -> str:
    """Escape Markdown special characters for Telegram Markdown parse_mode."""
    if not text:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


class HaBotTelegramBot(TeleClaudeBot):
    """HaBot Telegram bot. Stock/events/flights + Claude Code checker generation."""

    def __init__(self, token: str | None = None):
        project_dir = str(Path(__file__).parent.resolve())
        admin_chat_id = str(next(iter(ALLOWED_CHAT_IDS))) if ALLOWED_CHAT_IDS else ""

        claude = ClaudeSession(
            project_dir=project_dir,
            session_name_prefix="habot-telegram",
        )
        super().__init__(
            token=token or TELEGRAM_BOT_TOKEN,
            chat_id=admin_chat_id,
            claude_session=claude,
            project_dir=project_dir,
        )

        self._current_chat_id: Optional[str] = None
        self._pending_checker_prompt: Optional[str] = None
        self._pending_checker_url: Optional[str] = None
        # Set by main.py: callback(mode: str) that reschedules polling jobs.
        self.on_frequency_change = None

    # -- Auth + per-request chat_id override -------------------------------

    def process_update(self, update: dict):
        """Multi-user routing: authorize against ALLOWED_CHAT_IDS and track
        the requester's chat_id so self.send() replies to them (not admin)."""
        uid = update.get("update_id")
        if uid is not None:
            if uid in self._seen_update_ids:
                return
            self._seen_update_ids.add(uid)
            if len(self._seen_update_ids) > 200:
                self._seen_update_ids.discard(min(self._seen_update_ids))
            self.last_update_id = uid

        callback = update.get("callback_query")
        if callback:
            cb_chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            try:
                if int(cb_chat_id) not in ALLOWED_CHAT_IDS:
                    return
            except ValueError:
                return
            self._current_chat_id = cb_chat_id
            try:
                self._handle_callback(callback)
            finally:
                self._current_chat_id = None
            return

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))

        # Unauthorized user — reply once so they can request access, then drop
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            return
        if chat_id_int not in ALLOWED_CHAT_IDS:
            self._send_raw(
                chat_id,
                f"⛔ Not authorized.\n\nYour chat ID: `{chat_id}`\n"
                "Share this with the bot admin to get access.",
                parse_mode="Markdown",
            )
            return

        voice = message.get("voice") or message.get("audio")
        if voice:
            self._current_chat_id = chat_id
            try:
                self._handle_voice_message(voice.get("file_id"))
            finally:
                self._current_chat_id = None
            return

        text = message.get("text", "").strip()
        if not text:
            return

        self._last_message_text = text
        self._current_chat_id = chat_id
        try:
            if text.startswith("/"):
                cmd = text.split()[0].lower().split("@")[0]
                if cmd in self.commands:
                    print(f"[cmd] {cmd} from {chat_id}", flush=True)
                    self.commands[cmd]()
                else:
                    print(f"[cmd] Unknown command: {cmd}", flush=True)
                    self.send(f"❓ Unknown command: {cmd}\nType /help for available commands")
            else:
                self._handle_free_text(text)
        finally:
            self._current_chat_id = None

    # -- Send overrides that prefer current requester over admin -----------

    def _send_raw(self, chat_id: str, text: str, parse_mode: str = "HTML",
                  reply_markup: dict | None = None) -> Optional[int]:
        """POST sendMessage to an arbitrary chat_id. Returns message_id on success."""
        if not self.token or not chat_id:
            return None
        url = f"{self.base_url}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            resp = requests.post(url, data=data, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("result", {}).get("message_id")
            # HTML parse failed — retry without parse_mode
            data.pop("parse_mode", None)
            resp2 = requests.post(url, data=data, timeout=10)
            if resp2.status_code != 200:
                log.warning("Telegram send failed (%s): %s", resp2.status_code, resp2.text[:300])
            return resp2.json().get("result", {}).get("message_id") if resp2.status_code == 200 else None
        except Exception:
            log.exception("Telegram send_raw error")
            return None

    def send(self, message: str) -> bool:
        """Send to current requester if set, else admin. HTML parse_mode."""
        target = self._current_chat_id or self.chat_id
        return self._send_raw(target, message, parse_mode="HTML") is not None

    def send_md(self, message: str, chat_id: str | None = None,
                reply_markup: dict | None = None) -> Optional[int]:
        """Send with Markdown parse_mode (HaBot's native format)."""
        target = chat_id or self._current_chat_id or self.chat_id
        return self._send_raw(target, message, parse_mode="Markdown", reply_markup=reply_markup)

    def send_with_markup(self, message: str, reply_markup: dict) -> Optional[int]:
        target = self._current_chat_id or self.chat_id
        return self._send_raw(target, message, parse_mode="HTML", reply_markup=reply_markup)

    def edit_message(self, message_id: int, text: str, reply_markup: dict | None = None,
                     parse_mode: str = "HTML") -> bool:
        target = self._current_chat_id or self.chat_id
        if not self.token or not target:
            return False
        url = f"{self.base_url}/editMessageText"
        payload = {
            "chat_id": target,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            resp = requests.post(url, data=payload, timeout=10)
            if resp.status_code != 200:
                log.warning("Telegram edit failed (%s): %s", resp.status_code, resp.text[:300])
            return resp.status_code == 200
        except Exception:
            log.exception("Telegram edit error")
            return False

    # -- Broadcast (multi-user alerts) -------------------------------------

    def broadcast_md(self, text: str, reply_markup: dict | None = None):
        """Send a Markdown message to every registered subscriber."""
        for chat_id in monitor.get_registered_users():
            self._send_raw(str(chat_id), text, parse_mode="Markdown", reply_markup=reply_markup)

    # -- Alert senders (sync; called by scheduler via main.py) -------------

    def send_alert(self, item: dict, result: StockResult):
        price_str = f"{result.price} ₪" if result.price else "N/A"
        text = (
            f"🟢 *In Stock!*\n\n"
            f"*{_esc_md(result.name or item['item_id'])}*\n"
            f"Price: {price_str}\n"
            f"Source: {item['source'].upper()}\n\n"
            f"[Buy now]({result.url})"
        )
        self.broadcast_md(text)

    def send_smarticket_alert(self, events: list[SmartTicketEvent], source: str = "Smarticket"):
        lines = [f"🎟 *New {source} events available!*\n"]
        for e in events:
            date_str = ""
            if e.date:
                from datetime import date as _date
                try:
                    d = _date.fromisoformat(e.date)
                    date_str = f"📅 {d.strftime('%a %d/%m')}  "
                except ValueError:
                    pass
            lines.append(
                f"• *{_esc_md(e.name)}*\n"
                f"  {date_str}🕐 {e.time}  📍 {_esc_md(e.venue)}\n"
                f"  [Order tickets]({e.url})"
            )
        self.broadcast_md("\n\n".join(lines))

    def send_flight_alert(self, flights: list[FlightDeparture]):
        from datetime import datetime
        lines = ["✈️  *New TLV departure detected!*\n"]
        for f in flights:
            dep_str = datetime.fromtimestamp(f.scheduled_ts).strftime("%a %d/%m  %H:%M")
            lines.append(
                f"• *{_esc_md(f.flight_number)} — {_esc_md(f.airline_name)}*\n"
                f"  📅 {dep_str}  ✈️  TLV → {_esc_md(f.destination_city)} ({f.destination_iata})\n"
                f"  Status: {_esc_md(f.status)}\n"
                f"  [FR24]({f.fr24_url}) · [{_esc_md(f.airline_name)}]({f.airline_url}) · [Book (Kayak)]({f.booking_url()})"
            )
        self.broadcast_md("\n\n".join(lines))

    def send_failure_alert(self, notif: dict):
        """Broadcast a failed-check (or recovery) notification."""
        key = notif.get("key", "?")
        count = notif.get("count", 0)
        if notif.get("type") == "recovered":
            text = f"✅ *Check recovered*\n\n`{_esc_md(key)}` is responding again."
        else:
            text = (
                f"⚠️ *Repeated check failures*\n\n"
                f"`{_esc_md(key)}` has failed *{count}* times in a row.\n"
                f"Its checker may be broken or the source may be down."
            )
        self.broadcast_md(text)

    def send_weekly_summary(self):
        state = monitor.load_state()
        lines = ["📋 *Weekly Summary*\n"]
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
                    lines.append(f"• *{_esc_md(name)}*{price_str}\n  {status}")
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
        self.broadcast_md("\n".join(lines))

    def broadcast_startup(self, text: str):
        self.broadcast_md(text)

    # -- TeleClaudeBot hooks -----------------------------------------------

    def domain_commands(self):
        return {
            "/start": (self._cmd_start, "Welcome message"),
            "/events": (self._cmd_events, "Events hub (Smarticket + Kehilatayim)"),
            "/flights": (self._cmd_flights, "Flights hub (TLV departures)"),
            "/stock": (self._cmd_stock, "Stock hub (monitored products)"),
            "/frequency": (self._cmd_frequency, "How often to check (minutes/hourly/daily)"),
        }

    def help_text(self) -> str:
        return (
            "<b>🤖 HaBot</b>\n\n"
            "/events — Smarticket + Kehilatayim (filters, pause, toddler toggle)\n"
            "/flights — TLV departures (filters, pause)\n"
            "/stock — Monitored products (check, remove, pause)\n"
            "/frequency — How often I check (few minutes / hourly / daily)\n\n"
            "Paste a product URL in chat to subscribe.\n"
            "Send free text or voice to chat with Claude.\n\n"
            "/claude · /restart · /help"
        )

    def on_domain_callback(self, data: str, message_id: int) -> bool:
        if data.startswith("flights:"):
            return self._handle_flights_callback(data, message_id)
        if data.startswith("filter:"):
            return self._handle_filter_callback(data, message_id)
        if data.startswith("events:"):
            return self._handle_events_callback(data, message_id)
        if data.startswith("stock:"):
            return self._handle_stock_callback(data, message_id)
        if data.startswith("freq:"):
            return self._handle_frequency_callback(data, message_id)
        if data == "unsub:cancel":
            self.edit_message(message_id, "Cancelled.")
            return True
        if data.startswith("unsub:"):
            _, source, item_id = data.split(":", 2)
            removed = monitor.remove_item(source, item_id)
            if removed:
                self.edit_message(
                    message_id, f"✅ Removed {source.upper()} item <code>{item_id}</code>.",
                )
            else:
                self.edit_message(message_id, "Item not found.")
            return True
        if data.startswith("toggle:"):
            _, source, item_id = data.split(":", 2)
            items = monitor.get_items()
            current = next(
                (i for i in items if i["source"] == source and i["item_id"] == item_id),
                None,
            )
            if not current:
                self.edit_message(message_id, "Item not found.")
                return True
            new_enabled = not current.get("enabled", True)
            monitor.set_item_enabled(source, item_id, new_enabled)
            text, keyboard = self._build_stock_view()
            self.edit_message(message_id, text, reply_markup=keyboard, parse_mode="Markdown")
            return True
        if data.startswith("check:"):
            _, source, item_id = data.split(":", 2)
            checker = get_checker(source)
            if not checker:
                self.edit_message(message_id, f"⚠️ No checker for {source}")
                return True
            try:
                result = checker.check(item_id)
                status = "🟢 In stock" if result.in_stock else "🔴 Out of stock"
                price_str = f" — {result.price} ₪" if result.price else ""
                name = result.name or item_id
                self.edit_message(
                    message_id,
                    f"<b>{_html_esc(name)}</b>{price_str}\n{status} ({source.upper()})",
                )
            except Exception as e:
                self.edit_message(message_id, f"❌ Check failed: {e}")
            return True
        return False

    # -- Commands (sync) ---------------------------------------------------

    def _cmd_start(self):
        chat_id_int = int(self._current_chat_id) if self._current_chat_id else None
        is_new = False
        if chat_id_int is not None:
            is_new = monitor.register_user(chat_id_int)

        text = (
            "👋 *Welcome to HaBot!*\n\n"
            "I monitor product availability and community events, "
            "alerting you the moment something new appears.\n\n"
        )

        settings = monitor.get_filter_settings()
        toddler_on = monitor.is_toddler_filter_on()
        text += "🎟 *Event sources:*\n"
        for src, enabled in settings["sources"].items():
            icon = "✅" if enabled else "❌"
            text += f"• {icon} {src.capitalize()}\n"
        if toddler_on:
            text += f"• 🧒 Toddler filter ON (max age: {settings['max_age']})\n"

        flights_enabled = monitor.is_flights_enabled()
        flight_settings = monitor.get_flight_filter_settings()
        airlines_on = [k for k, v in flight_settings["airlines"].items() if v]
        text += "\n✈️  *Flight monitoring (TLV):*\n"
        icon = "✅" if flights_enabled else "⏸"
        text += f"• {icon} {'Active' if flights_enabled else 'Paused'}\n"
        text += f"• Airlines: {', '.join(airlines_on) if airlines_on else 'All'}\n"
        text += "• Use /flights to configure\n"

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

        freq_label = monitor.FREQUENCY_PRESETS[monitor.get_check_frequency()]["label"]
        text += (
            "\n🔔 *How it works:*\n"
            f"• Check frequency: *{freq_label}* (change with /frequency)\n"
            "• Alerts on new available events & stock transitions\n"
            "• Heads-up if a checker keeps failing\n"
            "• Weekly summary on Sundays at 19:00\n\n"
            "Use /events to manage event filters.\n"
            "Use /help to see all commands."
        )
        if is_new and chat_id_int is not None:
            text += f"\n\n✅ Registered for notifications (chat ID: `{chat_id_int}`)"
        self.send_md(text)

    def _cmd_events(self):
        text, keyboard = self._build_events_view()
        self.send_md(text, reply_markup=keyboard)

    def _cmd_flights(self):
        text, keyboard = self._build_flights_view()
        self.send_md(text, reply_markup=keyboard)

    def _cmd_stock(self):
        text, keyboard = self._build_stock_view()
        self.send_md(text, reply_markup=keyboard)

    def _cmd_frequency(self):
        text, keyboard = self._build_frequency_view()
        self.send_md(text, reply_markup=keyboard)

    def _build_frequency_view(self) -> tuple[str, dict]:
        current = monitor.get_check_frequency()
        presets = monitor.FREQUENCY_PRESETS
        lines = ["⏱ *Check Frequency*\n"]
        lines.append("How often I check stock, events and flights.\n")
        for mode, cfg in presets.items():
            mark = "✅ " if mode == current else "▫️ "
            lines.append(f"{mark}*{cfg['label']}*")
        lines.append("\n_Less frequent checks = fewer, later alerts._")

        buttons = []
        for mode, cfg in presets.items():
            label = ("✅ " if mode == current else "") + cfg["label"]
            buttons.append([{"text": label, "callback_data": f"freq:{mode}"}])
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _handle_frequency_callback(self, data: str, message_id: int) -> bool:
        mode = data.split(":", 1)[1]
        if mode in monitor.FREQUENCY_PRESETS:
            monitor.set_check_frequency(mode)
            if callable(self.on_frequency_change):
                try:
                    self.on_frequency_change(mode)
                except Exception:
                    log.exception("Failed to apply frequency change")
        text, keyboard = self._build_frequency_view()
        self.edit_message(message_id, text, reply_markup=keyboard, parse_mode="Markdown")
        return True

    # -- Subscribe flow (known URL direct; unknown → Claude plan) ----------

    def _subscribe_url(self, url: str):
        parsed = parse_product_url(url)
        if parsed:
            source, item_id = parsed
            _, is_new = monitor.add_item(source, item_id)
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
                self.send_md(
                    f"✅ Subscribed to *{_esc_md(name)}* ({source.upper()})\n"
                    f"Item ID: `{item_id}`{status_line}"
                )
            else:
                self.send_md(
                    f"Already monitoring *{_esc_md(name)}* ({source.upper()}){status_line}\n\n"
                    f"Use /stock to manage your subscriptions."
                )
            return

        # Unknown URL — ask Claude to propose a checker (plan mode)
        if self._claude_busy:
            self.send("⏳ Claude is already working on something. Please wait.")
            return

        self._claude_busy = True
        self._pending_checker_url = url
        self.send_md(f"🔍 Unknown source. Asking Claude to analyze...\n`{url}`")

        try:
            prompt = (
                f"Analyze this product/availability URL and propose a checker for it:\n"
                f"{url}\n\n"
                "1. Visit the URL and understand what product/service it tracks\n"
                "2. Figure out how to check availability (API, scraping, etc.)\n"
                "3. Propose a plan for creating a checker in checkers/{source}.py\n"
                "4. Include: source_name, how to parse the item_id from the URL, "
                "how to check availability, what data to extract\n\n"
                "DO NOT create any files yet. Just propose the plan."
            )
            response = self.claude.run(prompt, allow_edits=False)
            self._pending_checker_prompt = (
                f"Execute the plan to create a checker for {url}.\n\n"
                f"Create the checker file in checkers/ following the Checker ABC pattern. "
                f"Also update url_parser.py with the URL pattern for this source. "
                f"Register the checker at module level.\n\n"
                f"Previous analysis:\n{response}"
            )
            display = response[:3500] if response and len(response) > 3500 else (response or "")
            self.send_md(
                f"🧠 *Claude's Analysis:*\n\n{display}\n\n"
                f"👆 Use /approve to execute or /reject to cancel."
            )
            # Register with the base bot so /approve works
            self._claude_pending_prompt = self._pending_checker_prompt
        except Exception as e:
            log.exception("Claude analysis failed")
            self.send(f"❌ Claude analysis failed: {e}")
            self._pending_checker_prompt = None
            self._pending_checker_url = None
            self._claude_pending_prompt = None
        finally:
            self._claude_busy = False

    # -- Free text: URL → subscribe, else Claude ---------------------------

    def _handle_free_text(self, text: str):
        urls = _URL_PATTERN.findall(text)
        if urls:
            self._subscribe_url(urls[0])
            return
        # Delegate to base class free-text-to-claude handler
        self._handle_claude_message(text)

    def plan_prompt_wrapper(self, user_text: str) -> str:
        ctx = self._build_monitoring_context()
        return (
            f"{ctx}\n\n"
            f"The user sent this via Telegram: {user_text}\n\n"
            "Analyze the request and respond concisely. "
            "If a code change is needed, describe your plan (under 3000 chars). "
            "They will send /approve to let you implement it."
        )

    def _build_monitoring_context(self) -> str:
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
        return "\n".join(lines)

    # -- Inline keyboard views ---------------------------------------------

    def _build_events_view(self) -> tuple[str, dict]:
        from checkers.smarticket import fetch_events_range, is_relevant_for_toddler
        from checkers.kehilatayim import fetch_events as fetch_kehilatayim_events
        from datetime import date as _date

        paused = monitor.is_events_paused()
        toddler_on = monitor.is_toddler_filter_on()
        settings = monitor.get_filter_settings()
        sources_on = [s.capitalize() for s, en in settings["sources"].items() if en]

        lines = [f"🎟 *Events* {'⏸ PAUSED' if paused else '▶️ Active'}\n"]
        toddler_label = f"🧒 Toddler filter: *{'ON' if toddler_on else 'OFF'}*"
        if toddler_on:
            toddler_label += f" (max age {settings['max_age']})"
        lines.append(toddler_label)
        lines.append(f"📡 Sources: {', '.join(sources_on) if sources_on else 'None'}\n")

        def _format_section(source_name: str, raw_events: list, filter_fn) -> list[str]:
            evts = [e for e in raw_events if filter_fn(e)] if toddler_on else raw_events
            section = [f"*{source_name}*"]
            if not evts:
                section.append("  No events found")
                return section
            by_date: dict[str, list] = {}
            for e in evts:
                by_date.setdefault(e.date, []).append(e)
            for event_date in sorted(by_date.keys()):
                try:
                    d = _date.fromisoformat(event_date)
                    day_label = d.strftime("%a %d/%m")
                except ValueError:
                    day_label = event_date
                day_events = by_date[event_date]
                available = [e for e in day_events if e.available]
                sold_out = len(day_events) - len(available)
                section.append(f"\n_{day_label}_ ({len(available)} available, {sold_out} sold out)")
                for e in available:
                    section.append(f"• 🟢 {e.time} — {_esc_md(e.name)}")
                if not available:
                    section.append("  All sold out")
            return section

        if not paused:
            if monitor.is_source_enabled("smarticket"):
                lines.append("")
                try:
                    lines.extend(_format_section(
                        "Smarticket — next 7 days", fetch_events_range(),
                        lambda e: is_relevant_for_toddler(e.name),
                    ))
                except Exception:
                    lines.append("*Smarticket*\n  ❌ Error fetching events")
            if monitor.is_source_enabled("kehilatayim"):
                lines.append("")
                try:
                    lines.extend(_format_section(
                        "Kehilatayim (Givatayim)", fetch_kehilatayim_events(),
                        lambda e: is_relevant_for_toddler(e.name),
                    ))
                except Exception:
                    lines.append("*Kehilatayim*\n  ❌ Error fetching events")

        # Truncate to safe Telegram size
        text = "\n".join(lines)
        if len(text) > 3800:
            text = text[:3800] + "\n…"

        buttons = [
            [
                {"text": "▶️ Resume" if paused else "⏸ Pause",
                 "callback_data": "events:toggle"},
                {"text": f"🧒 Toddler: {'ON' if toddler_on else 'OFF'}",
                 "callback_data": "events:toddler"},
            ],
            [{"text": "⚙️ Filters", "callback_data": "events:filters"}],
            [{"text": "🔄 Refresh", "callback_data": "events:refresh"}],
        ]
        return text, {"inline_keyboard": buttons}

    def _build_stock_view(self) -> tuple[str, dict]:
        items = monitor.get_items()
        paused = monitor.is_paused()
        header = f"📊 *Stock* {'⏸ PAUSED' if paused else '▶️ Active'}"

        if not items:
            text = (f"{header}\n\nNo items being monitored.\n"
                    "Paste a product URL in chat to add one.")
            buttons = [[{"text": "▶️ Resume" if paused else "⏸ Pause",
                         "callback_data": "stock:toggle"}]]
            return text, {"inline_keyboard": buttons}

        lines = [header, ""]
        buttons = [[{"text": "▶️ Resume" if paused else "⏸ Pause",
                     "callback_data": "stock:toggle"},
                    {"text": "🔄 Refresh", "callback_data": "stock:refresh"}]]
        for item in items:
            checker = get_checker(item["source"])
            name = item["item_id"]
            enabled = item.get("enabled", True)
            disabled_tag = "" if enabled else " ⏸ paused"
            status_line = ""
            if checker:
                try:
                    result = checker.check(item["item_id"])
                    name = result.name or item["item_id"]
                    status = "🟢 In stock" if result.in_stock else "🔴 Out of stock"
                    price_str = f" — {result.price} ₪" if result.price else ""
                    status_line = f"\n  {status}{price_str} ({item['source'].upper()}){disabled_tag}"
                except Exception:
                    status_line = f"\n  ⚠️ Check failed ({item['source'].upper()}){disabled_tag}"
            else:
                status_line = f"\n  ⚠️ No checker ({item['source'].upper()}){disabled_tag}"
            lines.append(f"• *{_esc_md(name)}*{status_line}")
            toggle_label = "⏸ Pause" if enabled else "▶️ Resume"
            buttons.append([
                {"text": "🔍 Check", "callback_data": f"check:{item['source']}:{item['item_id']}"},
                {"text": toggle_label, "callback_data": f"toggle:{item['source']}:{item['item_id']}"},
                {"text": "🗑 Remove", "callback_data": f"unsub:{item['source']}:{item['item_id']}"},
            ])

        return "\n".join(lines), {"inline_keyboard": buttons}

    def _handle_events_callback(self, data: str, message_id: int) -> bool:
        if data == "events:toggle":
            monitor.toggle_events_pause()
        elif data == "events:toddler":
            monitor.toggle_toddler_filter()
        elif data == "events:filters":
            text, keyboard = self._build_filters_view()
            self.edit_message(message_id, text, reply_markup=keyboard, parse_mode="Markdown")
            return True
        elif data == "events:refresh":
            pass

        text, keyboard = self._build_events_view()
        self.edit_message(message_id, text, reply_markup=keyboard, parse_mode="Markdown")
        return True

    def _handle_stock_callback(self, data: str, message_id: int) -> bool:
        if data == "stock:toggle":
            monitor.toggle_pause()
        elif data == "stock:refresh":
            pass

        text, keyboard = self._build_stock_view()
        self.edit_message(message_id, text, reply_markup=keyboard, parse_mode="Markdown")
        return True

    def _build_filters_view(self) -> tuple[str, dict]:
        settings = monitor.get_filter_settings()
        toddler_on = monitor.is_toddler_filter_on()

        lines = ["⚙️ *Filter Settings*\n"]
        lines.append(f"🧒 Toddler filter: *{'ON' if toddler_on else 'OFF'}*")
        lines.append(f"📏 Max age: *{settings['max_age']}*\n")
        lines.append("*Event sources:*")
        for src, enabled in settings["sources"].items():
            icon = "✅" if enabled else "❌"
            lines.append(f"  {icon} {src.capitalize()}")
        lines.append("\n*Include terms:*")
        for term, enabled in settings["include_terms"].items():
            icon = "✅" if enabled else "❌"
            lines.append(f"  {icon} {term}")

        buttons = []
        btn_label = "🧒 Filter: OFF →" if toddler_on else "🧒 Filter: ON →"
        buttons.append([{"text": btn_label, "callback_data": "filter:toggle"}])

        age_row = []
        for age in (2.0, 2.5, 3.0, 3.5):
            label = f"{'✓ ' if settings['max_age'] == age else ''}{age}y"
            age_row.append({"text": label, "callback_data": f"filter:age:{age}"})
        buttons.append(age_row)

        src_row = []
        for src, enabled in settings["sources"].items():
            icon = "✅" if enabled else "❌"
            src_row.append({"text": f"{icon} {src.capitalize()}", "callback_data": f"filter:source:{src}"})
        buttons.append(src_row)

        for term, enabled in settings["include_terms"].items():
            icon = "✅" if enabled else "❌"
            buttons.append([{"text": f"{icon} {term}", "callback_data": f"filter:term:{term}"}])

        buttons.append([{"text": "✅ Done", "callback_data": "filter:done"}])
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _build_flights_view(self) -> tuple[str, dict]:
        enabled = monitor.is_flights_enabled()
        settings = monitor.get_flight_filter_settings()
        freq_label = monitor.FREQUENCY_PRESETS[monitor.get_check_frequency()]["label"]

        lines = ["✈️  *Flight Monitor (TLV Departures)*\n"]
        lines.append(f"Status: {'✅ Active' if enabled else '⏸ Paused'}")
        lines.append(f"Polling: {freq_label.lower()} (/frequency)\n")

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
        buttons.append([{"text": toggle_label, "callback_data": "flights:toggle"}])

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
                airline_row.append({"text": f"{icon} {iata}", "callback_data": f"flights:airline:{iata}"})
                if len(airline_row) == 4:
                    buttons.append(airline_row)
                    airline_row = []
            if airline_row:
                buttons.append(airline_row)

        hour_presets = [(1, 6, "01-06"), (6, 12, "06-12"), (12, 18, "12-18"), (22, 6, "22-06"), (0, 23, "All")]
        hour_row = []
        for lo, hi, label in hour_presets:
            active = min_h == lo and max_h == hi
            prefix = "-> " if active else ""
            hour_row.append({"text": f"{prefix}{label}", "callback_data": f"flights:hours:{lo}:{hi}"})
        buttons.append(hour_row)

        buttons.append([{"text": "✅ Done", "callback_data": "flights:done"}])
        return "\n".join(lines), {"inline_keyboard": buttons}

    def _handle_filter_callback(self, data: str, message_id: int) -> bool:
        if data == "filter:toggle":
            monitor.toggle_toddler_filter()
        elif data == "filter:done":
            text, keyboard = self._build_events_view()
            self.edit_message(message_id, text, reply_markup=keyboard, parse_mode="Markdown")
            return True
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

        text, keyboard = self._build_filters_view()
        self.edit_message(message_id, text, reply_markup=keyboard, parse_mode="Markdown")
        return True

    def _handle_flights_callback(self, data: str, message_id: int) -> bool:
        if data == "flights:toggle":
            monitor.toggle_flights()
        elif data == "flights:done":
            self.edit_message(message_id, "✅ Flight settings saved.")
            return True
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

        text, keyboard = self._build_flights_view()
        self.edit_message(message_id, text, reply_markup=keyboard, parse_mode="Markdown")
        return True


def _html_esc(text: str) -> str:
    import html as _h
    return _h.escape(text or "", quote=False)
