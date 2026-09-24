#!/bin/bash
# prod_runtime_probe.sh -- the ONE command the fleet's prod-runtime SSH key may run on the prod box.
#
# Installed on atlas-serve as /usr/local/bin/fleet-prod-probe and pinned in root's
# authorized_keys as `restrict,command="/usr/local/bin/fleet-prod-probe" <fleet key>`, so the key
# can run this script and nothing else: no shell, no arguments, no port forwarding. Same shape as
# the deploy key's forced command (philanthropy scripts/box/deploy-receive.sh).
#
# READ-ONLY BY CONTRACT (scripts/prod_diag_driver.md). Every SQL statement runs inside a session
# with default_transaction_read_only=on and a 10s statement_timeout, so the probe can neither
# write nor become the next 178s query. The DB credential never leaves the box: the probe reads
# the app's own env file, and the fleet only ever holds the forced-command key.
#
# Output: `@@<section>` header lines, each followed by tab-separated rows. prod_runtime.py parses
# it (scripts/test_prod_runtime.py pins the parser on a real capture).
set -uo pipefail
export LC_ALL=C

ENV_FILE=${PROBE_ENV_FILE:-/root/.config/atlas/web.env}
LOGDIR=${PROBE_CRON_LOGDIR:-/var/log/atlas}
set -a; . "$ENV_FILE" 2>/dev/null; set +a
DB=${PHILANTHROPY_DATABASE_URL:-}

sec() { printf '@@%s\n' "$1"; }

sql() {  # one read-only session per section; a failing section prints an error row, never aborts
    PGOPTIONS="-c default_transaction_read_only=on ${PROBE_PG_TIMEOUT--c statement_timeout=10000}" \
        psql "$DB" -X -q -A -t -F $'\t' -v ON_ERROR_STOP=1 -c "$1" 2>&1 \
        || printf 'error\t%s\n' "psql rc=$?"
}

sec meta
printf 'host\t%s\nnow\t%s\n' "$(hostname)" "$(date -u +%s)"

if [ -n "$DB" ]; then
    sec pg_settings  # no probe-side timeout: this section must read the server's OWN defaults
    PROBE_PG_TIMEOUT= sql "select 'max_connections', current_setting('max_connections')
         union all select 'db_statement_timeout', current_setting('statement_timeout')
         union all select 'idle_in_tx_timeout', current_setting('idle_in_transaction_session_timeout')"

    sec pg_role_settings  # rolname, is_superuserish(createrole|createdb), per-role config
    sql "select r.rolname, (r.rolsuper or r.rolcreaterole or r.rolcreatedb)::int,
                coalesce(array_to_string(s.setconfig, ','), '')
         from pg_roles r left join pg_db_role_setting s on s.setrole = r.oid and s.setdatabase = 0
         where r.rolcanlogin and r.rolname not like '\\_%' and r.rolname <> 'postgres'"

    sec pg_conn  # usename, client_addr, state, count
    sql "select coalesce(usename,''), coalesce(host(client_addr),'local'), coalesce(state,'-'), count(*)
         from pg_stat_activity where backend_type = 'client backend' group by 1,2,3 order by 4 desc"

    sec pg_statements_total  # calls, total_ms, mean_ms, max_ms, rows, query
    sql "select calls, round(total_exec_time)::bigint, round(mean_exec_time::numeric,1),
                round(max_exec_time)::bigint, rows,
                left(regexp_replace(query, '\\s+', ' ', 'g'), 240)
         from pg_stat_statements order by total_exec_time desc limit 15"

    sec pg_statements_max
    sql "select calls, round(total_exec_time)::bigint, round(mean_exec_time::numeric,1),
                round(max_exec_time)::bigint, rows,
                left(regexp_replace(query, '\\s+', ' ', 'g'), 240)
         from pg_stat_statements order by max_exec_time desc limit 10"

    sec pg_long  # pid, usename, state, age_s, wait_event, query
    sql "select pid, coalesce(usename,''), state, extract(epoch from now() - coalesce(xact_start, query_start))::int,
                coalesce(wait_event,''), left(regexp_replace(query, '\\s+', ' ', 'g'), 200)
         from pg_stat_activity
         where backend_type = 'client backend' and pid <> pg_backend_pid()
           and state in ('active', 'idle in transaction', 'idle in transaction (aborted)')
           and now() - coalesce(xact_start, query_start) > interval '60 seconds'
         order by 4 desc limit 20"

    sec pg_locks  # waiting lock count
    sql "select count(*) from pg_locks where not granted"

    sec pg_seqscan  # relname, seq_scan, seq_tup_read, idx_scan, n_live_tup
    sql "select relname, seq_scan, seq_tup_read, coalesce(idx_scan,0), n_live_tup
         from pg_stat_user_tables where n_live_tup > 1000000 and seq_scan > 0
         order by seq_tup_read desc limit 10"

    sec pg_writes  # relname, n_tup_ins, n_tup_upd, n_tup_del (since stats reset)
    sql "select relname, n_tup_ins, n_tup_upd, n_tup_del from pg_stat_user_tables
         order by n_tup_ins + n_tup_upd + n_tup_del desc limit 5"
else
    sec pg_settings
    printf 'error\tno PHILANTHROPY_DATABASE_URL in %s\n' "$ENV_FILE"
fi

