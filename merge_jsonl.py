#!/usr/bin/env python3
"""
merge_jsonl.py - merge per-subreddit collector artifacts into data/*.jsonl.

Exists because the collector now runs as one CI job per subreddit, so each run
produces several partial JSONL files that must be folded into the committed
ones. Replaces a bash loop that spawned a `jq` process per line (minutes on a
few thousand rows; this is a single pass).

Items dedupe by permalink, which is their primary key in schema.sql. Polls are
event records with no natural key and are simply appended -- the caller re-runs
this against a clean checkout on every push attempt, so a retry cannot double
them.

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


def merge(artifacts_dir, data_dir):
    items = read_jsonl(os.path.join(data_dir, "items.jsonl"))
    polls = read_jsonl(os.path.join(data_dir, "polls.jsonl"))
    seen = {it.get("permalink") for it in items}

    added_i = added_p = 0
    for d in sorted(glob.glob(os.path.join(artifacts_dir, "*"))):
        for it in read_jsonl(os.path.join(d, "items.jsonl")):
            p = it.get("permalink")
            if not p or p in seen:
                continue
            seen.add(p)
            items.append(it)
            added_i += 1
        new_polls = read_jsonl(os.path.join(d, "polls.jsonl"))
        polls.extend(new_polls)
        added_p += len(new_polls)

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

        # Re-merging the SAME artifacts must add no items. This is the property the
        # push-retry loop depends on -- without it a retry would duplicate rows.
        ai2, _ = merge(art, data)
        assert ai2 == 0, f"re-merge added {ai2} items, must be 0"
        print("  ok: re-merge is idempotent for items")

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
