#!/usr/bin/env bash
# fin-asst -- double-click launcher for macOS.
#
# Downloaded files that you double-click get a one-time macOS security
# warning ("cannot be opened because it is from an unidentified developer").
# If you see that: right-click (or Control-click) this file and choose
# "Open" instead of double-clicking -- you only need to do that once.
set -e
cd "$(dirname "$0")"

clear 2>/dev/null || true
echo "=================================================="
echo "  fin-asst"
echo "=================================================="
echo
echo "Setting things up. The first run installs a couple of small tools and"
echo "can take a minute or two; after that it starts in a few seconds."
echo
echo "This window has to stay open while the app is running -- closing it"
echo "stops the app. Your data is not affected either way."
echo

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (the tool this app uses to manage itself)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo
  echo "Could not install uv automatically. Ask whoever shared this app with"
  echo "you for help, or see: https://docs.astral.sh/uv/getting-started/installation/"
  echo
  read -n 1 -s -r -p "Press any key to close this window..."
  echo
  exit 1
fi

export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/fin-asst"

echo "Installing the app..."
uv sync

echo "Preparing your personal database (this stays on your computer only)..."
uv run finasst init >/dev/null

echo
echo "Starting fin-asst -- your browser will open automatically in a moment."
echo "To stop the app later, come back to this window and press Ctrl-C,"
echo "or just close the window."
echo

( sleep 2 && open "http://127.0.0.1:8000" ) &

uv run finasst serve

echo
read -n 1 -s -r -p "fin-asst has stopped. Press any key to close this window..."
echo
