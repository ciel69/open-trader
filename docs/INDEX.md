# Documentation index

Most documents are in Russian. English entry points: `README.md`, `SECURITY.md`, `CONTRIBUTING.md`. Russian entry: `README.ru.md`.

## Start here
| file | what |
|---|---|
| `PROVIDERS.md` / `PROVIDERS.ru.md` | connect an LLM provider: templates, keys, profiles, roles, local models, budgets, errors |
| `QUICKSTART.md` | quick start of the paper profile, tab-by-tab dashboard guide, troubleshooting |
| `RUNBOOK.md` | operations: start/stop, checklists, emergency, services, autostart |
| `WINDOWS.md` | running on Windows (not verified) |
| `PERMISSIONS.md` | permission rules for the agent shell (`./tb`) |
| `PUBLISH_CHECKLIST.md` | what to do before publishing this repository, audit results |

## Architecture and design
| file | what |
|---|---|
| `ARCHITECTURE.md` | layers, folders, data flow |
| `SPEC_V2.md` | v2 specification (overview); details in `spec_v2/` |
| `DESIGN_V2.md` | design analysis and target architecture, modes of Claude integration |
| `SPEC.md`, `SPEC_FEED.md` | earlier spec and feed spec |
| `HARNESS.md` | session harness: types, cycle |
| `BACKLOG.md` | open work items |

## Safety and control
| file | what |
|---|---|
| `GATEWAY.md` | tool gateway: 19 tools, rights, limits, audit, MCP |
| `CONTROL.md` | control plane: key, tokens, live gates, signed commands |
| `EXCHANGES.md` | exchange registry, one executor per exchange, how to add an exchange |
| `spec_v2/SETTINGS_API.md` | settings API contract: LLM providers, profiles, roles, exchanges, usage |
| `RISK_LEVELS.md` | risk levels 1-5 |
| `LIFECYCLE.md` | day lifecycle: start steps, closing checklist, reports |
| `NEWS.md` | news and the quarantined reader |
| `NOTIFICATIONS.md` | OS notifications |

## Brain and models
| file | what |
|---|---|
| `PROVIDERS.md` | how to connect providers and assign them to roles (step by step) |
| `BRAIN.md` | API brain daemon, profiles, swap, fallback |
| `ROLES.md` | roles, prompts, response schemas, evaluation |
| `ref/` | reference notes on external APIs and landscape (third-party material, see PUBLISH_CHECKLIST) |

## Research and trading practice
| file | what |
|---|---|
| `RESEARCH.md` | research results: no proven edge |
| `LESSONS.md` | lessons learned from trades |
| `TRADING.md` | entry rules and experiment mode |

## Specification parts and status (`spec_v2/`)
`01_plan.md` ... `08_case_memory.md` work-package specs, `STATUS.md` running status log, `SMOKE_RESULT.md` end-to-end run result, `WP*_*.md` notes. Do not edit the specs when contributing; propose changes in an issue.
