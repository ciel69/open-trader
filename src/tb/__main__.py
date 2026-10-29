"""Диспетчер `./tb <группа> <команда> [аргументы]`: сопоставляет группу модулю и запускает его как __main__.

Профиль ключа Bybit и прокси-переменные обрабатывает tb.launcher (его зовут обёртки tb, tb.cmd, tb.ps1) ДО этого диспетчера.
`python -m tb ...` напрямую тоже идёт через launcher.
"""
import importlib
import os
import runpy
import sys

from tb import paths

# группа -> модуль. Для групп из GROUPS_SUB вторым словом выбирается подкоманда.
SIMPLE = {
    "guard": "tb.core.guard",
    "journal": "tb.core.journal",
    "ideas": "tb.core.ideas",
    "session": "tb.core.session",
    "portfolio": "tb.core.portfolio",
    "learning": "tb.core.learning",
    "bybit": "tb.venues.bybit",
    "watch": "tb.marketdata.watch",
    "volwatch": "tb.marketdata.volwatch",
    "hotscan": "tb.marketdata.hotscan",
    "context": "tb.marketdata.context",
    "history": "tb.marketdata.history",
    "funding": "tb.marketdata.funding_history",
    "collect": "tb.marketdata.collect",
    "paper": "tb.research.paper_donchian",
    "tick": "tb.legacy_engine.tick",
    "patterns": "tb.marketdata.patterns_scan",
    "dash": "tb.dash.server",
}
# v2 (SPEC_V2): группа -> модуль с функцией main(argv) -> int. Код возврата берётся из main; пока пакет не реализован,
# заглушка печатает «не реализовано» и возвращает 2.
V2 = {
    "plan": "tb.brain.plan_cli",
    "brief": "tb.brain.brief",
    "events": "tb.brain.events_cli",
    "exec": "tb.executor.main",
    "exp": "tb.brain.exp_cli",
    "brain": "tb.brain.api_daemon",
    "gateway": "tb.gateway.cli",
    "mcp": "tb.mcp.server",
    "llm": "tb.llm.cli",
    # платформа (WP11-B): "модуль:функция" (по умолчанию main)
    "start": "tb.platform.quick:main_start",       # простой запуск: полная платформа одной командой (paper, фон)
    "stop": "tb.platform.quick:main_stop",
    "restart": "tb.platform.quick:main_restart",
    "up": "tb.platform.supervisor:main_up",
    "down": "tb.platform.supervisor:main_down",
    "status": "tb.platform.supervisor:main_status",
    "logs": "tb.platform.supervisor:main_logs",
    "dispatch": "tb.platform.dispatcher",
    "sync": "tb.platform.syncsvc",    # автосинхронизация капитала читающим ключом read: run|once|status|confirm
    "intel": "tb.intel.cli",       # фактура и новости (WP13a)
    "control": "tb.control.cli",   # плоскость управления (WP12)
    "memory": "tb.memory.cli",     # память случаев (WP15a)
    "risk": "tb.control.risk_cmd",  # уровни риска (WP16): risk level show|set|list|check, risk init-key
    "notify": "tb.lifecycle.notify_cli",  # уведомления ОС: status|on|off|set|test (WP-N)
    "setup": "tb.setup_cli",        # помощник настройки владельца: ключи, токены (только Terminal.app, TTY)
    "smoke": "tb.smoke",            # сквозной прогон paper на фейковом LLM во временном корне
    "audit-public": "tb.audit.public",   # проверка перед публикацией репозитория (только чтение)
    "day": "tb.lifecycle.cli",      # жизненный цикл дня (WP14): status|list|report|checklist|preflight (только чтение)
}
FEED = {"start": [], "once": ["--once"], "selftest": ["--selftest"]}
RESEARCH = {
    "daily": "tb.research.research_daily",
    "hourly": "tb.research.research_hourly",
    "pairs": "tb.research.research_pairs",
    "pool": "tb.research.research_pool",
    "robustness": "tb.research.research_robustness",
    "compare": "tb.research.compare_venues",
    "backtest": "tb.research.backtest",
    "patterns": "tb.research.research_patterns",
    "volume": "tb.research.research_volume",
}

