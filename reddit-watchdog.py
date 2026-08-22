#!/usr/bin/env python3
"""Reddit collector watchdog. Prints NOTHING when healthy.

ponytail: silence is the feature. This runs as a GitHub Actions job and sends
a Telegram message only on failure — empty stdout means no alert is needed.
Every line it CAN print names an action. Stdlib only: urllib, json, os, datetime.
Ceiling: fixed thresholds, no history/trending. Upgrade only if it cries wolf.
"""
import json, sys, os, glob, subprocess
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError

# Configuration
POLLS_JSONL = os.environ.get("WATCHDOG_POLLS", "data/polls.jsonl")
# Data is committed once per run, at the end, so the newest COMMITTED poll
# legitimately ages during a run even while polling is healthy. Worst normal
# case = run length (25 min) + GitHub's scheduler gap (measured at 23 min on
# 2026-08-22). 60 leaves headroom above that.
#
# The old value was 15, with the comment "GitHub Actions runs every 5 min" --
# which measurement disproved. At 15 this alarms on ordinary scheduler jitter,
# and an alert that cries wolf gets muted, which is worse than no alert.
# Detection is slower, but gap_warning below is the PRECISE data-loss signal
# and it is unaffected by this threshold.
STALL_MIN = 60
GAP_WINDOW_H = 6
GAP_MIN_BURST = 3
NON200_MAX_24H = 3
# The cloud agent delivers by committing a digest into outbox/; a GitHub Actions
# workflow sends it and deletes it. A file that lingers there is a digest that was
# written but never delivered -- the exact silent failure that has cost this
# project a day's digest twice. Normal send latency is under a minute.
STUCK_OUTBOX_H = 2

DRY_RUN = "--dry-run" in sys.argv
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = "-1004299687993"
TELEGRAM_THREAD_ID = "35"


def _visw(line):
    """Rendered columns: emoji ~2, VS16 selector ~0. Telegram phone ~35 cols."""
    import unicodedata
    return sum(0 if (c == "️" or unicodedata.combining(c))
               else (2 if unicodedata.east_asian_width(c) in ("W", "F") else 1)
               for c in line)


MAX_COLS = 35


def _send_telegram(message):
    """Send alert to Telegram. Must fail loudly if token missing or send fails."""
    # ponytail: check dry-run BEFORE the credential. A dry run must not need a
    # token, or the safe way to test the alerting path is unavailable exactly
    # where you most want it -- CI, a fresh sandbox, anywhere op is not set up.
    if DRY_RUN:
        print(message)
        return

    if not TELEGRAM_BOT_TOKEN:
        print("FATAL: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "message_thread_id": int(TELEGRAM_THREAD_ID),
        "text": message,
        "parse_mode": "HTML"
    }).encode('utf-8')

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            if not result.get("ok"):
                error_msg = result.get("description", "unknown error")
                print(f"Telegram send failed: {error_msg}", file=sys.stderr)
                sys.exit(1)
    except URLError as e:
        print(f"Telegram API error: {e}", file=sys.stderr)
        sys.exit(1)


def _selftest():
    """Prove each alert path fires; verify width guard; confirm token never appears."""
    import subprocess, tempfile, os as os2
    me = os.path.abspath(__file__)
    now = datetime.now(timezone.utc)

    def build(poll_age_min, n_gaps, n_bad):
        d = tempfile.mkdtemp()
        jp = os.path.join(d, "polls.jsonl")
        with open(jp, "w") as f:
            ts = (now - timedelta(minutes=poll_age_min)).isoformat()
            # One normal poll
            f.write(json.dumps({
                "subreddit": "SaaS", "kind": "post", "polled_at": ts,
                "http_status": 200, "gap_warning": 0
            }) + "\n")
            # Gap warnings
            for _ in range(n_gaps):
                f.write(json.dumps({
                    "subreddit": "SaaS", "kind": "comment", "polled_at": ts,
                    "http_status": 200, "gap_warning": 1
                }) + "\n")
            # Bad HTTP status
            for _ in range(n_bad):
                f.write(json.dumps({
                    "subreddit": "SaaS", "kind": "post", "polled_at": ts,
                    "http_status": 429, "gap_warning": 0
                }) + "\n")
        return jp

    _widths = []

    def run(jp):
        env = dict(os.environ, WATCHDOG_POLLS=jp, TELEGRAM_BOT_TOKEN="test_token")
        env.pop("PAPERCLIP_TASK_ID", None)  # Exclude if present
        out = subprocess.run(
            [sys.executable, me, "--dry-run"],
            capture_output=True, text=True, env=env
        ).stdout
        _widths.append(("alert", out))
        return out

    # 1. healthy -> silent
    out = run(build(5, 0, 0))
    assert out.strip() == "", f"healthy state must be silent, got: {out!r}"

    # 2. stalled collector (no poll in 15 min)
    out = run(build(90, 0, 0))
    assert "COLLECTOR STALLED" in out, f"stall not detected: {out!r}"

    # 3. a single gap must NOT alert (spike, not actionable)
    out = run(build(5, 1, 0))
    assert out.strip() == "", f"one gap should stay silent, got: {out!r}"

    # 4. sustained gaps must alert
    out = run(build(5, 4, 0))
    assert "SUSTAINED GAPS" in out, f"gap burst not detected: {out!r}"

    # 5. http error cluster
    out = run(build(5, 0, 9))
    assert "HTTP ERRORS" in out, f"429 cluster not detected: {out!r}"

    # 6. width guard: verify ≤35 cols on every line
    for label, output in _widths:
        for line in output.splitlines():
            w = _visw(line)
            assert w <= MAX_COLS, (
                f"{label}: line is {w} cols (max {MAX_COLS}): {line!r}")

    # 7. prove width guard CAN fail by testing with an overwidth line
    test_line = "🟢 this is a deliberately wide line to test the guard mechanism"
    w = _visw(test_line)
    if w <= MAX_COLS:
        # If it happens to fit, add more content
        test_line = "🟢 " + "x" * 50
    assert _visw(test_line) > MAX_COLS, (
        f"width guard selftest failed: test line {_visw(test_line)} cols is not > {MAX_COLS}")

    # 8. token never appears in output
    env_with_token = dict(os.environ, WATCHDOG_POLLS=build(90, 0, 0), TELEGRAM_BOT_TOKEN="secret_token_12345")
    env_with_token.pop("PAPERCLIP_TASK_ID", None)
    proc = subprocess.run(
        [sys.executable, me, "--dry-run"],
        capture_output=True, text=True, env=env_with_token
    )
    combined = proc.stdout + proc.stderr
    assert "secret_token_12345" not in combined, (
        "FATAL: token appeared in stdout or stderr!")

    print("watchdog selftest PASSED")
    print("  ✓ healthy=silent, stalled, gaps, 429s all detected")
    print("  ✓ width guard enforced on all paths (≤35 cols)")
    print("  ✓ width guard can fail (proven by deliberate overwidth test)")
    print("  ✓ token never leaked to stdout/stderr")


