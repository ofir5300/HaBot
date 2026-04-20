import logging
import os
import time

from apscheduler.schedulers.background import BackgroundScheduler

from teleclaude import kill_previous

import checkers
import monitor
from config import (
    ALLOWED_CHAT_IDS, CHECK_INTERVAL, STOCK_CHECK_INTERVAL, FLIGHT_CHECK_INTERVAL,
)
from habot_bot import HaBotTelegramBot

PIDFILE = os.path.join(os.path.dirname(__file__), "habot.pid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


def poll_stock(bot: HaBotTelegramBot):
    alerts = monitor.check_all()
    for item, result in alerts:
        log.info("ALERT: %s/%s is in stock!", item["source"], item["item_id"])
        bot.send_alert(item, result)


def poll_smarticket(bot: HaBotTelegramBot):
    if not monitor.is_source_enabled("smarticket"):
        return
    new_events = monitor.check_smarticket()
    if new_events:
        log.info("SMARTICKET ALERT: %d new events!", len(new_events))
        bot.send_smarticket_alert(new_events, source="Smarticket")


def poll_kehilatayim(bot: HaBotTelegramBot):
    if not monitor.is_source_enabled("kehilatayim"):
        return
    new_events = monitor.check_kehilatayim()
    if new_events:
        log.info("KEHILATAYIM ALERT: %d new events!", len(new_events))
        bot.send_smarticket_alert(new_events, source="Kehilatayim")


def poll_flights(bot: HaBotTelegramBot):
    if not monitor.is_flights_enabled():
        return
    new_flights = monitor.check_flights()
    if new_flights:
        log.info("FLIGHTS ALERT: %d new departures!", len(new_flights))
        bot.send_flight_alert(new_flights)


def daily_summary(bot: HaBotTelegramBot):
    log.info("Sending daily summary")
    bot.send_daily_summary()


def main():
    kill_previous(PIDFILE)

    checkers.discover()

    state = monitor.load_state()
    if not state["items"]:
        monitor.add_item("ksp", "138173")
        log.info("Seeded KSP item 138173")

    for cid in ALLOWED_CHAT_IDS:
        monitor.register_user(cid)

    bot = HaBotTelegramBot()
    if not bot.is_configured:
        raise SystemExit("TELEGRAM_BOT_TOKEN / ALLOWED_CHAT_IDS must be set")

    scheduler = BackgroundScheduler()
    scheduler.add_job(poll_stock, "interval", seconds=STOCK_CHECK_INTERVAL, args=[bot],
                      id="poll_stock", max_instances=1)
    scheduler.add_job(poll_smarticket, "interval", seconds=CHECK_INTERVAL, args=[bot],
                      id="poll_smarticket", max_instances=1)
    scheduler.add_job(poll_kehilatayim, "interval", seconds=CHECK_INTERVAL, args=[bot],
                      id="poll_kehilatayim", max_instances=1)
    scheduler.add_job(poll_flights, "interval", seconds=FLIGHT_CHECK_INTERVAL, args=[bot],
                      id="poll_flights", max_instances=1)
    scheduler.add_job(daily_summary, "cron", hour=19, minute=0, args=[bot], id="daily_summary")
    scheduler.start()
    log.info(
        "Stock check every %ds, Events every %ds, Flights every %ds, daily summary at 19:00",
        STOCK_CHECK_INTERVAL, CHECK_INTERVAL, FLIGHT_CHECK_INTERVAL,
    )

    bot.broadcast_startup(
        f"🤖 *HaBot started*\n\n"
        f"• Stock check: every {STOCK_CHECK_INTERVAL}s\n"
        f"• Smarticket: every {CHECK_INTERVAL}s\n"
        f"• Kehilatayim: every {CHECK_INTERVAL}s\n"
        f"• Flights (TLV): every {FLIGHT_CHECK_INTERVAL}s\n"
        f"• Daily summary: 19:00"
    )

    log.info("Starting HaBot...")
    thread = bot.start_polling()
    try:
        while bot.running:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        bot.stop_polling()
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
