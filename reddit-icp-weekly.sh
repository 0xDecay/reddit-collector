#!/bin/bash
# Weekly subreddit selection check. Runs the integration-filter probe and reports.
# ponytail: --no-agent job, so this costs zero model tokens. Deterministic
# aggregation; nothing here needs an LLM.
#
# Formatted for Telegram on a phone: proportional font, ~35 usable columns, so
# NO tables and no space-padded alignment. Vertical cards only. Never emit "|" —
# the Hermes Telegram adapter has pipe-table handling that would reformat it.
set -euo pipefail
DIR=/opt/data/reddit-collector
DB=$DIR/data/reddit.db

echo "📊 Weekly check · $(date -u '+%d %b')"
echo
python3 "$DIR/icp_probe.py" --db "$DB" --narrow --subs-file "$DIR/subreddits.txt" 2>&1
python3 - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
q = lambda s: c.execute(s).fetchall()
one = lambda s: c.execute(s).fetchone()[0]

print("Health")
print("%s items · %s polls" % (one("select count(*) from items"),
                               one("select count(*) from polls")))
print("%s gaps · %s non-200" % (
    one("select count(*) from polls where gap_warning=1"),
    one("select count(*) from polls where http_status is null or http_status!=200")))
print()

print("Volume, 7d")
for sub, n in q("""select subreddit, count(*) from items
                   where fetched_at > datetime('now','-7 day')
                   group by 1 order by 2 desc"""):
    print("%s %s" % (sub, n))
print()

# Gap TREND lives here, not in the hourly watchdog. One-off gaps are activity
# spikes and are not actionable at 4 subs; a rising trend across weeks is.
rows = q("""select subreddit, kind, count(*) from polls
            where gap_warning=1 and polled_at > datetime('now','-7 day')
            group by 1,2 order by 3 desc""")
print("Data loss, 7d")
if not rows:
    print("none · interval keeping up")
for sub, kind, n in rows:
    tot = c.execute("""select count(*) from polls where subreddit=? and kind=?
                       and http_status=200 and polled_at > datetime('now','-7 day')""",
                    (sub, kind)).fetchone()[0]
    print("%s/%ss %d of %d (%.1f%%)" % (sub, kind, n, tot, (100.0*n/tot) if tot else 0))
PY
echo
echo "Swap out any 🔴 sub."
echo "4 subs is the ceiling."
