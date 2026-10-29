# open-trader

[Русская версия](README.ru.md) | License: MIT

> ## DISCLAIMER (read first)
> - **Experimental software. Not financial advice.** Nothing here is a recommendation to buy, sell or hold anything.
> - **Profitability is NOT proven.** The research in this repository (`docs/RESEARCH.md`) tested rule-based strategies and found no edge that survives costs and robustness checks.
> - **Real money is at risk.** Perpetual futures with leverage can lose your whole deposit quickly. Use the `paper` profile first and never trade money you cannot afford to lose.
> - **Provided "as is", without warranty** (see `LICENSE`). You are solely responsible for your keys, your configuration and your trades.
> - Windows support is written but **not verified** on a real Windows machine. Some data sources (news, macro) are **not verified** either.

## What it is

An autonomous trading platform for USDT-margined perpetual futures on Bybit, with two parts:

1. A **deterministic executor** that holds the only trading key, checks every plan against hard limits, places orders with a stop and a take-profit, and watches positions on a one-second scale.
2. An **LLM "brain"** that reads a compact briefing, proposes plans and manages open positions, but only through a fixed **tool gateway**. The brain proposes, the code verifies.

The default profile is `paper`: it trades on paper against real public Bybit prices. Live trading is an explicit, gated owner decision.

## Safety principles

- The brain **never sees keys or files**. It gets a fixed catalog of 19 tools (`docs/GATEWAY.md`) with per-agent rights and limits; there are no shell, network, filesystem or direct-order tools.
- **Guard and risk levels live outside the LLM.** Size, leverage and margin are computed by code from capital and a risk level (1-5); an agent never sets them.
- **A stop can only be tightened**, never widened. No averaging down, no martingale.
- **KILL switch**: `state/KILL` halts trading; only a human can clear it (`./tb exec resume`).
- **Paper first**: the default profile is paper; live needs explicit gates in the control plane (`docs/CONTROL.md`).
- **Third-party text is data, not instructions.** News and headlines are read only by a **quarantined reader** (a separate cheap model call that can only answer with strict JSON); the trading brain never sees raw news (`docs/NEWS.md`).
- Dashboard listens on `127.0.0.1` only; control actions are signed and audited; paper needs no login, dangerous (live) actions need the control token once per browser.
- Everything is logged (decisions, gateway audit, events) so each action can be explained afterwards.

## Architecture

```
 news/RSS --> intel --> [quarantined reader] --> digest (numbers/ids only)
                                                   |
 Bybit public WS --> feed/scan --> events bus -----+--> briefing (dossier)
                                                   |
                         +-------------------------v-------------------+
                         |  BRAIN (LLM roles: morning_analyst,         |
                         |  position_manager, reviewer, day_auditor)   |
                         |  chat agent (Claude via MCP) / API daemon   |
                         +-------------------------+-------------------+
                                  tool calls only  |  (no keys, no files)
                         +-------------------------v-------------------+
                         |  GATEWAY: 19 tools, rights, limits, audit    |
                         +-------------------------+-------------------+
                                       plans (inbox/outbox)
                         +-------------------------v-------------------+
                         |  EXECUTOR (deterministic): guard + risk      |
                         |  levels + KILL + protections; only holder    |
                         |  of the trading key                          |
                         +------------+------------------+--------------+
                                      |                  |
                              paper port (default)   live/testnet port
                                                      Bybit REST/WS
 supervisor (`./tb up`) runs the services; dashboard + control plane on 127.0.0.1
```
Details: `docs/ARCHITECTURE.md`, `docs/DESIGN_V2.md`, `docs/SPEC_V2.md`.

## Status and honest limitations

