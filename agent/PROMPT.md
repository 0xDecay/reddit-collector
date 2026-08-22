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
- Format exactly as shown below (phone-friendly, 35 char max per line)
- If there is genuinely nothing new to report, respond with exactly `[SILENT]`

Keep the message under 900 characters.

### 4. Send to Telegram

First get the bot token. Try these in order and say in your output which one worked:
```bash
# a) already in the environment?
echo "${TELEGRAM_BOT_TOKEN:+present}"
# b) 1Password service account
op read "op://Agents2/frasier-env/TELEGRAM_BOT_TOKEN"
```
Then send:
```bash
python3 send_telegram.py < digest.txt
```
`send_telegram.py` validates Telegram's `ok: true` and exits non-zero with the API error if delivery is not confirmed. **Never treat "the script ran" as proof it delivered** — a silent delivery failure once cost a full day's digest. Report the message_id it returns.

**Never print the token value itself.** Report only whether it was found and from where.

### 5. Commit and push
The repo is public so cloning needs no credential, but pushing does. Get the token:
```bash
op read "op://Agents2/fyrq42zp3gmxd3depgawbbkk2e/credential"
```
That is a fine-grained PAT scoped to this repo only. Use it without printing it:
```bash
git add profiles/ data/
git -c user.name="reddit-scout" -c user.email="agent@local" \
    commit -m "synthesis: update profiles $(date -u +%F)"
git push "https://x-access-token:$PAT@github.com/0xDecay/reddit-collector.git" HEAD:main
```
Note this container may start in DETACHED HEAD — that is why the refspec is `HEAD:main`.

If the push fails, exit non-zero and print the error. Loud failure is the only safe choice.

### 6. Report what the environment actually had
End your run by stating plainly: was `op` available? did the Telegram send confirm with a message_id? did the push land? If any credential was missing, say exactly which — that is the most useful thing you can tell me.

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
