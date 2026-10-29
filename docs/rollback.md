# Rollback Scenarios - Stock Reorder Pipeline

This document covers rollback procedures for database, version, configuration,
and emergency kill-switch scenarios.

---

## 1. Database Rollback

### Re-seed to clean state

```bash
python -c "
from db.setup import seed_database
from pathlib import Path
seed_database(Path('stock_reorder.db'))
print('Database re-seeded')
"
```

### Restore from backup

```bash
cp backups/stock_reorder.db.20261028 stock_reorder.db
python -c "
from db.repository import table_counts, get_engine
engine = get_engine('stock_reorder.db')
print(table_counts(engine))
engine.dispose()
"
```

Expected counts:
- warehouses: 3
- products: 10
- suppliers: 6
- inventory: 30
- demand_history: 3600

---

## 2. Version Rollback

```bash
# List tags
git tag --sort=-version:refname "v*"

# Roll back the version in VERSION and pyproject.toml
python rollback.py version-rollback 0.1.0

# Checkout the tag source code
git checkout v0.1.0
```

### Bump forward again

```bash
python scripts/bump_version.py patch --tag
```

---

## 3. Configuration Rollback

Restore `.env` from a backup:

```bash
cp .env.backup.20261028 .env
python main.py --preflight
```

---

## 4. Kill Switch Rollback (Emergency Halt)

Activate:

```bash
python rollback.py kill-switch "SEV-1: corrupt data detected"
```

Deactivate after fix:

```bash
python rollback.py unkill
```

---

## 5. Docker Rollback

```bash
# Pull previous image
docker pull stock-reorder-agents:0.1.0

# Stop current container
docker stop stock-reorder

# Run previous version
docker run --env-file .env stock-reorder-agents:0.1.0
```

---

## 6. Rollback Decision Matrix

| Scenario | Action | Command |
|----------|--------|---------|
| Bad code deployed | Revert git + version | `git checkout vX.Y.Z` + `rollback.py version-rollback X.Y.Z` |
| Corrupt database | Re-seed or restore backup | `seed_database()` or `cp backup.db stock_reorder.db` |
| Bad config change | Restore `.env` backup | `cp .env.bak .env` |
| Emergency halt | Kill switch | `rollback.py kill-switch "reason"` |
| Bad Docker image | Rollback image tag | `docker run stock-reorder-agents:previous-tag` |