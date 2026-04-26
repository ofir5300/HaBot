# HaBot deploy (Raspberry Pi, systemd)

Runs HaBot as a systemd service on the Pi (`rpi@192.168.68.58`), with a daily
timer that does `git pull` from `origin/main` and restarts on new commits.

## Layout
- `habot.service` — main long-running service (`python -m main`)
- `habot-update.service` — oneshot updater (runs `update.sh`)
- `habot-update.timer` — fires `habot-update.service` 2min after boot, then daily
- `update.sh` — git fetch/reset, optional `pip install`, optional `playwright install`, then `systemctl restart habot.service`

The env file lives at `/home/rpi/habot.env` — **outside** the checkout — so
`git reset --hard` cannot wipe it.

## Bootstrap (once)

SSH in:
```bash
sshpass -p 'yupte100..' ssh rpi@192.168.68.58
```

System packages:
```bash
echo 'yupte100..' | sudo -S apt-get update
echo 'yupte100..' | sudo -S apt-get install -y git python3-venv
```

Clone + venv + deps:
```bash
git clone https://github.com/ofir5300/HaBot.git /home/rpi/habot
cd /home/rpi/habot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

Env file (chmod 600 — never commit):
```bash
cat > /home/rpi/habot.env <<'EOF'
TELEGRAM_BOT_TOKEN=...
ALLOWED_CHAT_IDS=...
STOCK_CHECK_INTERVAL=300
CHECK_INTERVAL=60
FLIGHT_CHECK_INTERVAL=60
EOF
chmod 600 /home/rpi/habot.env
```

Sudoers drop-in so the timer can restart without a password:
```bash
echo 'rpi ALL=(root) NOPASSWD: /bin/systemctl restart habot.service' \
  | sudo tee /etc/sudoers.d/habot
sudo chmod 440 /etc/sudoers.d/habot
sudo visudo -c
```

Install units:
```bash
sudo cp deploy/habot.service deploy/habot-update.service deploy/habot-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now habot.service habot-update.timer
```

Verify:
```bash
systemctl status habot.service
journalctl -u habot.service -n 50 --no-pager
systemctl list-timers habot-update.timer
```

## Updating

After merging a PR to `main`, the Pi will pick it up at the next daily timer
fire. To deploy immediately:
```bash
sudo systemctl start habot-update.service
journalctl -u habot-update.service -n 30 --no-pager
```

## Gotchas
- Pi runs Debian Bullseye + Python 3.9. PEP 604 `X | None` annotations evaluated at import time will crash — add `from __future__ import annotations` to the offending module.
- Don't run a second `python main.py` against the same Telegram bot token (locally or elsewhere) — `getUpdates` is single-instance and they will fight.
- `state.json` and `.env` must stay in `.gitignore` so `git reset --hard` preserves them.
