#!/usr/bin/env bash
# old-id-scan: READ-ONLY census of leftover old-style bundle app_ids on alpha.
#
# Sources: Postgres (every text/json column, discovered via information_schema),
# Valkey (SCAN, never KEYS), object storage (S3 list, depth<=2), ConfigMaps,
# pod literal env. Exits 1 if anything is found (so run post-cutover it must be 0),
# 2 if a source could not be examined (zero denominator is a failure, not a pass).
# Old prefixes: waddles.core.{example,app,service,integration,community},
# waddles.integrations.superpenguin (left-anchored: new ids never match).
# Env: KUBE_CONTEXT (dal2alpha) KUBE_NAMESPACE (waddlebot) REPORT_DIR. Secrets are
# read into env only; never echoed, logged, or written to a file.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KCTX="${KUBE_CONTEXT:-dal2alpha}"
NS="${KUBE_NAMESPACE:-waddlebot}"
REPORT_DIR="${REPORT_DIR:-/tmp/old-id-scan-report}"
VENV="${SWEEP_VENV:-/tmp/sweep-venv}"
K=(kubectl --context "$KCTX" -n "$NS")
mkdir -p "$REPORT_DIR"
RE='(^|[^a-z0-9.])waddles\.(core\.(example|app|service|integration|community)|integrations\.superpenguin)'
PF_PID=""
trap 'if [[ -n "$PF_PID" ]]; then kill "$PF_PID" 2>/dev/null || true; fi' EXIT
rc=0

# ---- 1. Postgres -------------------------------------------------------------
DB_DENOM="$("$HERE/lib/psql-ro.sh" "select count(*) from information_schema.columns c join information_schema.tables t using (table_schema,table_name) where c.table_schema='public' and t.table_type='BASE TABLE' and c.udt_name in ('text','varchar','bpchar','json','jsonb','_text','_varchar')")"
[[ "$DB_DENOM" -gt 0 ]] || { echo "FAIL: db: zero candidate columns discovered" >&2; exit 2; }
"$HERE/lib/psql-ro.sh" "select table_name||'.'||column_name||'='||cnt from (select c.table_name, c.column_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I where %I::text ~ %L', c.table_schema, c.table_name, c.column_name, \$re\$${RE}\$re\$), false, true, '')))[1]::text::bigint as cnt from information_schema.columns c join information_schema.tables t using (table_schema,table_name) where c.table_schema='public' and t.table_type='BASE TABLE' and c.udt_name in ('text','varchar','bpchar','json','jsonb','_text','_varchar')) x where cnt>0 order by cnt desc, 1" > "$REPORT_DIR/db.txt"
DB_COLS="$(wc -l < "$REPORT_DIR/db.txt")"
DB_ROWS="$(awk -F= '{s+=$NF} END{print s+0}' "$REPORT_DIR/db.txt")"
echo "db:      columns_examined=$DB_DENOM columns_with_old_ids=$DB_COLS rows_matching=$DB_ROWS"
head -8 "$REPORT_DIR/db.txt" | sed 's/^/           /'

# ---- 2. Valkey (SCAN only) ---------------------------------------------------
# Password stays inside the pod (REDISCLI_AUTH from the pod's own env).
VK_TOTAL=0; VK_OLD=0
for pod in valkey-kv-0 valkey-durable-0; do
  out="$("${K[@]}" exec "$pod" -c valkey -- sh -c "REDISCLI_AUTH=\"\$VALKEY_PASSWORD\" valkey-cli -p 6380 --tls --cacert /etc/valkey/tls/ca.crt --scan | awk -v P='waddles.core.example,waddles.core.app,waddles.core.service,waddles.core.integration,waddles.core.community,waddles.integrations.superpenguin' 'BEGIN{n=split(P,a,\",\")} {t++; for(i=1;i<=n;i++){s=index(\$0,a[i]); if(s>0 && (s==1 || substr(\$0,s-1,1) !~ /[a-z0-9.]/)){c[i]++; m=1}} if(m){o++; m=0}} END{printf \"%d %d\", t, o}'")" || { echo "FAIL: valkey scan on $pod" >&2; exit 2; }
  read -r t o <<<"$out"
  echo "valkey:  $pod keys_scanned=$t keys_with_old_ids=$o"
  VK_TOTAL=$((VK_TOTAL + t)); VK_OLD=$((VK_OLD + o))
