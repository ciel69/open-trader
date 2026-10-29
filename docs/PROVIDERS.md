# Connecting an LLM provider

[Русская версия](PROVIDERS.ru.md) | back to [README](../README.md)

The platform runs without any LLM: executor, scanner, dashboard and the paper profile work, and the day starts with a conservative plan. The **brain** (the LLM roles that write the morning plan, manage open positions and review closed trades) switches on only when three things are true:

1. at least one **provider** is enabled and has an **API key**;
2. a **profile** (provider + model + parameters) is assigned to the **roles** (default assignments ship in `config/brain.json`);
3. the **subscriber `api`** is enabled (it is off in the shipped config, so a fresh clone never calls an LLM by itself).

> **Honest status.** Every provider template is **not verified with a live key**: model names, base URLs and prices come from memory/docs and must be checked by you (`./tb llm check --provider ID --live`). Only **Anthropic** and **DeepSeek** have native adapters. Everything else (OpenAI, Gemini, Groq, Mistral, OpenRouter, Ollama, LM Studio, your own endpoint) goes through one generic **OpenAI-compatible** adapter (`/chat/completions`, tool calls, JSON mode) that was tested only against a local fake server. No live brain has been validated in production; prompts and response formats were tested on a fake LLM. Start on `paper`, with a cheap model and small budgets.

