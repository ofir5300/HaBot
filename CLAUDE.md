# HaBot — Availability Monitor Telegram Bot

## What is this?
A Telegram bot that monitors product availability (KSP stock), community events (Smarticket, Kehilatayim), and TLV flight departures — sending alerts when items come back in stock or new events/flights appear.

## Architecture
HaBot is a **thin domain layer on top of [TeleClaude](https://github.com/ofir5300/teleclaude)**, which provides the Telegram bot base class, Claude Code session integration, plan/approve/reject workflow, voice transcription, and self-restart. HaBot only ships the domain pieces:

- **`habot_bot.py`** — `HaBotTelegramBot(TeleClaudeBot)`. Implements `domain_commands()`, `on_domain_callback()`, `help_text()`, and multi-user broadcast. All domain commands (`/stock`, `/filters`, `/flights`, `/subscribe`, `/list`, ...) live here.
- **`main.py`** — sync entrypoint: starts `BackgroundScheduler` jobs (stock/events/flights/daily) and `bot.start_polling()`.
- **`monitor.py`** — state persistence, subscribers, filter settings, checker orchestration.
- **`checkers/`** — per-source adapters (`ksp`, `smarticket`, `kehilatayim`, `flights`).
- **`url_parser.py`** — URL → `(source, item_id)` dispatcher.
- **`config.py`** — env config (`TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS`, intervals).

TeleClaude owns everything else: Telegram HTTP, long-polling, auth-scoped updates, `/claude`, `/approve`, `/reject`, `/restart`, `/session`, `/context`, `/help`, voice → Whisper → Claude, plan-mode prompt flow.

## Running
```bash
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS
pip install -r requirements.txt
python main.py
```

`teleclaude` is installed from the pinned git ref in `requirements.txt` (stable commit, not editable local).

## Adding a new checker
1. Create `checkers/{source}.py`
2. Subclass `Checker`, implement `source_name` and `check(item_id)`
3. Call `register(YourChecker())` at module level
4. Add a URL pattern to `url_parser.py`
5. Auto-discovery in `checkers.discover()` handles the rest

### Checker minimal shape
```python
from checkers import Checker, StockResult, register

class KSPChecker(Checker):
    @property
    def source_name(self) -> str:
        return "ksp"

    def check(self, item_id: str) -> StockResult:
        return StockResult(in_stock=True, price=65.0, name="Product", url="...")

register(KSPChecker())
```

## Claude-driven checker generation
Unknown URL → TeleClaude plan mode proposes a checker → user `/approve` → TeleClaude edit mode writes `checkers/{source}.py` → bot auto-restarts.

The plan-mode prompt is customized in `HaBotTelegramBot.plan_prompt_wrapper`, which injects current monitoring context so Claude answers with HaBot state in mind.

## Key design decisions
- **Sync throughout.** TeleClaude is sync (raw `requests` + long-polling); APScheduler jobs use `BackgroundScheduler`. No asyncio.
- **Multi-user.** `ALLOWED_CHAT_IDS` is the authorization set; `monitor.get_registered_users()` is the broadcast set. `habot_bot.py` overrides `process_update` to route per-request replies to the requester, not the admin chat.
- **Transition detection only** — no duplicate alerts; state in `state.json` survives restarts.
- **No Docker** — git + systemd on RPi, so Claude Code edits land via `git pull` + `/restart`.
