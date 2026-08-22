#!/usr/bin/env python3
"""
Reddit feed collector. Polls r/*/new.rss and r/*/comments.rss, detects gaps,
stores items and polls to JSONL. Stdlib only.
"""
import sys
import json
import argparse
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import html
import re
from datetime import datetime, timezone
import os

# ponytail: request rate limit is per-IP. Verified: req 2 at +3s -> 429.
# Use ≥60s inter-request interval to stay below rate limit.
REQUEST_INTERVAL_SEC = 60

# Browser-like User-Agent; Reddit requires this to avoid 403.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Atom namespace.
ATOM_NS = "http://www.w3.org/2005/Atom"


def strip_html(text):
    """Strip HTML tags and unescape entities."""
    if not text:
        return text
    # Remove tags.
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape entities.
    text = html.unescape(text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_feed(url):
    """
    Fetch an Atom feed from URL. Return (status_code, xml_str) or (status_code, None).
    Sleeps REQUEST_INTERVAL_SEC before request to respect rate limit.
    """
    time.sleep(REQUEST_INTERVAL_SEC)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, None
    except urllib.error.URLError:
        return None, None  # Network error.


def parse_atom_feed(xml_str, subreddit, feed_kind, schema_kind):
    """
    Parse Atom feed XML. Yield dicts with keys:
    permalink, kind, subreddit, author, title, body, parent_title, created_utc.

    Args:
        xml_str: Atom XML feed data
        subreddit: subreddit name
        feed_kind: 'new' or 'comments' (for URL building)
        schema_kind: 'post' or 'comment' (for storage per schema.sql)
    """
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return

    # Extract all entries.
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        item = {}
        item["kind"] = schema_kind  # Store schema vocabulary ('post' or 'comment')
        item["subreddit"] = subreddit
        item["fetched_at"] = datetime.now(timezone.utc).isoformat()

        # Permalink from link/@href.
        link_elem = entry.find(f"{{{ATOM_NS}}}link")
        if link_elem is not None:
            item["permalink"] = link_elem.get("href")
        else:
            continue  # Skip if no permalink.

        # Author from author/name.
        author_elem = entry.find(f"{{{ATOM_NS}}}author")
        if author_elem is not None:
            name_elem = author_elem.find(f"{{{ATOM_NS}}}name")
            if name_elem is not None:
                item["author"] = name_elem.text
        else:
            item["author"] = None

        # Title.
        title_elem = entry.find(f"{{{ATOM_NS}}}title")
        item["title"] = title_elem.text if title_elem is not None else None

        # Body from content (HTML; need to strip tags).
        content_elem = entry.find(f"{{{ATOM_NS}}}content")
        if content_elem is not None:
            item["body"] = strip_html(content_elem.text)
        else:
            item["body"] = None

        # created_utc from published (ISO8601).
        published_elem = entry.find(f"{{{ATOM_NS}}}published")
        if published_elem is not None:
            item["created_utc"] = published_elem.text
        else:
            # Fallback to updated.
            updated_elem = entry.find(f"{{{ATOM_NS}}}updated")
            item["created_utc"] = updated_elem.text if updated_elem is not None else None

        # parent_title for comments: derive from entry title.
        # Reddit comment entry titles are: "<username> on <post_title>"
        # Verified against live reddit.com/r/test/comments.rss feed.
        # ponytail: regex extracts post_title from comment entry title; NULL for posts.
        item["parent_title"] = None
        if schema_kind == "comment" and item["title"]:
            # Extract parent title from "user on post_title" format via regex.
            match = re.match(r"^.*?\s+on\s+(.+)$", item["title"])
            if match:
                item["parent_title"] = match.group(1)

        yield item


def get_data_dir():
    """Resolve data directory relative to script's own directory."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "data")


def load_existing_permalinks(items_jsonl_path):
    """
    Load set of existing permalinks from items.jsonl for deduplication.
    Returns empty set if file doesn't exist.
    """
    if not os.path.exists(items_jsonl_path):
        return set()

    permalinks = set()
    try:
        with open(items_jsonl_path, 'r') as f:
            for line in f:
                if line.strip():
                    obj = json.loads(line)
                    if 'permalink' in obj:
                        permalinks.add(obj['permalink'])
    except (IOError, json.JSONDecodeError):
        pass

    return permalinks


def load_last_poll(polls_jsonl_path, subreddit, kind):
    """
    Load the last poll record for a (subreddit, kind) pair from polls.jsonl.
    Returns the record or None if not found.
    """
    if not os.path.exists(polls_jsonl_path):
        return None

    last_poll = None
    try:
        with open(polls_jsonl_path, 'r') as f:
            for line in f:
                if line.strip():
                    obj = json.loads(line)
                    if obj.get('subreddit') == subreddit and obj.get('kind') == kind:
                        last_poll = obj
    except (IOError, json.JSONDecodeError):
        pass

    return last_poll


def append_items(items_jsonl_path, items, existing_permalinks):
    """
    Append new items to items.jsonl. Return count of actually-appended rows (new items).
    ponytail: skip duplicates by checking against existing_permalinks set.
    """
    new_count = 0
    # ponytail: only write to items_jsonl on new items; skip duplicates.
    new_items = [item for item in items if item["permalink"] not in existing_permalinks]

    if new_items:
        try:
            with open(items_jsonl_path, 'a') as f:
                for item in new_items:
                    f.write(json.dumps(item) + '\n')
                    new_count += 1
        except IOError as e:
            print(f"Error appending to {items_jsonl_path}: {e}", file=sys.stderr)
            return 0

    return new_count


def record_poll(polls_jsonl_path, items_jsonl_path, subreddit, kind, http_status, items_list, oldest_ts, newest_ts, existing_permalinks):
    """
    Record a single poll to polls.jsonl. Return items_new count.
    ponytail: gap detection loads last poll from JSONL, maintains same behavior as SQLite version.
    """
    items_seen = len(items_list) if items_list else 0
    items_new = append_items(items_jsonl_path, items_list, existing_permalinks) if items_list else 0

    # Check for gap: compare oldest in THIS poll to newest in PREVIOUS poll for this (sub, kind).
    gap_warning = 0
    if oldest_ts is not None:
        last_poll = load_last_poll(polls_jsonl_path, subreddit, kind)
        if last_poll and last_poll.get('newest_in_feed'):
            prev_newest = last_poll['newest_in_feed']
            # If this poll's oldest is newer than previous poll's newest, we have a gap.
            if oldest_ts > prev_newest:
                gap_warning = 1
                print(
                    f"WARNING: gap detected in r/{subreddit} {kind}: "
                    f"prev_newest={prev_newest} < this_oldest={oldest_ts}",
                    file=sys.stderr,
                )

    # Append poll record to JSONL.
    poll_record = {
        "subreddit": subreddit,
        "kind": kind,
        "polled_at": datetime.now(timezone.utc).isoformat(),
        "http_status": http_status,
        "items_seen": items_seen,
        "items_new": items_new,
        "oldest_in_feed": oldest_ts,
        "newest_in_feed": newest_ts,
        "gap_warning": gap_warning,
    }

    try:
        with open(polls_jsonl_path, 'a') as f:
            f.write(json.dumps(poll_record) + '\n')
    except IOError as e:
        print(f"Error appending to {polls_jsonl_path}: {e}", file=sys.stderr)

    return items_new


def poll_subreddit(data_dir, subreddit):
    """Poll both new.rss and comments.rss for a subreddit."""
    items_jsonl = os.path.join(data_dir, "items.jsonl")
    polls_jsonl = os.path.join(data_dir, "polls.jsonl")

    existing_permalinks = load_existing_permalinks(items_jsonl)
    results = []
    # Mapping: feed name -> (schema_kind for storage)
    feeds = [("new", "post"), ("comments", "comment")]

    for feed_kind, schema_kind in feeds:
        url = f"https://www.reddit.com/r/{subreddit}/{feed_kind}.rss"
        print(f"Polling r/{subreddit} {feed_kind}...", file=sys.stderr)

        status, xml_str = fetch_feed(url)

        if status is None:
            # Network error.
            record_poll(polls_jsonl, items_jsonl, subreddit, schema_kind, None, [], None, None, existing_permalinks)
            print(f"r/{subreddit} {feed_kind}: network error", file=sys.stdout)
            results.append((subreddit, feed_kind, None, "network error"))
            continue

        if status != 200:
            # HTTP error (including 429).
            record_poll(polls_jsonl, items_jsonl, subreddit, schema_kind, status, [], None, None, existing_permalinks)
            print(f"r/{subreddit} {feed_kind}: HTTP {status}", file=sys.stdout)
            results.append((subreddit, feed_kind, status, f"HTTP {status}"))
            continue

        # Parse feed.
        items = list(parse_atom_feed(xml_str, subreddit, feed_kind, schema_kind))

        if not items:
            record_poll(polls_jsonl, items_jsonl, subreddit, schema_kind, 200, [], None, None, existing_permalinks)
            print(f"r/{subreddit} {feed_kind}: 0 items", file=sys.stdout)
            results.append((subreddit, feed_kind, 200, "0 items"))
            continue

        # Extract timestamps for gap detection.
        timestamps = [item.get("created_utc") for item in items if item.get("created_utc")]
        oldest_ts = min(timestamps) if timestamps else None
        newest_ts = max(timestamps) if timestamps else None

        items_new = record_poll(polls_jsonl, items_jsonl, subreddit, schema_kind, 200, items, oldest_ts, newest_ts, existing_permalinks)
        print(
            f"r/{subreddit} {feed_kind}: {len(items)} items, {items_new} new",
            file=sys.stdout,
        )
        results.append((subreddit, feed_kind, 200, f"{len(items)} items, {items_new} new"))

    return results


def status_command(data_dir):
    """Print status of all subreddits polled so far."""
    items_jsonl = os.path.join(data_dir, "items.jsonl")
    polls_jsonl = os.path.join(data_dir, "polls.jsonl")

    # Load polls and group by (subreddit, kind)
    polls_by_sub_kind = {}
    try:
        with open(polls_jsonl, 'r') as f:
            for line in f:
                if line.strip():
                    poll = json.loads(line)
                    key = (poll['subreddit'], poll['kind'])
                    if key not in polls_by_sub_kind:
                        polls_by_sub_kind[key] = {'count': 0, 'last_polled': None, 'gap_count': 0}
                    polls_by_sub_kind[key]['count'] += 1
                    polls_by_sub_kind[key]['last_polled'] = poll.get('polled_at')
                    polls_by_sub_kind[key]['gap_count'] += poll.get('gap_warning', 0)
    except (IOError, json.JSONDecodeError):
        pass

    print("Subreddit Status:")
    for (sub, kind), stats in sorted(polls_by_sub_kind.items()):
        print(
            f"  r/{sub} {kind}: {stats['count']} polls, last at {stats['last_polled']}, {stats['gap_count']} gaps"
        )

    # Item count per subreddit
    items_by_sub = {}
    try:
        with open(items_jsonl, 'r') as f:
            for line in f:
                if line.strip():
                    item = json.loads(line)
                    sub = item['subreddit']
                    items_by_sub[sub] = items_by_sub.get(sub, 0) + 1
    except (IOError, json.JSONDecodeError):
        pass

    print("\nItem Counts:")
    for sub in sorted(items_by_sub.keys()):
        print(f"  r/{sub}: {items_by_sub[sub]} items")


def selftest():
    """Run selftest with inline JSONL fixtures. No network."""
    import tempfile

    # Create temp directory for JSONL files.
    temp_dir = tempfile.mkdtemp()
    temp_items = os.path.join(temp_dir, "items.jsonl")
    temp_polls = os.path.join(temp_dir, "polls.jsonl")

    try:

        # Test 1: HTML strip.
        assert strip_html("<p>hello</p>") == "hello"
        assert strip_html("a  b    c") == "a b c"
        assert strip_html("&lt;tag&gt;") == "<tag>"
        print("✓ HTML strip works")

        # Test 2: Atom parse with inline fixture.
        atom_xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Test Post</title>
    <link href="https://reddit.com/r/test/comments/abc123/test_post/" />
    <author><name>testuser</name></author>
    <content type="html"><![CDATA[This is <b>bold</b> text]]></content>
    <published>2024-01-15T10:00:00Z</published>
  </entry>
  <entry>
    <title>Second Post</title>
    <link href="https://reddit.com/r/test/comments/def456/second/" />
    <author><name>user2</name></author>
    <content type="html"><![CDATA[Plain text]]></content>
    <published>2024-01-15T09:00:00Z</published>
  </entry>
</feed>
"""
        items = list(parse_atom_feed(atom_xml, "test", "new", "post"))
        assert len(items) == 2
        assert items[0]["title"] == "Test Post"
        assert items[0]["body"] == "This is bold text"
        assert items[0]["author"] == "testuser"
        assert items[0]["kind"] == "post", f"Expected kind='post', got {items[0]['kind']}"
        print("✓ Atom parse works")

        # Test 3: Store items and verify idempotency.
        existing_permalinks = load_existing_permalinks(temp_items)
        record_poll(temp_polls, temp_items, "test", "post", 200, items, "2024-01-15T09:00:00Z", "2024-01-15T10:00:00Z", existing_permalinks)

        # Count items in temp_items
        count1 = 0
        try:
            with open(temp_items, 'r') as f:
                count1 = sum(1 for line in f if line.strip())
        except IOError:
            pass

        # Store same items again (should be deduplicated).
        existing_permalinks = load_existing_permalinks(temp_items)
        record_poll(temp_polls, temp_items, "test", "post", 200, items, "2024-01-15T09:00:00Z", "2024-01-15T10:00:00Z", existing_permalinks)

        # Count items in temp_items again
        count2 = 0
        try:
            with open(temp_items, 'r') as f:
                count2 = sum(1 for line in f if line.strip())
        except IOError:
            pass

        assert count1 == 2 and count2 == 2, f"Idempotency test failed: count1={count1}, count2={count2}"
        print("✓ Idempotency works")

        # Test 3b: Verify items_new is 0 when re-inserting duplicates (critical for data fidelity).
        # This catches fabricated counts: if items_new always equals items_seen, the bug ships.
        poll_data = None
        try:
            with open(temp_polls, 'r') as f:
                lines = [line.strip() for line in f if line.strip()]
                if lines:
                    poll_data = json.loads(lines[-1])  # Last poll record for this test
        except (IOError, json.JSONDecodeError):
            pass

        assert poll_data is not None, "No poll record found"
        items_seen = poll_data.get('items_seen')
        items_new = poll_data.get('items_new')
        assert items_seen == 2, f"Expected items_seen=2, got {items_seen}"
        assert (
            items_new == 0
        ), f"CRITICAL: items_new should be 0 on duplicate insert, but got {items_new}. This means counts are fabricated."
        print("✓ Duplicate detection (items_new=0) works")

        # Test 4: Gap detection should FIRE.
        # Poll 1: oldest 09:00, newest 10:00.
        # Poll 2: oldest 11:00, newest 12:00. Should detect gap.
        item_early1 = {
            "permalink": "https://reddit.com/r/test3/comments/aaa/early1/",
            "kind": "post",
            "subreddit": "test3",
            "author": "u1",
            "title": "Early1",
            "body": "b1",
            "parent_title": None,
            "created_utc": "2024-01-15T09:00:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        item_early2 = {
            "permalink": "https://reddit.com/r/test3/comments/bbb/early2/",
            "kind": "post",
            "subreddit": "test3",
            "author": "u2",
            "title": "Early2",
            "body": "b2",
            "parent_title": None,
            "created_utc": "2024-01-15T10:00:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_permalinks = load_existing_permalinks(temp_items)
        record_poll(temp_polls, temp_items, "test3", "post", 200, [item_early1, item_early2], "2024-01-15T09:00:00Z", "2024-01-15T10:00:00Z", existing_permalinks)

        item_late1 = {
            "permalink": "https://reddit.com/r/test3/comments/ccc/late1/",
            "kind": "post",
            "subreddit": "test3",
            "author": "u3",
            "title": "Late1",
            "body": "b3",
            "parent_title": None,
            "created_utc": "2024-01-15T11:00:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        item_late2 = {
            "permalink": "https://reddit.com/r/test3/comments/ddd/late2/",
            "kind": "post",
            "subreddit": "test3",
            "author": "u4",
            "title": "Late2",
            "body": "b4",
            "parent_title": None,
            "created_utc": "2024-01-15T12:00:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        # This should trigger gap_warning because oldest (11:00) > prev newest (10:00).
        existing_permalinks = load_existing_permalinks(temp_items)
        record_poll(temp_polls, temp_items, "test3", "post", 200, [item_late1, item_late2], "2024-01-15T11:00:00Z", "2024-01-15T12:00:00Z", existing_permalinks)

        gap_flag = 0
        try:
            with open(temp_polls, 'r') as f:
                lines = [line.strip() for line in f if line.strip()]
                if lines:
                    last_poll = json.loads(lines[-1])
                    gap_flag = last_poll.get('gap_warning', 0)
        except (IOError, json.JSONDecodeError):
            pass

        assert gap_flag == 1, f"Gap detection failed: expected gap_warning=1, got {gap_flag}"
        print("✓ Gap detection works")

        # Test 5: No gap on overlap.
        item_overlap1 = {
            "permalink": "https://reddit.com/r/test4/comments/eee/overlap1/",
            "kind": "post",
            "subreddit": "test4",
            "author": "u5",
            "title": "Overlap1",
            "body": "b5",
            "parent_title": None,
            "created_utc": "2024-01-15T10:00:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        item_overlap2 = {
            "permalink": "https://reddit.com/r/test4/comments/fff/overlap2/",
            "kind": "post",
            "subreddit": "test4",
            "author": "u6",
            "title": "Overlap2",
            "body": "b6",
            "parent_title": None,
            "created_utc": "2024-01-15T11:00:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_permalinks = load_existing_permalinks(temp_items)
        record_poll(temp_polls, temp_items, "test4", "post", 200, [item_overlap1, item_overlap2], "2024-01-15T10:00:00Z", "2024-01-15T11:00:00Z", existing_permalinks)

        item_overlap3 = {
            "permalink": "https://reddit.com/r/test4/comments/ggg/overlap3/",
            "kind": "post",
            "subreddit": "test4",
            "author": "u7",
            "title": "Overlap3",
            "body": "b7",
            "parent_title": None,
            "created_utc": "2024-01-15T10:30:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        item_overlap4 = {
            "permalink": "https://reddit.com/r/test4/comments/hhh/overlap4/",
            "kind": "post",
            "subreddit": "test4",
            "author": "u8",
            "title": "Overlap4",
            "body": "b8",
            "parent_title": None,
            "created_utc": "2024-01-15T11:30:00Z",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        # This should NOT trigger gap because oldest (10:30) <= prev newest (11:00).
        existing_permalinks = load_existing_permalinks(temp_items)
        record_poll(temp_polls, temp_items, "test4", "post", 200, [item_overlap3, item_overlap4], "2024-01-15T10:30:00Z", "2024-01-15T11:30:00Z", existing_permalinks)

        gap_flag = 0
        try:
            with open(temp_polls, 'r') as f:
                lines = [line.strip() for line in f if line.strip()]
                if lines:
                    last_poll = json.loads(lines[-1])
                    gap_flag = last_poll.get('gap_warning', 0)
        except (IOError, json.JSONDecodeError):
            pass

        assert gap_flag == 0, f"Overlap test failed: expected gap_warning=0, got {gap_flag}"
        print("✓ No gap on overlap works")

        # Test 6: Validate all stored kinds are in the schema vocabulary {'post', 'comment'}.
        stored_kinds = set()
        try:
            with open(temp_items, 'r') as f:
                for line in f:
                    if line.strip():
                        item = json.loads(line)
                        stored_kinds.add(item.get('kind'))
        except (IOError, json.JSONDecodeError):
            pass

        poll_kinds = set()
        try:
            with open(temp_polls, 'r') as f:
                for line in f:
                    if line.strip():
                        poll = json.loads(line)
                        poll_kinds.add(poll.get('kind'))
        except (IOError, json.JSONDecodeError):
            pass

        valid_kinds = {"post", "comment"}
        invalid_item_kinds = stored_kinds - valid_kinds
        invalid_poll_kinds = poll_kinds - valid_kinds

        assert not invalid_item_kinds, f"Invalid kinds in items: {invalid_item_kinds}"
        assert not invalid_poll_kinds, f"Invalid kinds in polls: {invalid_poll_kinds}"
        print("✓ Kind vocabulary validation works")

        print("\nSelftest PASSED")
        return 0

    finally:
        # Clean up temp directory
        import shutil
        try:
            shutil.rmtree(temp_dir)
        except (OSError, IOError):
            pass


def main():
    parser = argparse.ArgumentParser(description="Reddit feed collector")
    parser.add_argument("--sub", action="append", dest="subs", help="Subreddit to poll (repeatable)")
    parser.add_argument("--subs-file", dest="subs_file", help="File with subreddits (one per line)")
    parser.add_argument("--data-dir", dest="data_dir", default=get_data_dir(), help="Path to data directory (default: data/)")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    parser.add_argument("--selftest", action="store_true", help="Run selftest and exit")

    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if args.status:
        status_command(args.data_dir)
        return 0

    # Collect subs from both --sub and --subs-file.
    subs = args.subs or []
    if args.subs_file:
        with open(args.subs_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    subs.append(line)

    if not subs:
        parser.print_help()
        return 1

    # Ensure data directory exists
    os.makedirs(args.data_dir, exist_ok=True)

    # Poll each subreddit.
    for sub in subs:
        poll_subreddit(args.data_dir, sub)

    return 0


if __name__ == "__main__":
    sys.exit(main())