All commands run from the repository root. Keys never go into the repository and are never printed: they live in `~/.config/traider-bot/` (Windows: `%USERPROFILE%\.config\traider-bot\`; override with `TB_KEYS_DIR`).

## 1. Pick a template

```
./tb llm presets          # JSON: id, adapter, base_url, models, key file/env, "added" (already in your registry)
./tb llm providers        # what is configured now: enabled?, key present? (only the mask ...1d1c), limits, prices
```

| id | adapter | default base URL | model keys | key file / env var | in registry by default |
|---|---|---|---|---|---|
| `deepseek` | native | `https://api.deepseek.com` | `flash`, `pro` | `deepseek.env` / `DEEPSEEK_API_KEY` | yes, **enabled** |
| `anthropic` | native | `https://api.anthropic.com` | `haiku`, `sonnet` | `anthropic.env` / `ANTHROPIC_API_KEY` | yes, disabled |
| `local` (Ollama) | openai-compatible | `http://127.0.0.1:11434/v1` | `qwen` (built-in) / `main` (template) | none | yes, disabled |
| `openai` | openai-compatible | `https://api.openai.com/v1` | `full`, `mini` | `openai.env` / `OPENAI_API_KEY` | no |
| `gemini` | openai-compatible | `https://generativelanguage.googleapis.com/v1beta/openai` | `flash`, `pro` | `gemini.env` / `GEMINI_API_KEY` | no |
| `groq` | openai-compatible | `https://api.groq.com/openai/v1` | `main` | `groq.env` / `GROQ_API_KEY` | no |
| `mistral` | openai-compatible | `https://api.mistral.ai/v1` | `main` | `mistral.env` / `MISTRAL_API_KEY` | no |
| `openrouter` | openai-compatible | `https://openrouter.ai/api/v1` | `auto` | `openrouter.env` / `OPENROUTER_API_KEY` | no |
| `lmstudio` | openai-compatible | `http://127.0.0.1:1234/v1` | `main` | none | no |

The "model key" is a short local alias; the real name sent to the API is the model's `name` (see step 3).

## 2. Add the provider (skip for DeepSeek, Anthropic and Ollama: they are already there)

CLI:
```
./tb llm provider add gemini --preset gemini      # written to config/local/providers.json, added DISABLED
```
Dashboard: **System -> Settings -> Providers -> "Add provider from template" -> "Add disabled"** (needs the dashboard token once: it is printed on first `./tb start`, or `./tb control token new`).

![Settings: AI connections](images/settings.png)

## 3. Key, model name, enable (order matters: a provider without a key cannot be enabled)

```
./tb llm key set gemini            # hidden prompt (needs a real terminal); writes ~/.config/traider-bot/gemini.env with mode 0600
./tb llm model set gemini flash --name gemini-2.5-flash --price-in 0.3 --price-out 2.5 --tools --json-mode
./tb llm provider enable gemini
./tb llm check --provider gemini                  # offline: config valid, key file present
```
Other ways to give the key:
- **File**: create `~/.config/traider-bot/<provider>.env` with one line `GEMINI_API_KEY=...` (no quotes, `chmod 600`). `./tb setup` does this for DeepSeek with hidden input.
- **Environment variable** with the same name, exported in the shell that runs `./tb start` (works if no file exists; the file is the more reliable way).
- **Dashboard**: provider card -> "Enter key" (write-only, never shown again; only for LLM providers). Exchange keys are **never** accepted by the dashboard, only in the terminal (`./tb setup`).

Fix names and prices for your account: take the exact model id from the provider's model list (`GET /models`) and your price list. Wrong names are the most common reason for the first live failure. `--price-in/--price-out` are USD per 1M tokens; they drive the budget meter, so a zero price disables cost protection for that model.

## 4. Profile and roles

A **profile** = provider + model + params (temperature, max tokens, thinking). A **role** has a chain: `primary` and ordered `fallback`s.

```
./tb brain profile set gem-flash --provider gemini --model flash --set temperature=0.1 --set max_tokens=2500
./tb brain profile list

./tb brain role list                                              # roles, chains, budgets, today's spend
./tb brain role chain position_manager --primary gem-flash --fallback ds-flash
./tb brain use all gem-flash                                      # or: one profile for ALL roles at once
```

| role | team/shared | when it wakes | what it does |
|---|---|---|---|
| `morning_analyst` | team `A` | day start (job), macro blackout, risk level change | morning briefing and day plan |
| `position_manager` | team `A` | entry filled, milestone, session ending, connectivity | hold / tighten stop / close an open position |
| `entry_scout` | team `A` | scanner signal | proposes (or skips) an entry; paper only |
| `reviewer` | shared | position closed | reviews the closed trade |
| `day_auditor` | shared | day close (job) | day summary, carry decisions |
| `news_reader` | shared | called on news headlines | the quarantined reader of third-party text (strict JSON only) |

Role names: `position_manager` means the live team's role, `A.position_manager` names the team explicitly, `shared.news_reader` a shared role. Chain and profile changes need a signing key (created automatically by `./tb start`, or `./tb control init-key`). The same screen exists in the dashboard (**Settings -> Roles**, change needs the token):

![Settings: roles](images/settings.png)

Mixing is allowed and sensible: a cheap model for `news_reader`/`reviewer`/`entry_scout`, a stronger one for `morning_analyst`/`position_manager`, a second provider as `--fallback` so a 429/5xx from one vendor does not blind the brain.

## 5. Check before you spend more than cents

```
./tb brain check                              # config/brain.json, roles, providers.json: structure (no network)
./tb brain probe gem-flash                    # offline probe record
./tb llm check --provider gemini --live       # LIVE: GET /models (is your model name there?) + one tiny completion. Costs a fraction of a cent.
./tb brain probe --all --live                 # LIVE per profile
./tb brain eval                               # regression prompts on a FAKE LLM (no network, no cost)
```
`--live` uses your key and your money. `./tb brain eval ... --live --confirm` (owner only, needs a terminal) runs the full golden set on the real model and is the only thing that can put the `live_ok` label on a profile. Without `live_ok` a profile must not manage live positions.

## 6. Turn the brain on

The subscriber `api` is off in the shipped `config/subscriptions.json`. Enable it with one command, no hand-written JSON:

```
./tb brain enable        # writes the personal layer config/local/subscriptions.json (git-ignored, merged over config/); safe to repeat
```
It also prints what the brain still lacks (provider key, profile/roles) and the next command. `./tb brain disable` turns it off again.

Then:
```
./tb start               # or ./tb restart if the platform already runs
./tb brain status        # assignments, revision, today's spend, queue
./tb logs brain
```
`./tb start` prints exactly one line when the brain stays off, for example:
`мозг выключен: подписчик api выключен (как включить: ...)`. The reasons, in the order they are checked:

| line says (reason) | fix |
|---|---|
| `подписчик api выключен` (subscriber api off) | `./tb brain enable` (step 6) |
| `профиль/модель мозга не настроены` | `config/brain.json` has no live team/roles: `./tb brain reload --from-config` or `./tb brain role list` |
| `провайдер X неизвестен или с ошибкой` | `./tb llm check`; fix `config/local/providers.json` |
| `провайдер X выключен` | `./tb llm provider enable X` (needs the key first) |
| `нет ключа провайдера X (файл x.env)` | step 3: key file or env var |

The brain starts as soon as **one** profile in the live team's chains is usable (provider enabled, key present). Other roles whose chain points to a keyless provider fall through to the next entry; assign everything to your provider with `./tb brain use all <profile>` to avoid noise.

After it starts, open the dashboard: **Brain -> Decisions** shows the card "Brain: working" and the log of decisions (who, what, why, result):

![Brain card and decision log](images/brain-decisions.png)

## 7. Your own OpenAI-compatible endpoint (proxy, vLLM, company gateway, LiteLLM...)

Any server that implements `POST {base_url}/chat/completions` works. Models are declared in the provider entry:

```
./tb llm provider add myproxy \
  --set 'adapter="openai_compat"' \
  --set 'base_url="https://llm.example.com/v1"' \
  --set 'key_file="myproxy.env"' --set 'key_env="MYPROXY_API_KEY"' \
  --set 'models={"main":{"name":"my-model","tools":true,"json_mode":true,"price":{"in":0.5,"out":1.5}}}'
./tb llm key set myproxy
./tb llm provider enable myproxy
./tb brain profile set my-main --provider myproxy --model main
./tb brain role chain position_manager --primary my-main
```
Notes: `--set` takes `field=JSON` (strings need the inner quotes). `adapter` accepts `openai_compat` (also written as `openai-compatible`). Capability flags matter: a role needs tool calls (`tools:true`) and the brain prefers `json_mode:true`; if your server lacks them, expect `BAD_FORMAT` skips. Some servers want `max_completion_tokens` instead of `max_tokens` (model field `max_tokens_field`) or reject `temperature` (`no_temperature`); see `docs/spec_v2/SETTINGS_API.md` section 1 for all fields. `base_url` must start with `http://` or `https://`; use `https://` for anything that is not on your own machine.

## 8. Local model (Ollama, LM Studio)

No key, no cost, **weaker output**: small local models often fail the strict JSON schema, which shows up as `BAD_FORMAT` in `./tb logs brain`. Use them for `news_reader`/`entry_scout` experiments first, not for position management.

```
ollama pull qwen2.5:14b          # in your own terminal; the Ollama server must be running
./tb llm model set local qwen --name qwen2.5:14b --tools --json-mode     # built-in provider "local" -> http://127.0.0.1:11434/v1
./tb llm provider enable local
./tb brain profile set loc-qwen --provider local --model qwen
./tb brain role chain entry_scout --primary loc-qwen --fallback ds-flash
```
LM Studio: `./tb llm provider add lmstudio --preset lmstudio`, enable "Local Server" in the app, set the model name shown by `GET http://127.0.0.1:1234/v1/models` with `./tb llm model set lmstudio main --name NAME`.

## Money and safety

- **Budgets** (all hard stops, USD): per provider in `limits` (`day_usd`, `hour_usd`, `rpm`, `concurrent`; defaults $3/day and $1/hour), overall in `config/brain.json` -> `budget` (`day_usd` 2.0, `job_usd` 0.05 per job, `jobs_per_hour` 12, warn at 80%), per role: `./tb brain role budget entry_scout --day-usd 0.3`. When a budget is exhausted the job is skipped or falls back; the brain never spends past it. Watch it with `./tb llm usage --days 7 --by role` and the dashboard (**System**).
- **Expensive models**: a "strong" model (`sonnet`, `pro`) on every scanner signal can burn the budget in hours. Keep `entry_scout` on a cheap model, keep `jobs_per_hour` low, set role budgets.
- **Paper first.** The agent `api` is paper-only in `config/agents.json` (`modes: ["paper"]`). The brain cannot open a live trade, change limits, leverage or keys. It proposes; guard, risk levels and the KILL switch are code.
- **Live is the owner's decision**, per `docs/CONTROL.md` and `docs/RUNBOOK.md`: gates, token, confirm phrases, and `live_ok` after a live eval. Swapping the model of a live role needs a terminal and a typed phrase. Profit is not proven (`docs/RESEARCH.md`); a better model is not a reason to raise risk.
- Third-party text (news) is read only by the quarantined reader; the trading roles never see it.

## Common errors

| symptom | cause | fix |
|---|---|---|
| `E_NO_KEY` on `provider enable` | key not stored yet | `./tb llm key set ID` or key file, then enable |
| `E_PROVIDER_OFF` on `profile set` | provider disabled | enable it first (needs key) |
| `E_SWAP_NO_KEY` on `role chain` / `use` | no signing key | `./tb start` once (auto) or `./tb control init-key` |
| `E_SWAP_PROFILE ... неизвестен или отключён` | profile does not exist / provider off | `./tb brain profile list`, create it |
| `мозг выключен: ...` on `./tb start` | see the table in step 6 | follow the "how to enable" hint |
| `--live` check: `auth` / 401 | wrong key, or key for another endpoint | re-enter: `./tb llm key set ID` |
| `--live` check: model not in list / 404 | wrong `name` | take it from `GET /models`, `./tb llm model set ... --name` |
| `BAD_FORMAT` in brain log, decisions skipped | model does not follow the JSON schema, or no tool/JSON support | stronger model, `json_mode`/`tools` flags, lower temperature, fallback |
| `E_ELEVATE` / 401 in the dashboard | changing settings needs the token | enter it once (`./tb control token new` if lost) |
| budget state `exhausted` | day/hour limit hit | `./tb llm usage`, raise the limit in `config/local/providers.json` / `config/local/brain.json` consciously, or wait |

More: `docs/BRAIN.md` (runtime, swap, fallback, files), `docs/ROLES.md` (prompts and schemas), `docs/spec_v2/07b_roles_providers.md` (design), `docs/spec_v2/SETTINGS_API.md` (settings API contract).