sec box  # key, value
read -r l1 l5 l15 _ < /proc/loadavg
printf 'load1\t%s\nload5\t%s\nnproc\t%s\n' "$l1" "$l5" "$(nproc)"
awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{printf "mem_total_kb\t%d\nmem_avail_kb\t%d\n", t, a}' /proc/meminfo
df -P / | awk 'NR==2{gsub("%","",$5); printf "disk_used_pct\t%s\n", $5}'

sec journal  # unit, error-priority lines in the last hour
journalctl --since "-1h" -p err --no-pager -q -o json 2>/dev/null \
    | grep -o '"_SYSTEMD_UNIT":"[^"]*"\|"SYSLOG_IDENTIFIER":"[^"]*"' | cut -d'"' -f4 \
    | sort | uniq -c | sort -rn | head -10 | awk '{print $2"\t"$1}'

sec restarts  # unit, NRestarts
for u in philanthropy philanthropy-rt nginx; do
    printf '%s\t%s\n' "$u" "$(systemctl show -p NRestarts --value "$u" 2>/dev/null || echo '?')"
done

sec crontab  # the installed job names (cronjob.sh <name>), one per line
crontab -l 2>/dev/null | grep -v '^\s*#' | grep -o 'cronjob\.sh [A-Za-z0-9_.-]*' | awk '{print $2}' | sort -u

sec cron_exits  # name, last_exit_epoch, last_rc, fails_24h, last_ok_epoch
now=$(date +%s)
for f in "$LOGDIR"/*.log; do   # logrotate moves history to .log.1, so read both
    [ -f "$f" ] || continue
    newest=$(stat -c %Y "$f" "$f.1" 2>/dev/null | sort -n | tail -1)
    [ $(( now - newest )) -lt 3456000 ] || continue   # touched in the last 40 days (monthly ingests)
    cat "$f.1" "$f" 2>/dev/null | tail -n 4000 | awk -v name="$(basename "$f" .log)" -v now="$now" '
        /^=== [0-9T:-]+Z .* EXIT -?[0-9]+ ===$/ {
            ts = $2; gsub(/[-T:Z]/, " ", ts); e = mktime(ts, 1); rc = $(NF-1)
            last = e; lrc = rc
            if (rc == 0) ok = e
            else if (now - e < 86400) fails++
        }
        END { if (last) printf "%s\t%d\t%s\t%d\t%d\n", name, last, lrc, fails, ok }'
done

# ---- data inflows (docs/data_inflows.json, scored by coverage_map.py) ----------------------
sec envkeys  # NAMES of non-empty keys in the app's env files -- never a value
cat "$(dirname "$ENV_FILE")"/*.env 2>/dev/null | sed -n 's/^\(export \)\{0,1\}\([A-Z_][A-Z0-9_]*\)=.\{1,\}$/\2/p' | sort -u

sec signals  # name, http_code, note -- the app's own token-gated read endpoints, on loopback
for s in ga4 gsc posthog cf clarity; do
    code=$(curl -s -m 30 -o /tmp/.fleet-probe-sig -w '%{http_code}' -H 'Host: philanthropy.org' \
        -H 'X-Forwarded-Proto: https' -H "x-pm-token: ${PHILANTHROPY_PM_TOKEN:-}" \
        "http://127.0.0.1:8000/990/api/signals/$s" 2>/dev/null)
    note=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1]))
print((d.get("credential_status") or d.get("error") or "") + " " + str(d.get("need") or d.get("detail") or "")[:160])' \
        /tmp/.fleet-probe-sig 2>/dev/null | tr '\t\n' '  ')
    printf '%s\t%s\t%s\n' "$s" "${code:-000}" "$note"
done
rm -f /tmp/.fleet-probe-sig

if [ -n "$DB" ]; then
    sec inflow_ts  # name, newest-row epoch (blank = no rows ever). Indexed or small tables only.
    PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=10000" \
        psql "$DB" -X -q -A -t -F $'\t' 2>/dev/null <<'SQL'
select 'dash:' || s, extract(epoch from (select ts from dash_events where source = s order by id desc limit 1)::timestamp)::bigint
  from unnest(array['posthog', 'clarity', 'org_claim']) s;
select 'dash:mailer_bounce', extract(epoch from greatest(
  (select max(ts) from dash_events where source = 'mailer' and kind = 'bounced'),
  (select max(ts) from dash_events where source = 'mailer' and kind = 'complained'))::timestamp)::bigint;
select 'stripe_webhook_events', extract(epoch from max(received_at)::timestamp)::bigint from stripe_webhook_events;
select 'messages', extract(epoch from (select created_at from messages order by id desc limit 1)::timestamp)::bigint;
select 'org_claims', extract(epoch from (select created_at from org_claims order by id desc limit 1)::timestamp)::bigint;
select 'website_discovery', extract(epoch from max(tried_at)::timestamp)::bigint from website_discovery;
select 'website_content', extract(epoch from max(fetched_at)::timestamp)::bigint from website_content;
select 'org_news', extract(epoch from max(fetched_at)::timestamp)::bigint from org_news;
select 'social_signals', extract(epoch from max(observed_at)::timestamp)::bigint from social_signals;
select 'email_intake_processed', extract(epoch from max(processed_at)::timestamp)::bigint from email_intake_processed;
SQL
fi
exit 0
