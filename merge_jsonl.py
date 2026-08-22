#!/usr/bin/env python3
"""
merge_jsonl.py - merge per-subreddit collector artifacts into data/*.jsonl.

Exists because the collector now runs as one CI job per subreddit, so each run
produces several partial JSONL files that must be folded into the committed
ones. Replaces a bash loop that spawned a `jq` process per line (minutes on a
few thousand rows; this is a single pass).

Every artifact contains the WHOLE file, not just that job's new rows, because
each collect job checks out the repo and collect.py appends to the committed
data. So both streams MUST be deduped or a 4-job run multiplies the file by 4.
That is exactly what happened on 2026-08-22: polls went 2,711 -> 13,563 in one
"successful" run.

Items dedupe by permalink (their primary key in schema.sql). Polls dedupe by
(subreddit, kind, polled_at); polled_at is microsecond-precision, so the triple
is unique per poll. Deduping both is also what makes the caller's push-retry
loop safe to re-run.

  python3 merge_jsonl.py --artifacts artifacts/ --data data/
  python3 merge_jsonl.py --selftest
"""
import argparse, glob, json, os, sys


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A truncated final line would otherwise abort the whole merge and
                # silently drop a run's data. Skip it; the row is re-collected.
                print(f"warn: skipping malformed line in {path}", file=sys.stderr)
    return out


def write_jsonl(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, path)  # atomic: never leave a half-written data file


def poll_key(p):
    return (p.get("subreddit"), p.get("kind"), p.get("polled_at"))


def merge(artifacts_dir, data_dir):
    items = read_jsonl(os.path.join(data_dir, "items.jsonl"))
    polls = read_jsonl(os.path.join(data_dir, "polls.jsonl"))
    seen = {it.get("permalink") for it in items}
    seen_polls = {poll_key(p) for p in polls}

    added_i = added_p = 0
    for d in sorted(glob.glob(os.path.join(artifacts_dir, "*"))):
        for it in read_jsonl(os.path.join(d, "items.jsonl")):
            p = it.get("permalink")
            if not p or p in seen:
                continue
            seen.add(p)
            items.append(it)
            added_i += 1
        for pl in read_jsonl(os.path.join(d, "polls.jsonl")):
            k = poll_key(pl)
            if k in seen_polls:
                continue
            seen_polls.add(k)
            polls.append(pl)
            added_p += 1

    os.makedirs(data_dir, exist_ok=True)
    write_jsonl(os.path.join(data_dir, "items.jsonl"), items)
    write_jsonl(os.path.join(data_dir, "polls.jsonl"), polls)
    print(f"merged: +{added_i} items (total {len(items)}), +{added_p} polls (total {len(polls)})")
    return added_i, added_p


def _selftest():
    import tempfile, shutil
    t = tempfile.mkdtemp()
    try:
        data, art = os.path.join(t, "data"), os.path.join(t, "art")
        os.makedirs(data); os.makedirs(os.path.join(art, "data-SaaS"))
        write_jsonl(os.path.join(data, "items.jsonl"), [{"permalink": "/a", "v": 1}])
        write_jsonl(os.path.join(data, "polls.jsonl"), [{"subreddit": "SaaS"}])
        write_jsonl(os.path.join(art, "data-SaaS", "items.jsonl"),
                    [{"permalink": "/a", "v": 1}, {"permalink": "/b", "v": 2}])
        write_jsonl(os.path.join(art, "data-SaaS", "polls.jsonl"), [{"subreddit": "SaaS"}])

        ai, ap = merge(art, data)
        assert ai == 1, f"expected 1 new item, got {ai}"
        assert len(read_jsonl(os.path.join(data, "items.jsonl"))) == 2
        print("  ok: dedupes items by permalink, appends the new one")
        assert ap == 0, f"identical poll should have deduped, got +{ap}"
        assert len(read_jsonl(os.path.join(data, "polls.jsonl"))) == 1
        print("  ok: dedupes polls by (subreddit, kind, polled_at)")

        # Re-merging the SAME artifacts must add nothing. This is the property the
        # push-retry loop depends on -- without it a retry would duplicate rows.
        ai2, ap2 = merge(art, data)
        assert (ai2, ap2) == (0, 0), f"re-merge added {ai2} items / {ap2} polls, must be 0"
        print("  ok: re-merge is idempotent for BOTH streams")

        # The real 2026-08-22 failure: N artifacts each carrying the whole file.
        for n in ("data-A", "data-B", "data-C"):
            os.makedirs(os.path.join(art, n), exist_ok=True)
            write_jsonl(os.path.join(art, n, "polls.jsonl"),
                        [{"subreddit": "SaaS", "kind": "post", "polled_at": "T1"},
                         {"subreddit": "SaaS", "kind": "post", "polled_at": "T2"}])
        merge(art, data)
        got = len(read_jsonl(os.path.join(data, "polls.jsonl")))
        assert got == 3, f"3 artifacts x same 2 polls should yield 1+2=3 rows, got {got}"
        print("  ok: N artifacts carrying the same rows do not multiply the file")

        with open(os.path.join(data, "items.jsonl"), "a") as f:
            f.write('{"permalink": "/c", "trunc"\n')
        assert len(read_jsonl(os.path.join(data, "items.jsonl"))) == 2
        print("  ok: a malformed line is skipped, not fatal")
        print("Selftest PASSED")
    finally:
        shutil.rmtree(t)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", default="artifacts/")
    ap.add_argument("--data", default="data/")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        merge(a.artifacts, a.data)
