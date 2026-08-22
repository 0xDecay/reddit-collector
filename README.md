# Reddit Collector

Polls Reddit's public RSS feeds into SQLite, detects when the feed window outran us, and turns accumulated data into a living markdown profile per subreddit.

**The profile file is the product** — a document about a few thousand potential buyers, written from what they actually said, that gets sharper every day it runs.

This is the operational runbook. For *why* things are the way they are — why r/Consulting was dropped, why the wake job lives in the default cron store, what the build got wrong — see [HANDOFF.md](HANDOFF.md), a frozen dated record.

---

## Where it runs

The live system is on the VPS `mootoshi`. **The database in this repo stays empty** — it's a schema template, not the data.

```
system cron (every 10 min, uid 10000)
  └─ collect.py ──► /opt/hermes/clean-data/reddit-collector/data/reddit.db
                          │
system cron (09:28 ET)    │
  └─ profile_subs.py ◄────┘
        └─► profiles/<sub>.md   (machine-generated sections)
                    │
Hermes cron (09:30 ET, deepseek-v4-pro)
  └─ reddit-scout ─► fills the synthesis sections in the same files
                   └─► daily digest ──► Telegram "reddit rss" topic
```

The collector is **outside** Hermes deliberately: it needs no model, no container restart, and keeps a 1-request-per-minute poller off the token budget. Hermes only reads the db.

| Path on mootoshi | What |
|---|---|
| `/opt/hermes/clean-data/reddit-collector/` | scripts, **live db**, live profiles (`/opt/data/…` inside the container) |
| `/opt/hermes/clean-data/profiles/reddit-scout/` | the agent's `SOUL.md` + `config.yaml` |
| `/opt/hermes/clean-data/scripts/` | `reddit-watchdog.py`, `reddit-icp-weekly.sh` — **note: `$HOME/scripts/`, not the `~/.hermes/scripts/` that `hermes cron create --help` claims** |
| `/etc/cron.d/reddit-collector` | collector every 10 min, profiler 09:28 ET |
| `/var/log/reddit-collector.log` | cron output (unbuffered) |

---

## Alerting — what tells you to act

You should not have to remember to check this. Three jobs cover it:

| Job | When | Behaviour |
|---|---|---|
| `reddit-scout-daily` | 09:30 ET daily | the digest itself |
| `reddit-watchdog` | hourly at :17 | **silent unless action is needed** |
| `reddit-icp-weekly` | Thu 09:35 ET | subreddit selection review |

### Message format

All three messages are **vertical cards, plain text, ≤35 columns** — Telegram renders a proportional font at roughly 35–40 usable columns on a phone, so tables and space-padded alignment collapse into noise. Rules, if you edit any of these:

- **No tables, no column alignment.** One stanza per entity, blank line between.
- **Never emit `|`.** The Hermes Telegram adapter has pipe-table handling that reformats it.
- **`⚠️` must carry the VS16 selector** — bare `⚠` renders as a text glyph on some Android builds.
- Status glyphs `🟢` `🟡` `🔴`; `·` to separate values inline.
- No permalinks or shell commands in a message — too long, cannot wrap. Point at this README instead.
- No markup needed: Hermes sends `parse_mode=MARKDOWN_V2` and **auto-escapes** for you, retrying as plain text if parsing fails. `·` `─` `█` `░` are not MarkdownV2 specials and pass through untouched.
- Over 4,096 chars Hermes **splits** into `(1/3)`-marked messages rather than truncating.

Both scripts assert this mechanically — `--selftest` fails if any emitted line exceeds 35 rendered columns (emoji counted as 2).

**The watchdog prints nothing when healthy** — that's the design, so it never becomes noise you learn to ignore. It fires only on:

- **collector stalled** — no poll in 25 min (dead cron or a stale `flock` lock)
- **sustained gaps** — 3+ in 6h, meaning the interval is genuinely too slow (a lone gap is an activity spike and is *not* actionable at 4 subs)
- **HTTP error cluster** — >3 non-200s in 24h (429s mean the 60s spacing is being violated)
- **digest ran but did not deliver** — the exact silent failure that cost a day's digest on 2026-08-20
- **digest hasn't run in 26h**, or the job was disabled/deleted

Verify the alerting itself still works — a watchdog silent because it's *broken* is worse than none:

```bash
ssh root@mootoshi 'docker exec -u hermes hermes-gateway python3 /opt/data/scripts/reddit-watchdog.py --selftest'
```

That asserts all 8 paths: silent when healthy, loud on each failure mode.

