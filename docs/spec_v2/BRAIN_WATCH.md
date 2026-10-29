# GET /api/brain/watch: «что видит мозг и чего ждёт» (контракт для вёрстки)

Чтение через слот EXT_API (`tb.brain.dashapi`, `server.py` не менялся). Только чтение, без секретов; заголовки новостей, названия и заметки не выводятся: только счётчики, возраст в секундах, enum и идентификаторы.
Источник: свежая сборка досье scope=start БЕЗ сохранения (в `state/briefs` ничего не пишется), health мозга (`runtime/brain/state.json`), задания `state/brain/jobs`, план дня, подписка `api`. Ответ проходит `scrub`. Сбой: `500 {"error":"watch_failed","kind":"..."}`. Обновлять раз в 10-15 с.

```json
{"ts": 1790000000.0,
 "brief": {"id": "B-20261028-1015-01", "ts": "2026-10-28T10:15:00Z", "built": true, "trunc": false},
 "state": {"code": "waiting_targets", "text": "ждёт целей: активных идей нет, горячих монет тут 3", "detail": {"hot": 3}},
 "blocks": {"market": {"present": true, "risk_off": false, "session": "europe", "blackout_in_min": null, "blackout_mode": null},
            "hot": {"present": true, "n": 3, "stale_s": null}, "struct": {"present": true, "n": 8, "stale_s": null, "regimes": {"range": 5, "trend_up": 3}, "file_n": 11, "age_s": 14, "syms": ["BTC", "CBRS"], "requested": ["XAN"], "requested_wait": ["XAN"]},
            "intel": {"present": true, "age_s": 120, "stale_s": null, "blackout": false},
            "news_digest": {"present": true, "n": 2, "age_s": 300, "stale": false},
            "ideas": {"present": false, "active": 0}, "recent": {"n": 8}},
 "dayplan": {"day_id": "D-20261028-3", "max_new_plans": 2, "regime": "range", "risks": ["blackout_soon"], "source": "llm"} ,
 "last_job": {"role": "position_manager", "ts": 1790000000.0, "status": "done", "outcome": "idle", "code": "IDLE_NO_PLAN", "types": ["connectivity"]},
 "next_wake": {"roles": {"position_manager": ["milestone"], "entry_scout": ["scanner_signal"]}, "jobs_per_hour": 12, "class_jobs_per_hour": {"3": 6},
               "role_jobs_per_hour": {"entry_scout": 6}, "subscriber_enabled": true, "kinds": ["hot_enter", "range_5m", "turnover_surge"]}}
```

`state.code` (приоритет сверху вниз): `brain_down` (защита по недоступности модели), `protect_only` (защита по бюджету и др.), `blackout` (окно новостей <= 60 мин; `detail.in_min`),
`blocked_by_dayplan` (план дня с `max_new_plans=0`; `detail`: regime, risks, source), `working` (есть планы, позиции или открытые задания), `waiting_targets` (активных идей нет; `detail.hot` = число горячих монет), иначе `working`.
`dayplan`, `last_job`, поля `null` допустимы. Поля `*_s` и `age_s` в секундах; `null` = нет данных.

Служебная запись `IDLE_NO_PLAN` в ленте решений (`/api/v2/decisions`): `kind=note`, `res.ok=true`, строка получает `idle:true`, `denied:false`, `noop:false`; в счётчик «Отказов Gate» (`day.rejects`) не входит; в UI нейтральный чип «ждёт».
Минимальная карточка: `static/v-decisions.js` (`applyWatch`, textContent, без внешних ресурсов) над лентой на вкладке «Решения»; полный дизайн за владельцем.
