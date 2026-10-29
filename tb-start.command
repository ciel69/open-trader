#!/bin/sh
# Холодный старт платформы одним двойным щелчком (macOS): вся платформа в фоне (профиль из config/local/platform.json, иначе paper), затем страница дашборда.
# Реальные деньги запускаются ТОЛЬКО кнопкой «Старт» в дашборде после входа по токену; профиль trade через start не включается.
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
# первый раз: ./tb setup (ключи/токены), затем docs/QUICKSTART.md
./tb start
open "http://127.0.0.1:8765/#control" 2>/dev/null || true
