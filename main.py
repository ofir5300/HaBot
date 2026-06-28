import logging
import os
import time

from apscheduler.schedulers.background import BackgroundScheduler

from teleclaude import kill_previous

import checkers
import monitor
from config import ALLOWED_CHAT_IDS
from habot_bot import HaBotTelegramBot

PIDFILE = os.path.join(os.path.dirname(__file__), "habot.pid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


def flush_failure_alerts(bot: HaBotTelegramBot):
    """Broadcast any queued failed-check / recovery notifications."""
    for notif in monitor.pop_failure_notifications():
        log.warning("CHECK FAILURE NOTIFY: %s", notif)
        bot.send_failure_alert(notif)


def poll_stock(bot: HaBotTelegramBot):
    alerts = monitor.check_all()
    for item, result in alerts:
        log.info("ALERT: %s/%s is in stock!", item["source"], item["item_id"])
        bot.send_alert(item, result)
    flush_failure_alerts(bot)


def poll_smarticket(bot: HaBotTelegramBot):
    if not monitor.is_source_enabled("smarticket"):
        return
    new_events = monitor.check_smarticket()
    if new_events:
        log.info("SMARTICKET ALERT: %d new events!", len(new_events))
        bot.send_smarticket_alert(new_events, source="Smarticket")
    flush_failure_alerts(bot)


def poll_kehilatayim(bot: HaBotTelegramBot):
    if not monitor.is_source_enabled("kehilatayim"):
        return
    new_events = monitor.check_kehilatayim()
    if new_events:
        log.info("KEHILATAYIM ALERT: %d new events!", len(new_events))
        bot.send_smarticket_alert(new_events, source="Kehilatayim")
    flush_failure_alerts(bot)


def poll_flights(bot: HaBotTelegramBot):
    if not monitor.is_flights_enabled():
        return
    new_flights = monitor.check_flights()
    if new_flights:
        log.info("FLIGHTS ALERT: %d new departures!", len(new_flights))
        bot.send_flight_alert(new_flights)
    flush_failure_alerts(bot)


def weekly_summary(bot: HaBotTelegramBot):
    log.info("Sending weekly summary")
    bot.send_weekly_summary()


def apply_frequency(scheduler, mode: str):
    """Reschedule the polling jobs to match the selected check-frequency mode."""
    intervals = monitor.frequency_intervals(mode)
    scheduler.reschedule_job("poll_stock", trigger="interval", seconds=intervals["stock"])
    scheduler.reschedule_job("poll_smarticket", trigger="interval", seconds=intervals["events"])
    scheduler.reschedule_job("poll_kehilatayim", trigger="interval", seconds=intervals["events"])
    scheduler.reschedule_job("poll_flights", trigger="interval", seconds=intervals["flights"])
    log.info("Check frequency set to '%s': %s", mode, intervals)


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

    mode = monitor.get_check_frequency()
    intervals = monitor.frequency_intervals(mode)

    scheduler = BackgroundScheduler()
    scheduler.add_job(poll_stock, "interval", seconds=intervals["stock"], args=[bot],
                      id="poll_stock", max_instances=1)
    scheduler.add_job(poll_smarticket, "interval", seconds=intervals["events"], args=[bot],
                      id="poll_smarticket", max_instances=1)
    scheduler.add_job(poll_kehilatayim, "interval", seconds=intervals["events"], args=[bot],
                      id="poll_kehilatayim", max_instances=1)
    scheduler.add_job(poll_flights, "interval", seconds=intervals["flights"], args=[bot],
                      id="poll_flights", max_instances=1)
    # Weekly summary: Sundays at 19:00
    scheduler.add_job(weekly_summary, "cron", day_of_week="sun", hour=19, minute=0,
                      args=[bot], id="weekly_summary")
    scheduler.start()

    # Let the bot reschedule polling jobs at runtime (/frequency command)
    bot.on_frequency_change = lambda m: apply_frequency(scheduler, m)

    freq_label = monitor.FREQUENCY_PRESETS[mode]["label"]
    log.info("Check frequency '%s': %s, weekly summary Sun 19:00", mode, intervals)

    bot.broadcast_startup(
        f"🤖 *HaBot started*\n\n"
        f"• Check frequency: *{freq_label}*\n"
        f"• Stock: every {intervals['stock']}s\n"
        f"• Events: every {intervals['events']}s\n"
        f"• Flights (TLV): every {intervals['flights']}s\n"
        f"• Weekly summary: Sun 19:00\n\n"
        f"Use /frequency to change how often I check."
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