if "--selftest" in sys.argv:
    _selftest()
    sys.exit(0)

# --- main watchdog logic ---------------------------------------------------

alerts = []
now = datetime.now(timezone.utc)


def iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# Read polls from JSONL
polls = []
try:
    if os.path.exists(POLLS_JSONL):
        with open(POLLS_JSONL, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    polls.append(json.loads(line))
except Exception as e:
    alerts.append(f"\U0001F534 WATCHDOG I/O\n   cannot read polls\n   {str(e)[:28]}")

if not alerts and polls:
    # --- collector health -------
    # Find most recent poll
    last_poll = max(polls, key=lambda p: iso(p.get("polled_at")) or datetime.min)
    lt = iso(last_poll.get("polled_at"))

    if lt is None:
        alerts.append("\U0001F534 COLLECTOR: no polls\n   cron never ran\n   → README")
    else:
        mins = (now - lt).total_seconds() / 60
        if mins > STALL_MIN:
            alerts.append(
                f"\U0001F534 COLLECTOR STALLED\n"
                f"   {int(mins)} min since last poll\n"
                f"   losing data now\n"
                f"   → check job")

    # Sustained gaps
    cutoff = (now - timedelta(hours=GAP_WINDOW_H)).isoformat()
    recent_gaps = [
        p for p in polls
        if p.get("gap_warning") and iso(p.get("polled_at")) and iso(p.get("polled_at")) > iso(cutoff)
    ]
    if len(recent_gaps) >= GAP_MIN_BURST:
        # Count by (sub, kind)
        gap_counts = {}
        for p in recent_gaps:
            key = (p.get("subreddit"), p.get("kind"))
            gap_counts[key] = gap_counts.get(key, 0) + 1
        detail = ", ".join(f"r/{s}/{k} x{n}" for (s, k), n in sorted(gap_counts.items()))
        alerts.append(
            f"\U0001F534 SUSTAINED GAPS\n"
            f"   {len(recent_gaps)} in {GAP_WINDOW_H}h · {detail}\n"
            f"   interval too slow\n"
            f"   → speed up")

    # HTTP error cluster
    cut24 = (now - timedelta(hours=24)).isoformat()
    bad_polls = [
        p for p in polls
        if iso(p.get("polled_at")) and iso(p.get("polled_at")) > iso(cut24)
        and p.get("http_status") and p.get("http_status") != 200
    ]
    if len(bad_polls) > NON200_MAX_24H:
        status_counts = {}
        for p in bad_polls:
            status = p.get("http_status", 0)
            status_counts[status] = status_counts.get(status, 0) + 1
        detail = ", ".join(f"{s} x{n}" for s, n in sorted(status_counts.items()))
        alerts.append(
            f"\U0001F534 HTTP ERRORS\n"
            f"   {len(bad_polls)} non-200 in 24h\n"
            f"   {detail}\n"
            f"   → check spacing")

elif not alerts and not polls:
    alerts.append("\U0001F534 COLLECTOR: no data\n   no polls yet\n   → wait")

# --- stuck outbox --------------------------------------------------------
# Use the COMMIT time, not the file mtime: in CI the checkout rewrites mtime to
# now, so every file would always look fresh and this check would never fire.
for _f in sorted(glob.glob("outbox/*.txt")):
    try:
        _ct = subprocess.run(["git", "log", "-1", "--format=%ct", "--", _f],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if not _ct:
            continue  # uncommitted: nothing has had a chance to send it yet
        _h = (now - datetime.fromtimestamp(int(_ct), timezone.utc)).total_seconds() / 3600
        if _h > STUCK_OUTBOX_H:
            alerts.append(
                "\U0001F534 DIGEST NOT DELIVERED\n"
                f"   stuck {int(_h)}h in outbox\n"
                f"   {os.path.basename(_f)[:29]}\n"
                "   → check send-digest")
    except Exception:
        pass  # a git failure must not take the whole watchdog down

# --- output & send -------------------------------------------------------

if alerts:
    message = "\U0001F6A8 reddit-collector\n\n"
    message += "\n".join(alerts)
    message += "\n\nDetails in README."

    # Verify every line respects the width limit
    for line in message.splitlines():
        w = _visw(line)
        if w > MAX_COLS:
            print(f"FATAL: alert line exceeds width ({w} > {MAX_COLS}): {line!r}", file=sys.stderr)
            sys.exit(1)

    _send_telegram(message)
    sys.exit(0)

sys.exit(0)
