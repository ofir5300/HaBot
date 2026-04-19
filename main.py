import atexit
import logging
import os
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

import checkers
import monitor
import telegram_bot
from config import TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS, CHECK_INTERVAL, STOCK_CHECK_INTERVAL, FLIGHT_CHECK_INTERVAL

PIDFILE = os.path.join(os.path.dirname(__file__), "habot.pid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


async def poll_stock(app: Application):
    """Check stock for all monitored items (KSP etc.)."""
    alerts = monitor.check_all()
    for item, result in alerts:
        log.info("ALERT: %s/%s is in stock!", item["source"], item["item_id"])
        await telegram_bot.send_alert(app, item, result)


async def poll_smarticket(app: Application):
    """Check Smarticket for new available events."""
    if not monitor.is_source_enabled("smarticket"):
        return
    new_events = monitor.check_smarticket()
    if new_events:
        log.info("SMARTICKET ALERT: %d new events!", len(new_events))
        await telegram_bot.send_smarticket_alert(app, new_events, source="Smarticket")


async def poll_kehilatayim(app: Application):
    """Check Kehilatayim for new available events."""
    if not monitor.is_source_enabled("kehilatayim"):
        return
    new_events = monitor.check_kehilatayim()
    if new_events:
        log.info("KEHILATAYIM ALERT: %d new events!", len(new_events))
        await telegram_bot.send_smarticket_alert(app, new_events, source="Kehilatayim")


async def poll_flights(app: Application):
    """Check for new TLV departures from Flightradar24."""
    if not monitor.is_flights_enabled():
        return
    new_flights = monitor.check_flights()
    if new_flights:
        log.info("FLIGHTS ALERT: %d new departures!", len(new_flights))
        await telegram_bot.send_flight_alert(app, new_flights)


async def daily_summary(app: Application):
    log.info("Sending daily summary")
    await telegram_bot.send_daily_summary(app)


def _kill_previous():
    """Kill any previous HaBot instance using the pidfile."""
    import time
    if os.path.exists(PIDFILE):
        try:
            old_pid = int(open(PIDFILE).read().strip())
            if old_pid == os.getpid():
                return
            os.kill(old_pid, signal.SIGTERM)
            log.info("Sent SIGTERM to previous instance (PID %d)", old_pid)
            # Wait briefly then force-kill if still alive
            time.sleep(2)
            try:
                os.kill(old_pid, signal.SIGKILL)
                log.info("Force-killed PID %d", old_pid)
            except ProcessLookupError:
                pass  # already dead, good
        except (ProcessLookupError, ValueError):
            pass  # already dead or corrupt file
        except PermissionError:
            log.warning("Cannot kill PID in pidfile (permission denied)")

    # Write our own PID
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(PIDFILE) and os.remove(PIDFILE))


def main():
    _kill_previous()

    # Discover all checker modules
    checkers.discover()

    # Seed default item if state is empty
    state = monitor.load_state()
    if not state["items"]:
        monitor.add_item("ksp", "138173")
        log.info("Seeded KSP item 138173")

    # Build Telegram app
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_bot.setup(app)

    # Set up scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_stock, "interval", seconds=STOCK_CHECK_INTERVAL, args=[app],
        id="poll_stock", max_instances=1,
    )
    scheduler.add_job(
        poll_smarticket, "interval", seconds=CHECK_INTERVAL, args=[app],
        id="poll_smarticket", max_instances=1,
    )
    scheduler.add_job(
        poll_kehilatayim, "interval", seconds=CHECK_INTERVAL, args=[app],
        id="poll_kehilatayim", max_instances=1,
    )
    scheduler.add_job(
        poll_flights, "interval", seconds=FLIGHT_CHECK_INTERVAL, args=[app],
        id="poll_flights", max_instances=1,
    )
    scheduler.add_job(
        daily_summary, "cron", hour=19, minute=0, args=[app],
        id="daily_summary",
    )

    # Auto-register all allowed users (so they get alerts even before /start)
    for cid in ALLOWED_CHAT_IDS:
        monitor.register_user(cid)

    # Start everything
    async def post_init(application: Application):
        await application.bot.set_my_commands(telegram_bot.COMMANDS)
        scheduler.start()
        log.info(
            "Stock check every %ds, Events every %ds, daily summary at 19:00",
            STOCK_CHECK_INTERVAL, CHECK_INTERVAL,
        )
        await telegram_bot.broadcast(
            application,
            f"🤖 *HaBot started*\n\n"
            f"• Stock check: every {STOCK_CHECK_INTERVAL}s\n"
            f"• Smarticket: every {CHECK_INTERVAL}s\n"
            f"• Kehilatayim: every {CHECK_INTERVAL}s\n"
            f"• Flights (TLV): every {FLIGHT_CHECK_INTERVAL}s\n"
            f"• Daily summary: 19:00",
            parse_mode="Markdown",
        )

    app.post_init = post_init
    log.info("Starting HaBot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
