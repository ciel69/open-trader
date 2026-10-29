"""Шлюз инструментов для агентов (WP11-A): каталог, реестр агентов, аудит, точка входа `call`.

Единственный путь агента к платформе. Пакет НЕ импортирует биржевой код, исполнитель, subprocess и сетевые модули (tests/test_layering.py).
Транспорты: in-process `gateway.call(agent_id, tool, args)` (API-демон) и MCP по stdio (`tb.mcp.server`).
"""
from tb.gateway.rpc import Gateway, call, tools_for  # noqa: F401
