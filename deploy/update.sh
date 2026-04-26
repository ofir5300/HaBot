#!/usr/bin/env bash
# HaBot updater: git pull from origin/main; if requirements changed,
# reinstall deps; restart service only when there are new commits.
set -euo pipefail

cd /home/rpi/habot

VENV=/home/rpi/habot/.venv
PIP="$VENV/bin/pip"
PY="$VENV/bin/python"

OLD_HEAD=$(git rev-parse HEAD)
git fetch --quiet origin main
NEW_HEAD=$(git rev-parse origin/main)

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
  echo "habot-update: already at $OLD_HEAD, nothing to do"
  exit 0
fi

echo "habot-update: $OLD_HEAD -> $NEW_HEAD"

REQ_CHANGED=0
if ! git diff --quiet "$OLD_HEAD" "$NEW_HEAD" -- requirements.txt; then
  REQ_CHANGED=1
fi

git reset --hard origin/main

if [ "$REQ_CHANGED" = "1" ]; then
  echo "habot-update: requirements.txt changed, reinstalling"
  "$PIP" install -r requirements.txt
  # Playwright browser may need refresh when its package version moves
  "$PY" -m playwright install chromium || true
fi

sudo /bin/systemctl restart habot.service
echo "habot-update: restarted habot.service"
