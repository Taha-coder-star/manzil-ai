#!/usr/bin/env bash
# Owner-run, read-only baseline. Never prints .env, tokens, phone numbers or messages.
set -Eeuo pipefail
cd /home/ubuntu/manzil-ai
for c in deploy-web-1 deploy-worker-1; do
  echo "== $c"
  sudo docker inspect --format '{{.Image}} {{.State.Status}} {{.RestartCount}}' "$c"
  sudo docker exec "$c" python -c 'from pathlib import Path; import hashlib; print("\n".join(hashlib.sha256(p.read_bytes()).hexdigest()+"  "+str(p) for root in ("app","migrations","demo") for p in sorted(Path(root).rglob("*")) if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"))'
  echo "-- docker diff /app"
  sudo docker diff "$c" | grep -E '^[ACD] /app' | grep -v __pycache__ || true
done
sudo docker ps -a --format '{{.Names}} | {{.Image}} | {{.Status}}'
sudo docker exec -i deploy-web-1 python - <<'PY'
import os
import psycopg
from app.db import tenant_transaction
A = "d7c5f8c7-8d83-4b41-9131-895e13d8aa21"
print("AI_PROVIDER", os.environ.get("AI_PROVIDER", "unset"))
queries = {
    "inventory": "SELECT status, is_demo, count(*) FROM property GROUP BY status, is_demo ORDER BY 1, 2",
    "projects": "SELECT is_demo, count(*) FROM project GROUP BY is_demo",
    "phase6": "SELECT listing_code, status, price_pkr, version FROM property WHERE listing_code LIKE 'DH6-%' ORDER BY 1",
    "roles": "SELECT role, is_active, count(*) FROM staff GROUP BY role, is_active ORDER BY 1, 2",
    "drafts": "SELECT kind, status, count(*) FROM listing_draft GROUP BY kind, status ORDER BY 1, 2",
    "jobs": "SELECT job_type, status, count(*) FROM job WHERE status IN ('pending', 'running') GROUP BY job_type, status ORDER BY 1, 2",
}
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
    with tenant_transaction(c, A):
        c.execute("SET TRANSACTION READ ONLY")
        for label, query in queries.items():
            print(label)
            for row in c.execute(query).fetchall():
                print(*row)
PY
