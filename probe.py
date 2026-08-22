#!/usr/bin/env python3
"""Phase 0 gate. Answers three questions in one run:
  1. does Reddit RSS answer a GitHub Actions runner (or 403)?
  2. what egress IP did this run get (fresh per run -> no 60s sleeps needed)?
  3. when did this run ACTUALLY fire vs its scheduled slot (GH delay)?
Appends one line to spike.log. No secrets, no data collection.
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

def get(url, ua=UA, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(400_000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return None, str(e).encode()

now = datetime.now(timezone.utc)

# egress IP -> tells us whether runs share an IP (rate-limit scope)
ip_status, ip_body = get("https://api.ipify.org", ua="curl/8")
ip = ip_body.decode().strip() if ip_status == 200 else "unknown"

# two Reddit calls back-to-back: does the 2nd 429 without a 60s sleep?
s1, b1 = get("https://www.reddit.com/r/SaaS/new.rss")
t0 = time.time()
s2, _ = get("https://www.reddit.com/r/Agency/new.rss")
gap = round(time.time() - t0, 1)

entries = b1.count(b"<entry>") if s1 == 200 else 0

line = json.dumps({
    "fired_at": now.isoformat(timespec="seconds"),
    "ip": ip,
    "reddit_1": s1,
    "reddit_2_immediate": s2,
    "gap_s": gap,
    "entries": entries,
    "run_id": os.environ.get("GITHUB_RUN_ID", ""),
})
with open("spike.log", "a") as f:
    f.write(line + "\n")
print(line)
