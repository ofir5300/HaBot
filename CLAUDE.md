# HaBot — Availability Monitor Telegram Bot

## What is this?
A Telegram bot that monitors product availability (starting with KSP) and sends alerts when items come back in stock.

## Architecture
- **Checker ABC** (`checkers/__init__.py`): Base class + auto-discovery registry for site-specific checkers
- **KSP Checker** (`checkers/ksp.py`): Uses `ksp.co.il/m_action/api/item/{uin}` — `addToCart: 1` = in stock
- **Monitor** (`monitor.py`): APScheduler polls every 30s, detects out→in transitions, persists state to `state.json`
- **Telegram** (`telegram_bot.py`): `/stock`, `/stock_toggle`, `/subscribe`, `/unsubscribe`, `/list`, `/approve`, `/reject`, `/restart`, `/claude`
- **Claude Integration** (`claude_integration.py`): Subprocess wrapper for `claude --print` with session persistence
- **URL Parser** (`url_parser.py`): Maps product URLs to `(source, item_id)` pairs

## Running
```bash
cp .env.example .env  # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
pip install -r requirements.txt
python main.py
```

## Adding a new checker
1. Create `checkers/{source}.py`
2. Subclass `Checker`, implement `source_name` and `check(item_id)`
3. Call `register(YourChecker())` at module level
4. The auto-discovery in `checkers.discover()` handles the rest

## Claude Code Integration (Phase 2)

HaBot can invoke Claude Code as a subprocess to dynamically generate checkers for unknown sites.

### How it works
- User sends an unknown URL → HaBot spawns `claude --print` in **plan mode** (read-only)
- Claude analyzes the site and proposes a checker plan
- User `/approve`s → HaBot spawns Claude in **edit mode** → Claude writes `checkers/{source}.py`
- Bot auto-restarts to pick up the new checker

### Generating a new checker
When asked to create a checker, follow this pattern:

1. **Create `checkers/{source}.py`** — subclass `Checker`, implement `source_name` property and `check(item_id)` method
2. **Call `register(YourChecker())` at module level** — auto-discovery handles the rest
3. **Update `url_parser.py`** — add a regex pattern mapping URLs to `(source, item_id)`
4. **Return a `StockResult`** — with `in_stock`, `price`, `name`, `url` fields

### Example: KSP checker structure
```python
from checkers import Checker, StockResult, register

class KSPChecker(Checker):
    @property
    def source_name(self) -> str:
        return "ksp"

    def check(self, item_id: str) -> StockResult:
        # Fetch data, parse response
        return StockResult(in_stock=True, price=65.0, name="Product", url="...")

register(KSPChecker())
```

### Session management
- Session ID persisted in `logs/claude_session.txt`
- `--resume` for multi-turn context continuity
- Flush creates summary in `.claude/last_session.md`

## Key design decisions
- Transition detection only (no duplicate alerts)
- State persisted to `state.json` (survives restarts)
- No Docker — designed for git+systemd deploy on RPi for fast Claude Code edits