USAGE = """использование: ./tb <группа> <команда> [аргументы]
  bybit <read|trade|testnet> <balance|positions|info|fees|open|close|sync|pending|cancel|watch|scan> ...   площадка Bybit (ключ и прокси: обёртка ./tb)
  guard ... | journal <open|close|post|analyze|list|review> | ideas <add|list|mark|stats> | session <start|status|extend|stop> | portfolio <set|deposit|show> | learning <start|status|stop>
  feed <start|once|selftest> | watch | volwatch | hotscan | patterns scan --sym ETH [--tf 1h|4h|1d] | context | paper | history | funding | collect
  research <%s>
  plan <submit|check|amend|stop|close|cancel|hold|release|adopt|list|show|schema|template|from-idea> ...   планы (мозг -> _inbox; v2)
  brief [--scope start|event|plan:ID] | events [--follow|ack|stats] | exp <list|stats|compare>   досье, события, статистика экспериментов (v2)
  exec <run|status|halt|resume|reconcile|verify|replay> ...   исполнитель планов (v2; run --profile trade|testnet|paper [--exchange ID]: по одному исполнителю на биржу, docs/EXCHANGES.md)
  brain run | enable | disable | status | use ЦЕЛЬ ПРОФИЛЬ | rollback | reload | jobs | probe | check | verify | eval | profile ... | role ...   API-мозг (WP11-C2, docs/BRAIN.md)
  start | stop | restart | status   платформа одной командой: все сервисы (с мозгом, если он настроен), в фоне, профиль paper (docs/QUICKSTART.md)
  up [--profile paper|testnet|trade] [--observe] [--services exec,scan,dash,dispatch,mcp,brain] [--detach] | down | status [--json|--line] | logs [сервис] [-f]   платформа (WP11-B)
  intel <status|fetch|show|sources|calendar|verify> ...   фактура и новости (WP13a); verify/fetch ходят в сеть: запускайте в обычном терминале
  sync <run|once|status|confirm>   автосинхронизация капитала (баланс читающим ключом read -> portfolio.json); confirm принимает большое расхождение
  dispatch <run|once|check|queue ID [--ack N]|stats|log>   диспетчер подписок на шину событий
  gateway <tools|agents|agent add|token new|call|audit|check> ...   шлюз инструментов агентов (WP11-A) | mcp serve --agent ID   MCP-сервер по stdio
  llm <providers|check [--live]|usage [--days N] [--by profile|provider|model|role]|presets|provider add|update|remove|enable|disable|model set|remove|key set|status|clear>   слой LLM: провайдеры, шаблоны, ключи (скрытый ввод), проверка (--live: живой запрос), расход
  risk level <show|list|check [--apply]|set N --confirm "..."> | risk init-key   уровни риска 1-5 (WP16; set и init-key только владелец в Terminal)
  memory <similar|stats|add|show|sync|migrate|verify|review-queue|review|lesson|backtest|compare> ...   память случаев: похожие ситуации, уроки-гипотезы, ретро-проверка (WP15a, WP15b)
  day <status|list|report [ID]|checklist|preflight> [--json]   жизненный цикл дня: состояние, итоги, сухой прогон чек-листа закрытия, preflight (WP14; только чтение)
  notify <status|on|off|set КАТЕГОРИЯ on|off|set quiet HH:MM-HH:MM|set quiet off|test>   уведомления ОС: мастер, категории, тихие часы (docs/NOTIFICATIONS.md)
  control <token new|status|gates|audit|cmd|run>   плоскость управления: ключ, токен входа, гейты live, аудит, сервис ctl (WP12)
  setup [--check]   помощник настройки: ключ подписи, токены, ключ DeepSeek (скрытый ввод; другие провайдеры: llm key set; docs/PROVIDERS.ru.md), права, итоговая таблица (docs/QUICKSTART.md)
  smoke [--max-seconds N] [--keep]   сквозной прогон paper (фейковый LLM, временный корень, публичные данные Bybit); docs/spec_v2/SMOKE_RESULT.md
  dash [--port 8765] [--open]   локальный дашборд (127.0.0.1): чтение; кнопки управления: paper без токена, опасное (live) по токену один раз
  audit-public [--json] [--root ПУТЬ] [--term СЛОВО] [--strict]   проверка дерева перед публикацией: секреты, личные данные, крупные файлы, лицензия (только чтение; docs/PUBLISH_CHECKLIST.md)
  tick ...   (старый движок) | test   (все тесты)""" % "|".join(RESEARCH)


def run(module, args):
    sys.argv = [module.replace(".", "/") + ".py"] + list(args)
    runpy.run_module(module, run_name="__main__", alter_sys=True)


def run_v2(module, args):
    """Запускает CLI-группу v2: импортирует модуль и вызывает main(args); -> код возврата (2, если main нет)."""
    module, _, name = module.partition(":")
    fn = getattr(importlib.import_module(module), name or "main", None)
    if not callable(fn):
        print("не реализовано: %s" % module, file=sys.stderr)
        return 2
    return int(fn(list(args)) or 0)


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 2
    group, rest = argv[0], argv[1:]
    if group == "test":
        import unittest
        suite = unittest.defaultTestLoader.discover(os.path.join(paths.root(), "tests"), top_level_dir=os.path.join(paths.root(), "tests"))
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
    if group == "feed":
        if not rest or rest[0] not in FEED:
            print("feed: start | once | selftest", file=sys.stderr)
            return 2
        run("tb.marketdata.feed", FEED[rest[0]] + rest[1:])
    elif group == "research":
        if not rest or rest[0] not in RESEARCH:
            print("research: " + " | ".join(RESEARCH), file=sys.stderr)
            return 2
        run(RESEARCH[rest[0]], rest[1:])
    elif group in V2:
        return run_v2(V2[group], rest)
    elif group in SIMPLE:
        run(SIMPLE[group], rest)
    else:
        print("неизвестная группа: %s\n%s" % (group, USAGE), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    from tb import launcher
    sys.exit(launcher.main(sys.argv[1:]))