**But a passing selftest is not proof the job works.** Running the script by hand tests the script; it does not test that the scheduler can *find* it. Verify the job itself:
```bash
ssh root@mootoshi 'docker exec -u hermes hermes-gateway hermes cron run <JOB_ID>'
# then confirm last_status flipped to ok:
ssh root@mootoshi 'python3 -c "
import json
for j in json.load(open(\"/opt/hermes/clean-data/cron/jobs.json\"))[\"jobs\"]:
    if j[\"name\"].startswith(\"reddit-\"): print(j[\"name\"], j.get(\"last_status\"), j.get(\"last_error\"))
"'
```

---

## Choosing which subreddits to watch

Decided by measurement, not by how promising a sub sounds. `icp_probe.py` scores every collected item for ownership language, connectable-stack mentions, spend signals, and *negative* employee/job-seeker signals.

**The rule:**
- Rank on **qualified items** (absolute), with **qualified %** as the density tiebreak.
- **Disqualify** any sub where `employee%` exceeds `owner%` **by at least 1.0 percentage point** — that's a career forum, not a buyer pool. The margin exists because a bare `>` fires on rounding noise: r/SaaS hit 4.62 vs 4.55 and got flagged red while both displayed as "4.6%". A tie is not evidence of anything.

`icp_probe.py --narrow` emits the phone format; the default wide table is for the terminal. `--subs-file` restricts scoring to currently-monitored subs, so dropped ones (whose rows stay as evidence) don't eat lines in the weekly report.

```bash
ssh root@mootoshi 'python3 /opt/hermes/clean-data/reddit-collector/icp_probe.py'
python3 icp_probe.py --selftest     # asserts the probes actually discriminate
```

The weekly job runs this for you and delivers it. **Pre-data scoring proved unreliable twice** (see HANDOFF) — trust the probe over intuition.

**Capacity is the hard limit:** each sub costs 2 requests × 60s. With the 60s lead sleep, N feeds take N minutes flat.

| subs | requests | cycle | vs 10-min tick |
|---|---|---|---|
| 3 | 6 | 6 min | comfortable |
| **4** | **8** | **8 min** (measured 8m02s) | **current — 2 min margin** |
| 5 | 10 | 10 min | ✗ collides; `flock` silently skips cycles |

Four is the ceiling at this interval. To go wider you'd need to poll `comments.rss` more often than `new.rss` (posts move ~10× slower) — documented, deliberately not built.

---

## Operations

**Is it healthy, and what did it collect?**
```bash
ssh root@mootoshi 'python3 - <<PY
import sqlite3
c = sqlite3.connect("/opt/hermes/clean-data/reddit-collector/data/reddit.db")
q = lambda s: c.execute(s).fetchall()
print("ITEMS PER SUB:")
for r in q("select subreddit,kind,count(*) from items group by 1,2 order by 1,2"): print("  ", r)
print("LAST 12 POLLS:")
for r in q("select polled_at,subreddit,kind,http_status,items_seen,items_new,gap_warning from polls order by id desc limit 12"): print("  ", r)
print("GAP WARNINGS (24h):", q("select count(*) from polls where gap_warning=1 and polled_at > datetime(\"now\",\"-1 day\")")[0][0])
print("NON-200 POLLS:", q("select http_status,count(*) from polls where http_status is null or http_status!=200 group by 1"))
PY'
```

**Read the actual product:**
```bash
ssh root@mootoshi 'cat /opt/hermes/clean-data/reddit-collector/profiles/SaaS.md'
```

**Did the agent wake and synthesise?**
```bash
ssh root@mootoshi 'docker exec -u hermes hermes-gateway hermes cron list 2>&1 | grep -A6 reddit-scout-daily'
```

**Digest didn't arrive?** Check delivery specifically — `last_status: ok` is the *agent's* status, not the delivery's:
```bash
ssh root@mootoshi 'python3 - <<PY
import json
for j in json.load(open("/opt/hermes/clean-data/cron/jobs.json"))["jobs"]:
    if j["name"] == "reddit-scout-daily":
        print("status:", j["last_status"], "| run:", j["last_run_at"])
        print("error:", j["last_error"])
        print("delivery_error:", j["last_delivery_error"])
PY'
```

**Change which subreddits are collected** — takes effect on the next 10-min tick:
```bash
ssh root@mootoshi 'vi /opt/hermes/clean-data/reddit-collector/subreddits.txt'
```
Keep the existing capitalisation of any sub already collecting (`Agency`, not `agency`) — rows are keyed by the string as written, so changing case splits the history into two subreddits.

**Force a cycle now** (~8 min). `flock` makes this safe mid-cycle — it exits rather than double-polling:
```bash
ssh root@mootoshi 'flock -n /run/reddit-collector.lock setpriv --reuid=10000 --regid=10000 --clear-groups /usr/bin/python3 -u /opt/hermes/clean-data/reddit-collector/collect.py --subs-file /opt/hermes/clean-data/reddit-collector/subreddits.txt'
```

