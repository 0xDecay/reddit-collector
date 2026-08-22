# Reddit Collector Migration — Decision Record

**Date:** 2026-08-22  
**Status:** Historical record. The system described here is now live.

This file captures the architectural decisions and reasoning behind migrating Reddit Collector from a VPS-based system (Hermes cron + system cron) to GitHub Actions + Claude cloud agent. It is frozen — do not edit it. Future changes go in README.md.

---

## What changed

### Before (VPS + Hermes)
- **Collection:** System cron every 10 minutes, polling into `/opt/hermes/clean-data/reddit-collector/data/reddit.db` (SQLite, live on disk)
- **Profiling:** System cron at 09:28 ET, ran `profile_subs.py` against the live SQLite
- **Synthesis:** Hermes cron at 09:30 ET, ran `reddit-scout` agent (deepseek-v4-pro), which read SQLite, filled profiles, wrote digest, and sent it directly to Telegram via Hermes's `--deliver telegram` flag
- **Watchdog:** Hermes cron (hourly), sent Telegram alerts via Hermes
- **Secrets:** Telegram bot token in `/etc/hermes.env`, Hermes environment variables

### After (GitHub Actions + Claude cloud)
- **Collection:** GitHub Actions every 5 minutes, parallel per subreddit (no per-IP rate-limit collisions), appends to append-only JSON files in git
- **Profiling:** Still deterministic (`profile_subs.py`), runs inside the cloud agent
- **Synthesis:** Claude cloud agent (daily routine, 9:30 AM UTC), reads JSONL via `load_db.py`, fills profiles, writes digest to `outbox/`, pushes
- **Delivery:** GitHub Actions workflow (triggered by outbox/ push), reads digest, sends to Telegram, clears outbox
- **Watchdog:** GitHub Actions (hourly), sends alerts only when broken
- **Secrets:** Only Telegram bot token in GitHub repo secret; cloud agent sandbox has zero secrets by design

---

## Why this migration?

1. **VPS operational burden:** Hermes is complex, requires container expertise, and had multi-store cron bugs (some jobs dead since 2026-07-29). Switching to GitHub Actions removes the operational tail.

2. **Collector speed:** The old 10-minute cycle cost us ~30–40% of items on activity spikes (measured 2026-08-20: r/SaaS/comments rotates every 14–16 minutes). Parallel per-subreddit collection in Actions gives us a true 5-minute cycle without hitting per-IP rate limits.

3. **Better data durability:** Append-only JSONL in git is immutable and version-controlled. The old SQLite on disk had zero backup strategy. If the file corrupted, we lost history.

4. **Simpler credential model:** Hermes required injecting secrets into the container environment. Separating collection (no secrets), synthesis (read-only), and delivery (has secret) means the cloud sandbox never touches the credential. If the agent is compromised, the token stays safe.

5. **Cost predictability:** GitHub Actions is metered; Hermes heartbeats bleed tokens. The new model is event-driven (digest only runs daily) with no polling overhead.

---

## Key architectural decisions

### JSONL instead of SQLite in git

**Decision:** Store raw collected data as append-only JSONL (`items.jsonl`, `polls.jsonl`), rebuild SQLite on demand.

**Why not keep SQLite in git?**
- SQLite is a binary format; git cannot delta it, so every collection cycle adds the full file size (~20–50 MB over time)
- No way to version-control partial writes or recovery
- Rebuilding forces us to separate data from schema; `load_db.py` is always in sync with the Python code

**Why rebuild on-demand?**
- `profile_subs.py` and the agent read SQLite, but they don't write to it
- The rebuild is deterministic (same JSONL always produces the same db)
- No race conditions; each agent run gets its own fresh db

**Tradeoff:** One extra step (JSONL → SQLite in agent), but gains immutability and audit trail.

### Outbox delivery pattern

**Decision:** Agent writes digest to `outbox/digest-<date>.txt`, commits and pushes. A separate GitHub Actions workflow watches `outbox/`, sends to Telegram, then deletes the file.

**Why this pattern?**
- The cloud agent sandbox has no 1Password CLI, no `TELEGRAM_BOT_TOKEN`, no way to post HTTP requests to Telegram's API (actually, it does have `terminal` and curl, but we intentionally skip it)
- Pushing to git is the only communication channel out of the sandbox
- GitHub Actions already has the secret and can send

**Why not just have the agent send directly?**
- Credential management: injecting a secret into a cloud sandbox violates a simple security boundary. If the agent prompt is modified (malicious or accidental), it could exfiltrate the token
- Auditability: every digest is a git commit, versioned, reviewable

**Tradeoff:** One extra workflow, but cleaner separation of concerns.

### Parallel collection per subreddit

**Decision:** GitHub Actions matrix strategy: one job per subreddit, each polls 2 feeds in sequence, then merge all outputs.

**Why parallel?**
- Old system: 4 subreddits took 4 × 2 × 60s = 8 minutes on one IP, causing rate-limit collisions with itself
- New system: Each job gets a fresh runner IP (no per-IP collision), so they complete in true parallel (~2 min per job max), and merge happens once
- Result: 5-minute cycle is comfortable; 8-minute old cycle is bumping against limits

**Why not fire all 8 requests at once?**
- Reddit's per-IP rate limit is ~1 req/min. If we fired all 8 in parallel, we'd hit 429s immediately
- Each job sleeps 60s between its 2 sequential requests (new.rss, comments.rss), respecting the per-IP limit within that job

**Tradeoff:** Slightly longer individual job runtime, but faster overall cycle and zero rate-limit collisions.

