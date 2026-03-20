# Controlled Live Rollout Runbook

Run this only after all paper gates pass.

## Preconditions

- [ ] 30+ days of paper data recorded in `ops/paper_trading_gate_log.md`
- [ ] Positive expectancy confirmed
- [ ] Max drawdown under 15%
- [ ] Guardian-Executor synthetic kill tests passed
- [ ] Telegram approval flow tested end-to-end

## Launch Day Procedure

1. Set position risk to 0.1% max.
2. Keep manual Telegram approval for every trade.
3. Run first session in reduced time window.
4. Record every decision and deviation from paper assumptions.

## First-Week Monitoring

- [ ] Compare live slippage against paper assumptions daily
- [ ] Review Research Agent recommendation drift
- [ ] Confirm momentum/copy/farming filters are firing as expected
- [ ] Run end-of-day autopsy for worst live decision

## Rollback Triggers

Rollback immediately to paper mode if any occurs:

- Kill threshold breach or repeated near-breach events
- Unexpected strategy drift from SOUL rules
- Missing stop-loss enforcement
- Sustained slippage materially above paper model

## Rollback Procedure

1. Disable live execution.
2. Keep research cycle running.
3. Capture incident summary in MEMORY and weekly log.
4. Re-run paper validation for 7+ additional days before retry.