done
[[ "$VK_TOTAL" -gt 0 ]] || { echo "FAIL: valkey: zero keys scanned (auth/TLS problem?)" >&2; exit 2; }

# ---- 3. Object storage -------------------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"; "$VENV/bin/pip" install -q --require-hashes -r "$HERE/../../hub_api/requirements.txt"
fi
S3_PORT="${S3_LOCAL_PORT:-18333}"
"${K[@]}" port-forward svc/waddlebot-seaweedfs "$S3_PORT:8333" >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 1 20); do curl -sk -o /dev/null "https://localhost:$S3_PORT/" && break; sleep 0.5; done
S3_ENDPOINT="https://localhost:$S3_PORT"
S3_ACCESS_KEY="$("${K[@]}" get secret waddlebot-secrets -o jsonpath='{.data.BUNDLE_STORAGE_S3_ACCESS_KEY_ID}' | base64 -d)"
S3_SECRET_KEY="$("${K[@]}" get secret waddlebot-secrets -o jsonpath='{.data.BUNDLE_STORAGE_S3_SECRET_ACCESS_KEY}' | base64 -d)"
export S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY
S3_JSON="$("$VENV/bin/python" -W ignore "$HERE/lib/old_id_helpers.py" s3)" || { echo "FAIL: s3 listing" >&2; exit 2; }
unset S3_ACCESS_KEY S3_SECRET_KEY
echo "s3:      $(python3 -c "import json,sys;d=json.loads(sys.argv[1]);print('buckets=%d prefixes_examined=%d objects_examined=%d with_old_ids=%d'%(d['examined_buckets'],d['examined_prefixes'],d['examined_objects'],d['total']))" "$S3_JSON")"
S3_OLD="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['total'])" "$S3_JSON")"
kill "$PF_PID" 2>/dev/null || true; PF_PID=""

# ---- 4. ConfigMaps + pod env -------------------------------------------------
CM_JSON="$("${K[@]}" get cm -o json | python3 "$HERE/lib/old_id_helpers.py" cm)"
PE_JSON="$("${K[@]}" get pods -o json | python3 "$HERE/lib/old_id_helpers.py" podenv)"
CM_OLD="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['total'])" "$CM_JSON")"
PE_OLD="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['total'])" "$PE_JSON")"
echo "configmaps: $(python3 -c "import json,sys;d=json.loads(sys.argv[1]);print('examined=%d matches=%d'%(d['examined_configmaps'],d['total']))" "$CM_JSON")"
echo "pod env:    $(python3 -c "import json,sys;d=json.loads(sys.argv[1]);print('pods=%d env_vars=%d matches=%d'%(d['examined_pods'],d['examined_env_vars'],d['total']))" "$PE_JSON")"

TOTAL=$((DB_ROWS + VK_OLD + S3_OLD + CM_OLD + PE_OLD))
printf '{"db_columns_examined":%s,"db_columns_with_old_ids":%s,"db_rows":%s,"valkey_keys_scanned":%s,"valkey_old_keys":%s,"s3_old_prefixes":%s,"configmap_matches":%s,"pod_env_matches":%s,"total":%s}\n' \
  "$DB_DENOM" "$DB_COLS" "$DB_ROWS" "$VK_TOTAL" "$VK_OLD" "$S3_OLD" "$CM_OLD" "$PE_OLD" "$TOTAL" | tee "$REPORT_DIR/old-id-scan.json" >/dev/null
echo "TOTAL old-id findings: $TOTAL  (detail: $REPORT_DIR/db.txt, old-id-scan.json)"
[[ "$TOTAL" -eq 0 ]] || rc=1
exit "$rc"