**Raw cron log:**
```bash
ssh root@mootoshi 'tail -40 /var/log/reddit-collector.log'
```

> **Don't run `./run.sh` on the Mac to "check on things."** It works, but it collects into *this repo's* empty db and writes into this repo's `profiles/`, creating a second dataset that silently diverges from the live one. `run.sh` is for local development only.

**Changing the digest target:** `hermes cron edit` can only change `--schedule` and `--prompt`, so a new `--deliver` means remove + recreate. The prompt is preserved at `/opt/hermes/clean-data/reddit-collector/.cron-prompt.txt`. **Removing a job also deletes `cron/output/<job_id>/` — copy anything you want out of it first.**

---

## Rollback

Removes everything this system added; nothing else on the box is touched. No container restart needed — profiles and cron jobs are read at invocation.
First get the three job ids (`hermes cron rm` takes an **id**, not a name):
```bash
ssh root@mootoshi 'docker exec -u hermes hermes-gateway hermes cron list 2>&1 | grep -E "reddit-(scout-daily|watchdog|icp-weekly)" -A1'
```
Then:
```bash
ssh root@mootoshi '
for id in <SCOUT_ID> <WATCHDOG_ID> <WEEKLY_ID>; do
  docker exec -u hermes hermes-gateway hermes cron rm "$id"
done
rm -f /etc/cron.d/reddit-collector
rm -rf /opt/hermes/clean-data/profiles/reddit-scout
rm -rf /opt/hermes/clean-data/reddit-collector
rm -f /opt/hermes/clean-data/scripts/reddit-watchdog.py
rm -f /opt/hermes/clean-data/scripts/reddit-icp-weekly.sh
rm -f /var/log/reddit-collector.log
echo rolled back'
```

---

## Local development

```bash
./run.sh                        # one full cycle: collect → profile → status
python3 collect.py --selftest   # no network
python3 profile_subs.py --selftest
python3 icp_probe.py --selftest
python3 reddit-watchdog.py --selftest
```

Flags — `collect.py`: `--sub X` (repeatable) · `--subs-file <path>` · `--db <path>` · `--status` · `--selftest`
`profile_subs.py`: `--db <path>` · `--out <dir>` · `--sub X` · `--selftest`

---

## Files

| File | Notes |
|---|---|
| `collect.py` | RSS → SQLite, gap detection. stdlib only |
| `profile_subs.py` | db → markdown profiles, deterministic only. stdlib only |
| `icp_probe.py` | the subreddit selection rule, executable |
| `reddit-watchdog.py` | silent-unless-broken health alerting (deployed to the VPS) |
| `reddit-icp-weekly.sh` | weekly selection review (deployed to the VPS) |
| `schema.sql` | **authoritative — do not alter** |
| `subreddits.txt` | the live picks, one per line |
| `targets.md` | the scout's original research (superseded by measurement — see HANDOFF) |
| `run.sh` | one local cycle |
| `data/reddit.db` | empty by design; the live db is on the VPS |
| `HANDOFF.md` | frozen decision record |

---

## Known ceilings

**Feeds return only the ~25 most recent items.** Poll too slowly and the window advances past you and data is silently lost — which is why every poll records `gap_warning`. Measured 2026-08-20: `r/SaaS/comments.rss` replaced 22 of 25 items in 14 minutes, and loses items on ~3.3% of polls during activity spikes. Adding subreddits makes this *worse*, not better — each one lengthens the cycle.

**No score, no upvote count, no comment count.** Reddit's public feeds strip engagement metrics entirely. Any "what gets upvoted" analysis is **not derivable** from these feeds. The profiler writes `not derivable from RSS feeds` rather than guessing, and the agent's SOUL forbids inventing it. Nothing downstream should pretend otherwise.

**Rate limit ≈ 1 request per minute per IP.** Verified: request 1 → 200, request 2 at +3s → 429. `collect.py` sleeps ≥60s before *every* request. Two collectors on one IP will 429 each other.

**A browser-like User-Agent is required** — default urllib returns 403. Note `/r/<sub>/about.json` returns 403 regardless of UA; only the `.rss` endpoints work, so verify a subreddit exists by fetching `new.rss`.

**Read-only by design.** This system never posts, logs in, or creates accounts. Posting is done by a human from a real browser session — that is the architecture, not a limitation.

---

## Deliberately not built yet

Reply drafters · post drafters · send dashboards · landing pages.

This is the first half of the design. The second half waits for **a week of real data proving a signal exists**. The agent's `SOUL.md` forbids it from proposing them, so nothing drifts into building them on its own.
