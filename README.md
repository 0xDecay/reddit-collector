# Reddit Collector

A system that collects Reddit discussions, synthesizes what buyers are saying into daily profiles, and sends a morning summary to Telegram. The profile files are the product — living documents about potential customers, updated daily from what they actually wrote.

---

## How it works

Four moving parts run the whole system:

```
GitHub Actions (every 5 min)
  └─ Collector polls Reddit RSS
       └─ appends to data/items.jsonl & data/polls.jsonl
            └─ commits to git

                    [Next day]
                    
Claude Cloud Agent (daily, 9:30 AM UTC)
  └─ Reads the JSONL via load_db.py
       └─ Updates profiles/<subreddit>.md (five synthesis sections)
            └─ Writes digest to outbox/digest-<date>.txt
                 └─ Commits & pushes to git

GitHub Actions (triggered by outbox/ push)
  └─ Reads digest, sends to Telegram
       └─ Clears outbox/ (deletes the file)
```

**The flow in words:**
- The **Collector** runs every 5 minutes, fetches new posts and comments from Reddit's public RSS feeds for four subreddits (SaaS, Agency, sweatystartup, smallbusiness), and appends them to two append-only JSON files stored in git.
- The **Cloud Agent** wakes daily at 9:30 AM Eastern, rebuilds an SQLite database from the accumulated JSONL, reads what people actually said, fills in the synthesis sections (demographics, psychographics, tools tried, tone), and writes a concise Telegram digest.
- The **Digest Sender** workflow watches for new files in `outbox/`, sends them to Telegram, then deletes them. The secret (Telegram bot token) never enters the cloud sandbox — only GitHub Actions holds it.
- The **Watchdog** runs hourly and sends an alert only when something is broken (collector stalled, too many HTTP errors, digest didn't run, etc.). Silence means healthy.

---

## Something's wrong — what do I check?

### No digest arrived this morning

1. **Did the agent run?** Check the last commit to the repo:
   ```bash
   git log --oneline -5
   ```
   Look for a recent commit starting with `synthesis:`. If it's more than 26 hours old, the agent either didn't wake or failed to push.

2. **Is there a digest stuck in outbox/?**
   ```bash
   git ls-tree -r --name-only HEAD | grep outbox/
   ```
   If you see `outbox/digest-<date>.txt`, the agent wrote it but the sender workflow failed. Check Actions:
   ```bash
   gh run list --workflow send-digest.yml --limit 5
   ```
   Look for the most recent run. If it failed or is "Waiting", click it to see the error.

3. **Did the agent encounter a data problem?** Read the last synthesis commit message:
   ```bash
   git log --oneline -1 | grep synthesis
   git show HEAD --stat
   ```
   If the profiles weren't updated (no changes to `profiles/`), the agent thought there was no news and wrote nothing to outbox. That's correct — a silent run means no signals worth reporting.

4. **Check the routine setup.** The cloud agent runs via a Claude routine named `reddit-scout-daily`, scheduled at 30 13 * * * UTC (9:30 AM Eastern). To verify it exists and is enabled:
   ```bash
   # No direct CLI — this is in Claude's cloud scheduler, not GitHub Actions
   # If it's truly down, you'll see >26h since last synthesis commit
   ```

### Digest arrived but looks wrong or unreadable

Check the raw file that was sent:
```bash
git log --all --oneline | grep 'digest sent' | head -1
git show <COMMIT>:outbox/digest-*.txt
```

Problems to look for:
- Lines longer than 35 characters (emoji count as 2 chars). The digest was meant for a phone and will wrap badly.
- Fabricated numbers (score, upvotes, comment counts). RSS feeds don't carry these — if any appear, that's a bug. The agent's SOUL.md forbids it.
- Quotes that don't match the actual data. Quotes must be verbatim from the SQLite database.

To fix it: edit `agent/PROMPT.md` step 3 (the digest format template) or `agent/SOUL.md` (the rules), commit, and wait for the next run.

### Watchdog sent an alert

The watchdog runs hourly and stays silent when healthy. An alert means one of these:
- **Collector stalled** — no successful poll in 15+ minutes. Check recent Actions runs:
  ```bash
  gh run list --workflow collect.yml --limit 5 --status completed
  ```
  If they show "failure", look at the latest:
  ```bash
  gh run view <RUN_ID> --log
  ```
- **Too many HTTP 429 errors** — Reddit is rate-limiting. This means the 60-second sleep between requests is being violated. Unlikely unless collect.py was modified.
- **Too many data gaps in 6 hours** — the RSS window is advancing faster than we poll. This is data loss and means we need fewer subreddits or faster polling (both are hard limits).
- **Digest ran but didn't deliver** — the agent wrote to outbox but the send-digest workflow didn't fire or failed. See "No digest arrived" above.
- **Digest hasn't run in 26+ hours** — the routine is disabled or the agent has an unrecoverable error.

### Collector stopped committing data

Check if the Actions workflow is running:
```bash
gh run list --workflow collect.yml --limit 10
```

If runs are failing:
```bash
gh run view <RUN_ID> --log
```

Common issues:
- **One subreddit's collector failed, but others succeeded.** The merge step is `fail-fast: false`, so a timeout on one subreddit doesn't stop the others. Check which one and why.
- **Merge step failed.** Check for git conflicts or disk full (unlikely in GitHub Actions). The rebase logic should handle concurrent pushes gracefully.
- **Permission denied pushing to origin.** The Actions job should have `permissions: contents: write`. Check the workflow file hasn't been edited.

To manually trigger collection now (for testing):
```bash
gh workflow run collect.yml
```

### Digest stuck in outbox/ and never sent

The send-digest workflow triggers on any push to `outbox/`. If a digest file is still there:

1. **Check if the workflow ran:**
   ```bash
   gh run list --workflow send-digest.yml --limit 3
   ```

2. **If the latest run failed,** see what went wrong:
   ```bash
   gh run view <RUN_ID> --log
   ```

3. **Force a retry** — just re-push the file (or make a dummy commit to trigger the workflow):
   ```bash
   git commit --allow-empty -m "retry send-digest workflow"
   git push
   ```

---

## Secrets and credentials

**Where the secrets live:**

- **TELEGRAM_BOT_TOKEN** — stored in the GitHub repo as a secret (Settings → Secrets and variables → Actions). Used by:
  - `send-digest.yml` workflow (sends the digest to Telegram)
  - `watchdog.yml` workflow (sends alerts when healthy = false)

- **Cloud agent sandbox** — deliberately holds no secrets. It cannot send Telegram messages directly because 1Password CLI and the token are not available in the sandbox. This is by design: the agent writes to `outbox/` and GitHub Actions does the sending. The secret never leaves GitHub.

**When to rotate the token:**

Only if you have reason to believe it leaked. Two instances leaked into logs during the migration from VPS (2026-08-20 and 2026-08-22) — if you were monitoring the logs, rotate the token immediately:

1. Generate a new token from BotFather (Telegram)
2. Update the repo secret:
   ```bash
   gh secret set TELEGRAM_BOT_TOKEN --body <NEW_TOKEN>
   ```
3. Test by manually triggering the watchdog:
   ```bash
   gh workflow run watchdog.yml --ref main
   ```

A 1Password service-account token also leaked during migration. If you use 1Password for anything else, rotate that too (ask Dhroov for the details).

---

## How to change which subreddits are collected

Edit `subreddits.txt` (one per line, exact capitalization):
```bash
vi subreddits.txt
# Make changes
git add subreddits.txt
git commit -m "collect: watch sweatystartup instead of X"
git push
```

The change takes effect on the very next collection cycle (within 5 minutes). Keep the capitalization of subreddits already collecting — changing `Agency` to `agency` will split the history into two separate datasets.

To drop a subreddit: remove the line. The profile file stays in `profiles/` as evidence, but the agent won't update it.

**Capacity limit:** Each subreddit takes ~2 minutes to poll (2 feeds × 60s sleep). We currently run 4 subreddits in 8 minutes flat, leaving a 2-minute buffer before the next 5-minute collection job. Adding a 5th subreddit would collide with the interval. To go wider, either:
- Poll comments.rss less often than new.rss (posts move ~10× slower)
- Speed up the overall collection (not currently built)

---

## How to change the digest format

The digest is a text message sent to Telegram, read on a phone at ~35 usable columns. The format template and rules live in `agent/PROMPT.md` step 3. Edit it and commit:
```bash
vi agent/PROMPT.md
# Update the digest format shape under "### 3. Produce the morning digest"
git add agent/PROMPT.md
git commit -m "docs: update digest format"
git push
```

The change takes effect on the next daily run. Test locally by hand-running the agent's PROMPT.md.

Key rules (enforced by the agent's selftest):
- **No tables, no pipes.** Use `·` to separate values inline.
- **≤35 rendered columns per line.** Emoji count as 2 columns.
- **Plain text only.** No markdown, no HTML backticks.
- **No fabricated numbers.** Reddit RSS carries no score, upvote, or comment counts — any such number is made up and forbidden.
- **Verbatim quotes only.** If quoting from the data, the exact string must appear in the SQLite database.

If the digest is getting too long (>900 chars), either condense the signals or drop some subreddits from the summary.

---

## Known ceilings and limitations

**Feeds return ~25 most recent items.** We poll every 5 minutes, but Reddit's RSS window rotates every ~14–16 minutes. On activity spikes, items exit the window before we see them — that's a gap. Every poll records whether a gap happened in `data/polls.jsonl`. Measured during 2026-08-20: r/SaaS/comments loses ~3.3% of items on normal days.

**No score, no upvote count, no comment count in the data.** Reddit's public RSS feeds strip all engagement metrics. Any analysis claiming "what gets upvoted" or "consensus view" is **not derivable** from what we collect. The agent's SOUL.md forbids it, and the profile files use the phrase "not derivable from RSS feeds" when the data is missing.

**Rate limit ≈ 1 request per minute per IP.** Verified: request 1 at t=0 → 200 OK, request 2 at t=+3s → 429 Too Many Requests. Each subreddit needs 2 feeds × 60s = 120s minimum. `collect.py` sleeps 60s before every request to stay safe.

**A modern User-Agent is required.** Default urllib returns 403 Forbidden. `collect.py` sends a Chrome User-Agent, which works. (The endpoint `/r/<sub>/about.json` returns 403 regardless, so you can't validate a subreddit exists that way — use `new.rss` instead.)

**Read-only by design.** This system never posts, logs in, or creates accounts. The only writer is a human using a real browser. Posting is out of scope.

---

## Deliberately not built yet

Reply drafters, post drafters, dashboards, landing pages. These are the *second half* of the design. The rule: don't build them until a week of real data proves a signal exists. The agent's SOUL.md forbids it from proposing them, so they won't drift in on their own.

---

## Files

| File | What |
|---|---|
| `collect.py` | Polls Reddit RSS feeds, detects gaps, appends to JSONL. Runs in GitHub Actions every 5 min. |
| `load_db.py` | Rebuilds SQLite from the JSONL files. Used by the cloud agent to read data. |
| `send_telegram.py` | Sends a digest text to Telegram. Runs in GitHub Actions after the agent pushes. |
| `reddit-watchdog.py` | Silent unless broken — sends one Telegram alert per failure mode (hourly in Actions). |
| `profile_subs.py` | Generates the machine sections of profile markdown (n-grams, quotes, histograms). Deterministic only. |
| `icp_probe.py` | Scores each collected item for buyer signals (ownership language, spend signals). Used for subreddit selection. |
| `.github/workflows/collect.yml` | GitHub Actions: collector every 5 min, parallel per subreddit, merge & commit. |
| `.github/workflows/send-digest.yml` | GitHub Actions: triggered by outbox/ push, sends Telegram, clears outbox. |
| `.github/workflows/watchdog.yml` | GitHub Actions: hourly health check, silent when healthy. |
| `agent/SOUL.md` | The cloud agent's binding brief — what it synthesizes, how it behaves, constraints. |
| `agent/PROMPT.md` | The cloud agent's daily task — rebuild db, update profiles, produce digest, commit & push. |
| `subreddits.txt` | Live picks, one per line. Changes take effect on next 5-min collection cycle. |
| `data/items.jsonl` | Append-only log of posts and comments. Stored in git. |
| `data/polls.jsonl` | Append-only log of collection events (time, HTTP status, gap warnings). Stored in git. |
| `profiles/<subreddit>.md` | The product — daily-updated synthesis of what buyers say. Human sections + machine sections. |
| `schema.sql` | SQLite schema (authoritative — do not edit). |
| `HANDOFF.md` | Frozen record of migration decisions and historical lessons. |

---

## Honesty about this migration

Two secrets leaked into logs during the transition from VPS to GitHub Actions (detected 2026-08-20 and 2026-08-22). They have not yet been rotated. **TODO**: rotate the Telegram bot token and the 1Password service-account token immediately if you see this.

The hourly watchdog workflow has been tested manually and works when triggered by hand, but **has never yet fired on its own schedule.** If you set up monitoring on the Telegram alert, confirm it actually works by forcing a failure (or waiting 26 hours and seeing if it fires naturally).

---

## Development and testing

Run a full local cycle (collect + synthesize + status):
```bash
./run.sh
```

Run individual components:
```bash
python3 collect.py --selftest              # validates collect.py logic
python3 profile_subs.py --selftest         # validates profile generation
python3 icp_probe.py --selftest            # validates subreddit scoring
python3 reddit-watchdog.py --selftest      # validates all 8 alert paths
python3 send_telegram.py --selftest        # validates Telegram formatting
```

Collect from specific subreddits:
```bash
python3 collect.py --sub SaaS --sub Agency
```

Rebuild the database from JSONL:
```bash
python3 load_db.py --out /tmp/test.db
```

All tools are stdlib only — no dependencies to install.
