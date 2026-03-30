import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
STOCK_CHECK_INTERVAL = int(os.getenv("STOCK_CHECK_INTERVAL", "300"))
FLIGHT_CHECK_INTERVAL = int(os.getenv("FLIGHT_CHECK_INTERVAL", "60"))

# Allowlist: comma-separated chat IDs that may use the bot.
# Falls back to legacy TELEGRAM_CHAT_ID for backward compat.
_raw_ids = os.getenv("ALLOWED_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", "")
ALLOWED_CHAT_IDS: set[int] = {int(x.strip()) for x in _raw_ids.split(",") if x.strip()}

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
