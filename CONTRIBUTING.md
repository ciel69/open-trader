# Contributing

Thanks for your interest. This is experimental trading software: please read the disclaimer in `README.md` first. Discussion in English or Russian is fine; most docs are in Russian.

## Running tests
```
./tb test                                                     # whole suite
python3 -m unittest discover -s tests -p 'test_layering.py'   # one file
```
Tests must pass on Python 3.9 on macOS/Linux, and stay portable to Windows. They run without network access and without real keys.

## Test hermeticity
Tests must not depend on, or change, the owner's personal state. `tests/_bootstrap.py` (imported by every test) points the personal locations at empty temp dirs:
`TB_STATE_DIR_CONTROL` (`state/control`: risk level, platform/trading flags), `TB_STATE_DIR_BRAIN` (`state/brain`), `TB_CONFIG_LOCAL` (`config/local`), `TB_KEYS_DIR` (signing key, tokens; `~/.config/traider-bot` is never read).
The hooks live in `tb.paths` / `tb.core.localcfg`, apply only to the default project root (a test with its own `TB_ROOT` is unaffected) and do nothing when the variables are unset (production).
So the risk level in tests is always the default (3), whatever the owner has chosen. Rules: a test that needs a key or a custom root creates its own temp dir and restores the environment (`patch.dict(os.environ)` or `addCleanup`; if `setUp` can run twice, remember the original environment once); never write to the real `state/`, `config/`, `runtime/`;
do not hard-code counts that other work packages change without naming what they count. `tests/test_zz_realstate_guard.py` compares snapshots of the real dirs (and checks for a leaked `TB_ROOT`); with a live platform it checks only `config/` and skips `state/`, `runtime/` (the platform writes there itself).

## Rules
- **Standard library only.** No third-party dependencies, no `pip install` step.
- **Cross-platform.** Use `os.path`/`tb.paths` for paths, `tb.fsutil` for atomic replace; no shell-only tricks; keep `tb`, `tb.cmd`, `tb.ps1` behaviour identical (logic lives in `src/tb/launcher.py`).
- **No real keys in tests, fixtures, docs or issues.** Fake secrets must be obviously fake. Never read `~/.config/traider-bot/` from tests or from the brain/gateway layers.
- **Never write to the real `state/`, `runtime/`, `data/` or `config/` from tests.** Use a temporary root (`TB_ROOT` or `--root`); `tests/test_zz_realstate_guard.py` checks this.
- **Keep the layers** (`tests/test_layering.py`): `brain`, `logic`, `core`, `dash`, `gateway`, `mcp`, `llm`, `control`, `lifecycle` do not import `tb.venues` or `tb.executor`; `logic` is pure; the gateway and MCP import no network, process or exchange code; the exchange secret name appears only in the launcher and venue modules.
- **Style.** Small modules, docstring at the top saying what the module is and what it must not do, explicit error codes, comments explain why. No emojis in code. Keep Python 3.9 syntax (no `match`, no `X | Y` types).
- **Third-party text is data.** Anything from news, names or exchanges must be sanitised and never reach a prompt as an instruction.
- **Docs.** Update the relevant file in `docs/` and `docs/INDEX.md` with the change.

## Proposing new things
- **New role (LLM task)**: add `config/roles/<role>.json|.md`, a strict response schema, a semantic check in `brain/validate.py`, fixtures in `tests/fixtures_roles/`, and a test. The LLM proposes, code verifies (`docs/ROLES.md`).
- **New data source (news, macro)**: add a parser in `src/tb/intel/sources.py` with fixtures, `verified: false` by default, no network in tests (`docs/NEWS.md`).
- **New LLM provider**: an adapter in `src/tb/llm/adapters/`, an entry in `config/providers.json` (no keys; `key_file` only), tests with a fake transport (`docs/BRAIN.md`).
- **New gateway tool**: do NOT add trading-capable tools (anything that places orders, changes size/leverage/margin/limits, clears KILL, reads files or keys) without prior discussion in an issue. Read-only tools and tightening-only operations are easier to accept. See `docs/GATEWAY.md`.
- **Strategy research**: rules must be fixed before looking at data, with train/test split, costs, and robustness checks (`docs/RESEARCH.md`). Report negative results too.

## Pull requests
Small, focused, with tests. Describe what changed and why, and which docs you touched. Run `./tb test` and `./tb audit-public` before opening a PR.
Security problems: do not open a public issue, see `SECURITY.md`.
