@echo off
rem Холодный старт платформы (Windows): вся платформа в фоне (профиль из config\local\platform.json, иначе paper), затем страница дашборда.
rem Реальные деньги запускаются ТОЛЬКО кнопкой "Старт" в дашборде после входа по токену; профиль trade через start не включается.
cd /d "%~dp0"
rem Первый раз: tb.cmd setup (ключи и токены), затем docs\QUICKSTART.md
call tb.cmd start
start "" "http://127.0.0.1:8765/#control"
