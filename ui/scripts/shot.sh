#!/bin/sh
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
  --window-size="${4:-1600},${5:-1000}" --virtual-time-budget=4000 \
  --screenshot="shots/$1.png" "http://localhost:5173/?theme=${3:-light}#/$2" 2>/dev/null
