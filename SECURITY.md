# Security Policy

## Reporting a vulnerability

Do NOT open a public issue for a security problem. Report privately through GitHub: the repository's **Security** tab, then **Report a vulnerability** (private vulnerability reporting).
Include: affected file/command, steps to reproduce (on paper profile or a temporary root, never with real keys), and the impact you see.
Expect an acknowledgement within a reasonable time; this is a volunteer project with no SLA. Please allow time for a fix before disclosure.

## Threat model (short)

The platform is designed so that a compromised or misbehaving LLM cannot move money outside strict limits:
- The LLM ("brain") never sees API keys or the filesystem. It only gets a fixed tool catalog through the gateway (`docs/GATEWAY.md`) with per-agent rights and limits.
- Only the deterministic executor holds the trading key. Every plan passes `guard` and risk levels (outside the LLM); a stop can only be tightened; `state/KILL` halts trading and only a human removes it.
- Third-party text (news, headlines, names) is data, never instructions; a quarantined reader model with a strict JSON schema is the only thing that sees raw news (`docs/NEWS.md`).
- The dashboard listens on 127.0.0.1 only; control actions need a login token and a signed command protocol (`docs/CONTROL.md`).
- Paper profile is the default; live needs explicit owner gates.

In scope: bypass of guard/risk levels/KILL, a path for LLM or third-party text to reach keys, files, shell or orders, key or token leakage into logs, state, or the dashboard, command-signing or login weaknesses, path traversal or injection in the dashboard/control API.

## NOT a vulnerability

- Losing money by trading: this is experimental software, profit is not proven, see `docs/RESEARCH.md`.
- Anything that requires the attacker to already control your machine account, your key files in `~/.config/traider-bot/`, or your shell.
- Wrong or stale third-party data (unverified sources, `docs/NEWS.md`), exchange outages, or model mistakes that stay inside the configured limits.
- Findings that only work after the owner loosens limits, enables live mode, or edits `config/risk_profile.json` by hand.
- Missing Windows hardening: Windows is unverified.

## Reminders for users

- Keep real exchange keys out of the repository, tests, issues and logs. Use a key with trading rights only (no withdrawal) and IP restrictions if the exchange allows.
- Start with the `paper` profile. Do not run `trade` until you have read `docs/CONTROL.md` and `docs/PERMISSIONS.md`.
- Run `./tb audit-public` before publishing any fork or copy.
