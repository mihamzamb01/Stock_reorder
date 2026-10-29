# Stock Reorder - Incident Runbook

This runbook covers incident detection, response, and recovery for the
stock-reorder multi-agent pipeline.

---

## 1. Severity Levels

| Severity | Definition | Response Time | Escalation |
|----------|-----------|---------------|------------|
| **SEV-1** | Pipeline completely down; no reorders possible | 15 min | On-call lead + eng manager |
| **SEV-2** | Pipeline degraded (latency > threshold, failure rate > 20%) | 1 hour | On-call engineer |
| **SEV-3** | Non-critical issue (single-agent failure, retry succeeded) | 4 hours | Eng team |

---

## 2. Incident Detection

### Automated alerts

The pipeline emits three alert types tracked in `runs/executions.csv`:

| Alert | Condition | Threshold |
|-------|-----------|-----------|
| LATENCY | Slowest agent step | `ALERT_LATENCY_MS=30000` |
| FAILURE_RATE | Failed steps ratio | `ALERT_FAILURE_RATE_PCT=20` |
| COST | Estimated LLM cost | `ALERT_COST_LIMIT_USD=10` |

View recent alerts:

```bash
cat runs/executions.csv
```

Build the supervision dashboard:

```bash
python main.py --dashboard-sheet
# opens runs/dashboard.html
```

---

## 3. Incident Response

### SEV-1: Pipeline down

1. **Check the kill switch** — is it active?
   ```bash
   cat kill_switch.flag 2>/dev/null || echo "no flag file"
   echo "STOCK_REORDER_KILL_SWITCH=$STOCK_REORDER_KILL_SWITCH"
   ```
2. **If active, deactivate** (only after root cause is understood):
   ```bash
   python rollback.py unkill
   ```
3. **Verify the pipeline runs**:
   ```bash
   python main.py --preflight
   python main.py --warehouse WH-NORTH --max-items 3
   ```
4. **Check logs** in `runs/<correlation_id>.json`.

### SEV-2: Degraded performance

1. Identify the slowest agent from the dashboard.
2. Check model provider status (LarpRouter / OpenRouter / NVIDIA).
3. Consider re-routing a role via env vars:
   ```bash
   export MODEL_REORDER_PROVIDER=openrouter
   export MODEL_REORDER_NAME=google/gemini-3.5-flash-lite
   ```
4. Re-run the pipeline and monitor.

### SEV-3: Non-critical

1. Record the incident in `runs/` trace.
2. Create a follow-up ticket if it recurs 3+ times in 24h.

---

## 4. Kill Switch (Emergency Halt)

The kill switch halts the pipeline before any model calls or database writes.

**Activate:**

```bash
# Via environment variable
export STOCK_REORDER_KILL_SWITCH=1
export KILL_SWITCH_REASON="SEV-1: LarpRouter outage"

# Or via flag file
echo "kill_switch=1
reason=SEV-1: LarpRouter outage" > kill_switch.flag
```

**Deactivate:**

```bash
unset STOCK_REORDER_KILL_SWITCH
rm -f kill_switch.flag
# or
python rollback.py unkill
```

**Check status:**

```bash
python -c "from kill_switch import is_killed, reason; print(is_killed(), reason())"
```

---

## 5. Database Rollback

Re-seed to a clean known-good state:

```bash
python -c "
from db.setup import seed_database
from pathlib import Path
seed_database(Path('stock_reorder.db'))
"
```

Restore from a backup:

```bash
cp backups/stock_reorder.db.YYYYMMDD stock_reorder.db
```

Verify table counts:

```bash
python -c "
from db.repository import table_counts
from db.repository import get_engine
engine = get_engine('stock_reorder.db')
print(table_counts(engine))
engine.dispose()
"
```

---

## 6. Version Rollback

Roll back to a previous tagged release:

```bash
# List available tags
git tag --sort=-version:refname "v*"

# Roll back version
python rollback.py version-rollback 0.1.0

# Checkout the tag
git checkout v0.1.0
```

---

## 7. Post-Incident

1. Update `runs/executions.csv` with the resolution.
2. Add a note to `runs/<correlation_id>.json`.
3. Create a post-mortem issue with:
   - Root cause
   - Timeline
   - Action items
4. Update this runbook if procedures changed.