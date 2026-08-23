# Integration-filter probe: ranks monitored subreddits by how many plausible BUYERS
# they produce, not by volume. This is the selection rule made executable.
#
# Decision rule (see HANDOFF.md):
#   - rank on qualified items/day (absolute), qualified% as density tiebreak
#   - DISQUALIFY any sub where employee% > owner% -> career forum, not a buyer pool
#
# ponytail: regex proxies, not semantics. Ceiling: crude on sarcasm/quotes and it
# flatters builder-heavy subs on stack% (they discuss APIs as builders, not buyers).
# Upgrade path: only if a week of data shows the ranking disagrees with reality.

import sqlite3, re, collections
import datetime

import argparse, os
_ap = argparse.ArgumentParser(description="Score collected Reddit text against the integration filter.")
_ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/reddit.db"))
_ap.add_argument("--selftest", action="store_true")
_ap.add_argument("--narrow", action="store_true",
                 help="vertical cards for a chat client (Telegram); default is the wide terminal table")
_ap.add_argument("--since-hours", type=float, default=None,
                 help="Only score items fetched in the last N hours. Without it the "
                      "probe scores ALL collected items, which is right for the weekly "
                      "selection check but wrong for a daily 'what came in today' count.")
_ap.add_argument("--subs-file",
                 help="restrict to the subs listed here; without it, every sub in the db is scored "
                      "(including dropped ones, whose rows remain as frozen evidence)")
_args = _ap.parse_args()
DB = _args.db

# Integration filter: does the author own a domain/brand, run a connectable stack,
# and hold autonomous buying authority? These are text proxies, not proof.
OWNER = [
    r"\bmy (agency|clients?|company|business|firm|studio|shop)\b",
    r"\bour (clients?|agency|company|customers?)\b",
    r"\bretainer\b", r"\bwe charge\b", r"\bmy team\b", r"\binvoic",
    r"\bproposal\b", r"\bclient work\b", r"\bmy (domain|site|website)\b",
]
STACK = [
    r"\bhubspot\b", r"\bsalesforce\b", r"\bpipedrive\b", r"\bgohighlevel\b|\bghl\b",
    r"\bzapier\b", r"\bmake\.com\b", r"\bn8n\b", r"\bairtable\b", r"\bcrm\b",
    r"\bapi\b", r"\bwebhook", r"\bstripe\b", r"\bmailchimp\b|\bklaviyo\b",
]
BUYING = [
    r"\bwe (pay|paid|spend|spent)\b", r"\bi (pay|paid|spend|spent)\b",
    r"\b(per|/)\s?(month|mo)\b.{0,12}\$", r"\$\d[\d,]*\s?(/|per\s)?(mo|month|k/mo)",
    r"\bbudget\b", r"\bsubscription\b", r"\bwe hired\b|\bi hired\b",
]
# Negative: employee / job-seeker / student. These FAIL the filter.
EMPLOYEE = [
    r"\bmy (manager|boss|supervisor)\b", r"\binterview(s|ing)?\b", r"\boffer letter\b",
    r"\brecruiter\b", r"\bpromotion\b", r"\blaid off\b", r"\bresume\b|\bcv\b",
    r"\binternship\b", r"\bmba\b", r"\bsalary\b|\bcomp(ensation)?\b",
    r"\bhr\b", r"\bonboarding\b", r"\bmy firm (pays|expects)\b", r"\bpartner track\b",
]

def hits(text, pats):
    return sum(1 for p in pats if re.search(p, text, re.I))

def _selftest():
    # The scoring is the GTM decision rule; a silently broken regex mis-ranks subs.
    owner_txt = "My agency just signed a retainer and we pay for HubSpot every month"
    emp_txt   = "My manager set up the interview, recruiter wants my resume"
    assert hits(owner_txt, OWNER)  >= 2, "owner probes should fire on owner language"
    assert hits(owner_txt, STACK)  >= 1, "stack probes should fire on HubSpot"
    assert hits(owner_txt, BUYING) >= 1, "spend probes should fire on 'we pay'"
    assert hits(owner_txt, EMPLOYEE) == 0, "owner text must not trip employee probes"
    assert hits(emp_txt, EMPLOYEE) >= 3, "employee probes should fire on job-seeker language"
    assert hits(emp_txt, OWNER)    == 0, "employee text must not trip owner probes"
    # qualification: ownership AND (stack or spend), not employee-dominated
    o, st, b, e = (hits(owner_txt, OWNER), hits(owner_txt, STACK),
                   hits(owner_txt, BUYING), hits(owner_txt, EMPLOYEE))
    assert o and (st or b) and e <= o, "owner text should qualify"
    o, st, b, e = (hits(emp_txt, OWNER), hits(emp_txt, STACK),
                   hits(emp_txt, BUYING), hits(emp_txt, EMPLOYEE))
    assert not (o and (st or b) and e <= o), "employee text must NOT qualify"
    print("selftest PASSED - owner/employee probes discriminate correctly")

if _args.selftest:
    _selftest(); raise SystemExit(0)

