# Example configs

Files in `config/` that carry the maintainer's personal values are mirrored here as `*.example` with neutral content.
Copy one over its `config/` twin only if you want to reset to it. These files are examples, not recommendations; read `docs/RISK_LEVELS.md` and `docs/GATEWAY.md` first.

| file | what is neutralised |
|---|---|
| `config/agents.json.example` | token hashes set to null (create your own with `./tb gateway token new`) |
| `config/watch_config.json.example` | the dated macro blackout list is emptied |
| `config/risk_profile.json.example` | profile renamed; absolute USDT limits are sized for a tiny account, review them |

Everything else in `config/` is a neutral default and is published as is (see `docs/PUBLISH_CHECKLIST.md`).
