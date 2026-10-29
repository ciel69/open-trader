# Screenshots

All images are taken from the **synthetic fixture** (`tests/fixtures_v2/gen.py`): fake capital, fake trades, no keys, no real paths. Never add a screenshot of a real account. The dashboard UI is in Russian, 1440x1000 px, dark theme.

| file | page (hash) | what it shows |
|---|---|---|
| `overview.png` | `#overview` | Now: capital, risk, open position, plans in work, limits headroom |
| `brain-decisions.png` | `#decisions` | brain status card and decision log |
| `plans.png` | `#plans` | board of plans |
| `trades.png` | `#trades` | statistics, PnL curve, history |
| `settings-providers.png` | `#settings` (tab Providers) | LLM providers, keys, "add from template" |
| `settings-roles.png` | `#settings` -> Roles | role -> profile chains |

## Regenerate (after a redesign)

Never use your real root: the fixture lives in a temp directory and the dashboard runs on a separate port (not 8765).

```
D=$(mktemp -d)
python3 tests/fixtures_v2/gen.py $D/root --live &        # keeps heartbeat and prices fresh; stop it when done (kill %1)
```
The fixture shows an equity that may resemble a real account: edit the numbers in a **copy** of `gen.py` (the screenshots here use 1000.0 / 812.5 / 1042.0 instead of the fixture's equity, free and high-water mark; the copy needs `HERE` set to the absolute `tests/fixtures_v2` path).

The Settings screens read `config/`; the fixture root has none, so copy the shipped config (no secrets, never `config/local/`) and use an empty keys directory:
```
cp config/*.json $D/root/config/ 2>/dev/null; cp -R config/roles $D/root/config/; mkdir -p $D/keys
TB_ROOT=$D/root TB_KEYS_DIR=$D/keys ./tb dash --port 8774 --max-seconds 1500 &
```
Shoot with any browser at 1440x1000, for example headless Chrome:
```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --hide-scrollbars \
  --window-size=1440,1000 --virtual-time-budget=8000 --screenshot=docs/images/overview.png "http://127.0.0.1:8774/#overview"
```
The Roles screen is a sub-tab (not addressable by URL): open `#settings`, click "Роли", capture (DevTools, or a CDP script that clicks the button and calls `Page.captureScreenshot`).
Finally: stop the dashboard and the generator, look at every PNG (no home-directory paths, no e-mail, no real amounts, no keys; the red "ctl молчит" badge is an artifact of the fixture, which runs no control service), then `./tb audit-public`.