if not os.path.exists(DB):
    raise SystemExit(f"no database at {DB}\n"
                     f"the live db is on mootoshi: "
                     f"scp root@mootoshi:/opt/hermes/clean-data/reddit-collector/data/reddit.db /tmp/live.db")

c = sqlite3.connect(DB)
_q = """select subreddit, permalink, coalesce(title,'')||' '||coalesce(body,'')
         from items where author is not null and author not like '%AutoModerator%'"""
_p = ()
if _args.since_hours:
    _cut = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=_args.since_hours)).isoformat()
    _q += " and fetched_at >= ?"
    _p = (_cut,)
rows = c.execute(_q, _p).fetchall()

agg = collections.defaultdict(lambda: dict(n=0, owner=0, stack=0, buying=0, emp=0, qual=0, disq=0))
examples = collections.defaultdict(list)

for sub, link, text in rows:
    a = agg[sub]; a["n"] += 1
    o, s, b, e = hits(text, OWNER), hits(text, STACK), hits(text, BUYING), hits(text, EMPLOYEE)
    a["owner"] += bool(o); a["stack"] += bool(s); a["buying"] += bool(b); a["emp"] += bool(e)
    # "qualified" = shows ownership AND (stack or spend), and is not dominated by employee language
    if o and (s or b) and e <= o:
        a["qual"] += 1
        if len(examples[sub]) < 3 and len(text) > 120:
            examples[sub].append((text.strip()[:200], link))
    if e and not o:
        a["disq"] += 1

# ponytail: bar glyphs isolated here so swapping them is a one-line change if a
# client renders them unevenly (proportional fonts give glyphs different widths).
BAR_FULL, BAR_EMPTY, BAR_LEN = "\u2588", "\u2591", 10

def bar(pct, ceiling=15.0):
    """Bar for owner%. ceiling=15 because observed owner% tops out ~12%."""
    filled = max(0, min(BAR_LEN, round(BAR_LEN * pct / ceiling)))
    return BAR_FULL * filled + BAR_EMPTY * (BAR_LEN - filled)

# ponytail: the rule is "employee% > owner% disqualifies", but a bare > fires on a
# rounding-width difference (SaaS hit 4.62 vs 4.55 and got flagged while both
# displayed as "4.6%"). A tie is not evidence of a career forum, so require a real
# margin. Ceiling: 1.0pp is a judgement call; widen it if red still fires on noise.
DISQUAL_MARGIN_PP = 1.0

def disqualified(a, n):
    return (100 * a["emp"] / n) - (100 * a["owner"] / n) >= DISQUAL_MARGIN_PP

def glyph(a, n):
    """Status per the decision rule: employee-dominated is disqualifying."""
    if disqualified(a, n):  return "\U0001F534"   # red
    if a["qual"] == 0:      return "\U0001F7E1"   # yellow
    return "\U0001F7E2"                           # green

# ponytail: dropped subs keep their rows as evidence, so without a subs file the
# weekly report would spend lines on subreddits nobody collects any more.
_only = None
if _args.subs_file and os.path.exists(_args.subs_file):
    _only = {l.strip() for l in open(_args.subs_file)
             if l.strip() and not l.startswith("#")}

def ranked():
    # rank on qualified items (absolute), qualified% as density tiebreak
    items = [kv for kv in agg.items() if _only is None or kv[0] in _only]
    return sorted(items,
                  key=lambda kv: (-kv[1]["qual"], -kv[1]["qual"] / max(kv[1]["n"], 1)))

if _args.narrow:
    for sub, a in ranked():
        n = max(a["n"], 1)
        own = 100 * a["owner"] / n
        print("%s r/%s" % (glyph(a, n), sub))
        print("   %d qualified \u00b7 %.1f%% owner" % (a["qual"], own))
        print("   %s owner signal" % bar(own))
        if disqualified(a, n):
            print("   \u26a0\ufe0f emp %.1f%% beats owner %.1f%%" % (100 * a["emp"] / n, own))
        print()
else:
    print("%-11s %6s %8s %8s %8s %9s %9s %9s" % ("SUB","ITEMS","owner%","stack%","spend%","EMPLOYEE%","QUALIFIED%","DISQUAL%"))
    for sub, a in ranked():
        n = max(a["n"], 1)
        print("%-11s %6d %7.1f%% %7.1f%% %7.1f%% %8.1f%% %9.1f%% %8.1f%%" % (
            sub, a["n"], 100*a["owner"]/n, 100*a["stack"]/n, 100*a["buying"]/n,
            100*a["emp"]/n, 100*a["qual"]/n, 100*a["disq"]/n))
        _window = f"last {_args.since_hours:g}h" if _args.since_hours else "all time"
        print("            -> qualified items: %d   (plausible buyers, %s)" % (a["qual"], _window))

    print()
    print("=== sample QUALIFIED items (author shows ownership + stack/spend) ===")
    for sub in agg:
        print("--- r/%s ---" % sub)
        if not examples[sub]: print("   (none)")
        for t, l in examples[sub]:
            print("   %s" % re.sub(r"\s+", " ", t))
            print("     %s" % l)
