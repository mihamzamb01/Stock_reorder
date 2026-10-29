# Kill Switch - Stock Reorder Pipeline

The kill switch is an emergency halt mechanism that stops the pipeline before
any model calls, database writes, or report generation.

## How It Works

The kill switch is checked at two points:

1. **Startup** — `main.py` checks before building the graph and exits with code 3.
2. **Agent runtime** — `AgentRuntime.killed()` is available for per-node checks.

## Activation Methods (checked in order)

| Method | Example |
|--------|---------|
| Environment variable | `export STOCK_REORDER_KILL_SWITCH=1` |
| Flag file | `echo "kill_switch=1" > kill_switch.flag` |

Optional reason:

```bash
export KILL_SWITCH_REASON="SEV-1: model provider outage"
```

## Commands

```bash
# Activate via CLI
python rollback.py kill-switch "SEV-1: database corruption suspected"

# Deactivate via CLI
python rollback.py unkill

# Check status
python -c "
from kill_switch import is_killed, reason, flag_path
print('killed:', is_killed())
print('reason:', reason())
print('flag:', flag_path())
"
```

## Integration in CI

GitHub Actions sets `STOCK_REORDER_KILL_SWITCH=0` by default. If a commit
needs to block all pipeline runs, an admin can push a `kill_switch.flag` file
or set the secret in GitHub repository settings.

## Docker

```bash
docker run --env STOCK_REORDER_KILL_SWITCH=1 stock-reorder-agents
# exits immediately with code 3
```