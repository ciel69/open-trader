"""Исполнитель v2 (WP5): детерминированный демон без LLM, единственный держатель торгового ключа в целевой схеме.

Контракты: docs/spec_v2/02_executor.md, 06_contracts.md. Портов биржи: `ports.py` (VenuePort, MarketFeed, PortError).
Пакеты brain/logic/core/dash НЕ импортируют этот пакет (tests/test_layering.py).
"""
