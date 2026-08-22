# Reddit Scout — Daily Synthesis Prompt

Read `agent/SOUL.md` in full first. It is your binding brief and the single source of truth. Do not proceed without reading it.

---

## Today's Pass

### 1. Rebuild the SQLite database
Run `python3 load_db.py --out data/reddit.db` to rebuild the database from `data/items.jsonl` and `data/polls.jsonl`. (The flag is `--out`, not `--db`.) Also run `git pull --rebase` first so you synthesise against the newest collected data.

### 2. Synthesize profiles
For each of **SaaS, Agency, sweatystartup, smallbusiness**:
- Read `data/reddit.db` with Python. Check:
  - Item counts by subreddit and kind (post/comment)
  - Any `gap_warning` rows in the polls table from the last 24h (a gap means items were lost; report it)
- Update `profiles/<subreddit>.md`:
  - Fill ONLY the five synthesis blocks (demographics, psychographics, what_they_tried, mod_rules, tone)
  - Change a section ONLY when new data contradicts or extends it—no churn
  - Append one dated changelog line: `- [YYYY-MM-DD] <what>: <why>`
  - Never touch the machine-generated sections (n-grams, quotes, histograms, author counts, volume trend)

**Thin data rule:** If a subreddit has under ~50 items total, output "not enough data yet" and do not synthesize from noise.

### 3. Produce the morning digest
Summarize for Telegram:
- Per-subreddit volume, key signals, sentiment shifts, new participant cohorts
- Anything you could NOT conclude (data gaps, contradictions)
- Status glyph: 🟢 healthy, 🟡 thin, 🔴 needs action

**Format — copy this shape exactly.** It is read on a phone, so every line must
fit in **35 rendered columns**. An emoji counts as **2 columns**, not 1. Plain
text only: no `*`, no `_`, no backticks, no `|` tables — the sender does not use
a parse mode, so markdown characters appear literally and a table becomes an
unreadable wall. Wrap prose yourself; do not rely on the client to wrap it.

```
Reddit Scout · Fri 22 Aug

🟢 r/SaaS  428 items
  Pricing anxiety up sharply.
  Founders comparing Stripe
  fees after the rate change.
  reddit.com/r/SaaS/comments/x

🟡 r/Agency  61 items
  Thin. Retainer churn talk
  continues, nothing new.

🔴 r/smallbusiness  0 items
  3 gap warnings - items were
  lost, collector may be down.

Couldn't call:
  sweatystartup volume too low
  to read a trend either way.
```

Rules for the body text:
- One card per subreddit, always in the same order: SaaS, Agency, sweatystartup, smallbusiness.
- Two-space indent under each header. Blank line between cards.
- The `Couldn't call:` block is **required** whenever something was unconcludable — a data gap, a contradiction, or thin volume. Omit the block only when there is genuinely nothing you failed to conclude. Never quietly drop an uncertainty to make the digest look cleaner.
- Include a permalink only when one specific thread is the evidence for the claim above it. Never invent one.
- Never state a score, upvote count, or comment count. Reddit RSS does not carry them, so any such number would be fabricated.

If there is genuinely nothing new to report across every subreddit, write **no
outbox file at all** (see step 4). Do not write a file containing `[SILENT]` —
that string would be delivered to the phone verbatim.

Keep the message under 900 characters.

### 4. Queue the digest for sending

**You do not send the message yourself, and you do not need any Telegram credential.**
This sandbox has no `op` and no `TELEGRAM_BOT_TOKEN` (verified 2026-08-22). A GitHub
Actions workflow holds the secret and does the sending, so no credential ever enters
this environment.

Write the digest to the outbox:
```bash
mkdir -p outbox
cat > "outbox/digest-$(date -u +%F).txt" <<'DIGEST'
<your digest text here, exactly as it should appear in Telegram>
DIGEST
```
If there is genuinely nothing new, write **nothing** to outbox/ — an absent file is
how "no news" is expressed. Do not write a file containing `[SILENT]`.

### 5. Commit and push
Pushing works with the ambient git credentials already in this sandbox — no token
needed. The container has been on `main` (not detached HEAD), but use the explicit
refspec anyway:
```bash
git add profiles/ outbox/
git -c user.name="reddit-scout" -c user.email="agent@local" \
    commit -m "synthesis: update profiles $(date -u +%F)"
git push origin HEAD:main
```
Pushing a file into `outbox/` triggers the send-digest workflow, which sends it and
then clears the outbox. **If the push fails, exit non-zero and print the error** —
a dropped push means the digest is never sent.

### 6. Report what the environment actually had
End your run by stating plainly: did the push land on origin/main, and did you write a digest file to `outbox/` (or deliberately write none because there was no news)? If the push failed, say so loudly — that is the one failure that silently costs a day's digest.

---

## Digest Format

Read on a phone, proportional font, ~35 usable columns.

**Shape:**
```
Reddit digest - [DATE]

green-circle r/SaaS
   [volume] new · [key signal]
   [detail]

yellow-circle r/Agency
   [volume] new
   [warning or thin-data note]

[blank line]

Signals
   [emerging theme]
   [detail]

Could not conclude
   [gap or ambiguity]
   [why]
```

**Rules:**
- Every line ≤35 characters
- NO tables, NO pipes `|`, NO space-padded alignment
- One stanza per subreddit, blank lines between
- Status glyph first (🟢 🟡 🔴), then subreddit name
- Detail lines indented 3 spaces
- Use `·` to separate values, never `|` or `-`
- NO permalinks or shell commands (too long)
- NO markdown, NO HTML, NO backticks
- Plain text + emoji only

**If nothing new:** respond with exactly `[SILENT]` (no digest, no send).

---

## Hard Constraints

- Reddit RSS carries NO score, NO upvote count, NO comment count. Never state what "performed well" or what the consensus was. Write exactly: "not derivable from RSS feeds"
- Quotes must be verbatim strings copied from the db, each with its permalink
- Read-only. Never contact Reddit.
- Do not propose reply drafters, post drafters, dashboards or landing pages. Out of scope by design until a week of data proves a signal exists.
- The marker blocks are load-bearing. If you overwrite them, you destroy the evidence layer (verbatim quotes, n-grams, histograms). Respect the boundaries exactly.
