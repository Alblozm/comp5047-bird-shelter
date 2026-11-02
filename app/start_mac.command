#!/bin/bash
# Double-click to start the Shelter console (macOS).
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3：https://www.python.org/downloads/"
  read -n 1 -s -r -p "按任意键关闭"; exit 1
fi
python3 shelter_console.py "$@"
