# Trading Bot Implementation Execution

This repository now includes an execution package that implements the attached plan without modifying the plan file.

## Files Added

- `scripts/precheck_local.sh`
- `scripts/lightsail_openclaw_bootstrap.sh`
- `scripts/deploy_soul_and_crons.sh`
- `scripts/validate_paper_safety.sh`
- `.env.tradingbot.example`
- `ops/paper_trading_gate_log.md`
- `ops/live_rollout_runbook.md`

## How To Execute

1. Local prep on this machine:
   - `cp .env.tradingbot.example .env.tradingbot`
   - Update `LIGHTPANDA_WSS_URL` and model values as needed.
   - Run: `bash scripts/precheck_local.sh`

2. Copy repo to your target Lightsail OpenClaw host (or pull from git there).

3. On the Lightsail host:
   - `bash scripts/lightsail_openclaw_bootstrap.sh`
   - Configure Lightsail Networking in AWS console:
     - Restrict SSH to Tailscale range/IP.
     - Remove public `80/443`.
   - `bash scripts/deploy_soul_and_crons.sh`

4. Validate in Telegram:
   - `/status`
   - `Run full 5-minute scan now (paper mode)`
   - `Run Research Agent cycle now: recall all historical losing trades, apply first-minute momentum + copy-trading filter, and propose updates`
   - `Show autopsy of our worst historical loss`
   - `bash scripts/validate_paper_safety.sh`

## Plan Mapping

- Phase 0-3: automated by bootstrap + deploy scripts.
- Phase 4-5: covered by validation commands, `scripts/validate_paper_safety.sh`, and checklist gates.
- Phase 6-7: tracked by `ops/paper_trading_gate_log.md` and `ops/live_rollout_runbook.md` over time.

## Important Notes

- `openclaw`, `clawhub`, `tailscale`, and host firewall changes are expected to run on the Lightsail Linux instance.
- The local macOS environment is used to prepare artifacts and orchestrate deployment only.
- `finalImplemenatationSoul.md` is treated as the canonical SOUL source and is copied verbatim to `~/.openclaw/workspace/SOUL.md`.