### Event-driven watchdog (no interval polling)

**Decision:** Watchdog runs hourly in Actions (via schedule), not via a constantly-polling Hermes routine.

**Why hourly instead of every 5 minutes?**
- Alerts should be rare (silence = healthy). Hourly is responsive enough for "collector stalled"
- Reduces token burn (no constant agent wakeups)

**Why not use Hermes interval heartbeat?**
- Hermes's interval-based heartbeat on Opus is prohibitively expensive (see MEMORY.md: 10 agents × 2 heartbeats/min × 60min × 4000 tokens = 4.8M tokens/day)
- GitHub Actions is free for public repos and cheap for private

---

## What was learned during migration

### Secret leakage incidents
- 2026-08-20: Telegram bot token logged to GitHub Actions output (captured in real time during testing)
- 2026-08-22: 1Password service-account token leaked (same session)

Both are recorded in MEMORY.md as actionable. Action: rotate both tokens immediately if discovered during production monitoring.

### Hermes cron unreliability
The old system had jobs in two places:
- `/opt/hermes/clean-data/cron/jobs.json` (live store)
- Per-profile cron cache (stale)

Jobs like `contentron` last ticked 2026-07-29; others have no record. Manual heartbeats worked, but scheduled fires never happened. Lesson: Hermes cron ≠ reliable scheduling. GitHub Actions is more transparent (you can see every run in the UI) and has built-in retry.

### `$HOME/scripts/` vs `~/.hermes/scripts/` trap
Hermes cron create help claims `~/.hermes/scripts/`, but the actual path is `$HOME/scripts/`. Scripts stored in the wrong dir never run, and `last_status: ok` is the agent status, not proof the script was found. This cost debugging time; GitHub Actions is clearer.

---

## What stayed the same

- **Subreddit list** (`subreddits.txt`): Same format, same four subreddits
- **Profile format** (`profiles/<subreddit>.md`): Same structure, machine sections + synthesis sections
- **Agent SOUL and role:** Reddit Scout is still read-only synthesis only; no posting, no interaction
- **Watchdog silence design:** Silent when healthy; loud only on specific failure modes
- **No score/upvote/comment counts:** Still forbidden (RSS doesn't carry them)

---

## Gotchas for future edits

### Do not edit `schema.sql` without rebuilding JSONL

If you change the schema, you must also ensure all historical JSONL rows are compatible. Currently:
- `items.jsonl` has 4 fields (permalink, kind, subreddit, posted_at)
- `polls.jsonl` has 7 fields (subreddit, kind, polled_at, http_status, items_seen, items_new, gap_warning)

Adding a field is safe (adds a new column, old rows get NULL). Removing a field breaks the rebuild. Renaming is also breaking.

### The cloud agent cannot be run locally in the same way

The agent runs in Claude's cloud sandbox with ambient git credentials (no token needed to push). Local testing needs to set up git credentials manually.

### Outbox files must not be gitignored

If you add `outbox/*.txt` to `.gitignore`, the send-digest workflow will never trigger. The workflow watches `paths: ['outbox/*.txt']`.

### Parallel collection means order is not guaranteed

Items from different subreddits may be interleaved in `items.jsonl` depending on which job finishes first. This is fine — `load_db.py` rebuilds SQLite with proper indexes. But if you try to read raw JSONL and expect subreddit-wise chunks, you'll be surprised.

### The watchdog uses fixed thresholds, not trending

Thresholds (15-min stall, 3 gaps in 6h, etc.) are hardcoded in `reddit-watchdog.py`. If you want to change them, edit the `STALL_MIN`, `GAP_MIN_BURST`, etc. constants. No trend history is tracked.

---

## Rollback plan (if needed)

The old VPS system is still deployable, but we are not keeping it warm. To restore:

1. Provision the VPS (mootoshi) again
2. Restore the VPS-era scripts from git history (`git log --all -- collect.py` etc., check out an older ref)
3. Reinstall Hermes and deploy the three cron jobs
4. Point the live db back to the VPS path

This is a multi-hour job and would lose ~2 days of profile updates. It's a last resort only if GitHub Actions becomes unavailable.

---

## Verification checklist (as of 2026-08-22)

- [x] Collector runs every 5 min and appends to JSONL
- [x] Collector handles concurrent pushes (rebase + retry)
- [x] Profile synthesis runs daily and updates profiles
- [x] Digest is formatted correctly (≤35 columns, plain text)
- [x] Digest is sent to Telegram
- [x] Outbox is cleared after send
- [x] Watchdog runs hourly
- [ ] Watchdog has actually fired on schedule (tested manually, never seen a real fire)

---

## Known issues to watch

1. **Watchdog schedule unverified.** It's been tested by hand and works, but has not yet fired on its own hourly schedule. If you're relying on it for alerting, confirm it actually fires in production.

2. **Secret rotation is manual.** There's no automated secret rotation. If a token leaks, you must manually update the repo secret with `gh secret set`.

3. **Subreddit capacity.** Four subreddits is the ceiling at the current interval. Going to five would require either faster polling or selective feed frequencies (not currently built).

4. **Profile drift.** If you accidentally edit a profile file's machine-generated sections (between the HTML comment markers), the next agent run will overwrite your edits with fresh synthesis. Edits should only happen in the synthesis blocks (demographics, psychographics, etc.).

---

## References

- **README.md** — current operational runbook
- **MEMORY.md** — ongoing lessons and decisions (updated per-session)
- **agent/SOUL.md** — the cloud agent's binding brief
- **agent/PROMPT.md** — the cloud agent's daily task
