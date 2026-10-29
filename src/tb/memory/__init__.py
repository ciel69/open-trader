"""Память случаев (WP15a, docs/spec_v2/08_case_memory.md): база опыта ДЛЯ АГЕНТА, не для человека.

Слои: cases (схема tb.case/1 и хранилище с хеш-цепочкой), features (признаки из журналов), similar (сходство Gower, чистая логика),
agg (агрегат и предупреждения n, чистая логика), lessons (уроки-гипотезы), review (схема tb.casereview/1 и валидатор), privacy,
migrate (миграция и sync), cli (`./tb memory ...`).
Память ничего не разрешает и не запрещает: разрешения только guard/Gate/agents.json. Пакет не импортирует tb.intel.items, tb.venues, tb.executor, tb.gateway.
"""