- Experimental. It has been run on paper only; a live LLM brain has not been validated in production (prompts and response formats were tested on a fake LLM; the live evaluation is the owner's job).
- **No proven edge.** Our own research (`docs/RESEARCH.md`) tested minute trend rules, hourly breakout, pairs, daily momentum, funding contrarian, nine candle/level hypotheses and volume-after-impulse. None passed. The value of the system is risk discipline and measurement of hypotheses, not a promise of income.
- News sources are unverified until you run `./tb intel verify`; the news reader has not been tested on live text.
- Slow market data (derivatives, macro) is optional and not started by default.
- Windows (`tb.cmd`, `tb.ps1`, `docs/WINDOWS.md`) is not verified. macOS is the tested platform.
- Documentation is mostly in Russian; the README, SECURITY, CONTRIBUTING are in English.

## Requirements

- **Python 3.9 or newer** (`python3 --version`). No external dependencies: standard library only, no `pip install`.
- macOS or Linux (tested on macOS). Windows: `tb.cmd` / `tb.ps1` exist but are **not verified** (`docs/WINDOWS.md`).
- Internet access: the paper profile trades on paper against **real public Bybit prices** (WebSocket and REST, public endpoints). No Bybit account or key is needed for paper.
- An LLM provider key is **optional**: the platform, dashboard and paper work without a live brain. The brain needs a provider (see below).

## Quick start (paper)

**Paper** means simulated trading: orders are filled by a local paper executor at real market prices; no exchange key is used and no money moves. Run everything in a terminal from the repository root.

```
git clone <this repository> && cd <repository folder>   # or unzip it; then: chmod +x tb   (a zip may drop the executable bit)
./tb help                  # sanity check: prints the command list (needs python3 >= 3.9)
./tb setup --check         # optional: readiness table (what is missing); ./tb setup asks for keys with hidden input
./tb start                 # the whole platform in the background, profile paper
./tb status                # services, heartbeat, brain line
```
`./tb start` creates the signing key and the dashboard token itself on first run (the token is printed once; keep it) and prints the dashboard link and next steps. Then:

1. Open **http://127.0.0.1:8765/** (local only; paper needs no login, the token is asked once, only for dangerous actions and for changing settings).
2. **System -> Control -> Start**: mode `paper`, session type (`intraday`), minutes, run. The start goes through steps S1-S9 (tab **Day**).
3. The brain is **off** until you connect an LLM provider (next section). Until then `./tb start` prints one line `мозг выключен: <reason> (как включить: ...)` and everything else works: dashboard, scanner, plans, paper executor, journal.

Stop: `./tb stop` (restart: `./tb restart`). Emergency: `./tb exec halt --flatten`. If you pressed Ctrl+Z in the terminal that started the supervisor, `./tb start|stop|restart` resume it by themselves. Another port: put `{"svc": {"dash": {"port": 8780}}}` into `config/local/platform.json`. Full guide: `docs/QUICKSTART.md` (Russian); operations: `docs/RUNBOOK.md`. Self-check on a temporary root with a fake LLM (uses public Bybit data, touches nothing of yours): `./tb smoke`.

## Connect an LLM provider

Details, table of templates and troubleshooting: **[docs/PROVIDERS.md](docs/PROVIDERS.md)** ([RU](docs/PROVIDERS.ru.md)). Shortest path for a provider that is already in the registry (DeepSeek; Anthropic works the same way with `anthropic` and profile `an-sonnet`):

```
./tb llm presets                                 # templates: anthropic deepseek openai gemini groq mistral openrouter local lmstudio
./tb llm key set deepseek                        # hidden prompt; stored in ~/.config/traider-bot/deepseek.env (outside the repo)
./tb brain enable                                # turns the brain subscriber on (writes config/local/, prints what is still missing)
./tb llm check --provider deepseek               # offline config check; add --live for one tiny paid request
./tb restart                                     # or ./tb start; ./tb brain status shows the assignments
```
Another vendor (here Gemini): `./tb llm provider add gemini --preset gemini`, `./tb llm key set gemini`, `./tb llm provider enable gemini`, `./tb brain profile set gem-flash --provider gemini --model flash`, `./tb brain use all gem-flash`. Any OpenAI-compatible URL and local models (Ollama, LM Studio) are covered in `docs/PROVIDERS.md`. **All templates are unverified with a live key; only Anthropic and DeepSeek have native adapters.** Keep to paper and small budgets (`config/brain.json` -> `budget`). The same screens exist in the dashboard (**System -> Settings -> Providers / Roles**).

## What you will see

All screenshots below are synthetic fixture data (`tests/fixtures_v2/gen.py`), not a real account. The dashboard UI language is Russian. How to regenerate: `docs/images/README.md`.

| | |
|---|---|
| ![Overview](docs/images/overview.png) | **Now**: capital, risk, open position with stop/take, plans in work, limits headroom, events |
| ![Brain decisions](docs/images/brain-decisions.png) | **Brain -> Decisions**: brain status card and the log of every decision (who, what, why, result) |
| ![Plans](docs/images/plans.png) | **Brain -> Plans**: board of plans (waiting / watching / in trade / closed), entry conditions, nearest trigger |
| ![Trades](docs/images/trades.png) | **Trades**: statistics, cumulative PnL, exit reasons, history |
| ![Providers](docs/images/settings-providers.png) | **System -> Settings -> Providers**: add a provider from a template, enter a key (write-only), enable, check |
| ![Roles](docs/images/settings-roles.png) | **System -> Settings -> Roles**: which profile serves which role, fallbacks, budgets |

## Troubleshooting

| problem | what to do |
|---|---|
| `./tb: Permission denied` or `command not found: python3` | `chmod +x tb`; install Python 3.9+; or run `python3 -m tb.launcher help` with `PYTHONPATH=src` |
| Port 8765 is busy / dashboard does not open | `./tb status`, `./tb logs dash`; change the port in `config/local/platform.json` (see Quick start) and `./tb restart` |
| `./tb status` shows the platform "frozen" or `start`/`stop` hangs | the supervisor was suspended by Ctrl+Z (state T). `./tb start`, `stop`, `restart` and `down` send it SIGCONT themselves; if they cannot, they print the manual `kill -CONT <pid>; kill -TERM <pid>` |
| `мозг выключен: ...` / brain does nothing | no subscriber `api`, no provider key, provider disabled or no profile: the line says which; follow `docs/PROVIDERS.md` section 6 |
| `E_ELEVATE` or a token prompt in the dashboard | the action is "dangerous" (live, changing settings, keys): enter the dashboard token once; lost: `./tb control token new` |
| Commands fail with network/DNS errors, WebSocket never connects | a sandbox, VPN or firewall blocks outbound access; run in a plain terminal. Service proxies are disabled on purpose for exchange traffic (`docs/WINDOWS.md`, `docs/RUNBOOK.md`) |
| Windows | not verified on a real machine; follow `docs/WINDOWS.md` and please report what breaks |
| Something else | `./tb status`, `./tb logs [exec|scan|feed|dispatch|ctl|dash|brain]`, `./tb day status`, `./tb setup --check`; `./tb smoke` checks the whole chain on a temporary root |

## Repository layout

```
src/tb/        the package (stdlib only): core, executor, gateway, mcp, brain, llm, control, lifecycle, intel, memory, marketdata, venues, platform, dash, research, audit
config/        settings (no secrets); examples with neutral values: examples/config/
docs/          documentation (index: docs/INDEX.md), design notes: docs/spec_v2/
tests/         unit and scenario tests (fixtures are synthetic)
tb, tb.cmd, tb.ps1   entry points (macOS/Linux, Windows cmd, PowerShell)
state/ runtime/ data/   created at run time; private, never committed
```
Your keys live outside the repository, in `~/.config/traider-bot/`.

## Documentation

Start with `docs/INDEX.md`. Key pages: `PROVIDERS` (connect an LLM), `QUICKSTART`, `RUNBOOK`, `ARCHITECTURE`, `GATEWAY`, `CONTROL`, `BRAIN`, `ROLES`, `RISK_LEVELS`, `LIFECYCLE`, `NEWS`, `RESEARCH`, `LESSONS`, `PERMISSIONS`, `WINDOWS`.

## Tests

```
./tb test          # whole suite (unittest, no network, no real keys)
python3 -m unittest discover -s tests -p 'test_layering.py'     # a single file
```

## Contributing

See `CONTRIBUTING.md` (rules: stdlib only, cross-platform, no real keys in tests, do not write to the real `state/`, keep the layers, do not add trading tools to the gateway without discussion). Security issues: `SECURITY.md`. Conduct: `CODE_OF_CONDUCT.md`.
Before publishing a fork, run `./tb audit-public`.

## License

MIT, see `LICENSE`.
