#!/usr/bin/env python3
"""
Rebuild SQLite database from JSONL files. Stdlib only.
Usage: python3 load_db.py --out /path/to/reddit.db [--items-file data/items.jsonl] [--polls-file data/polls.jsonl]
Or import: from load_db import load_db; db_path = load_db("/path/to/items.jsonl", "/path/to/polls.jsonl", out="/tmp/reddit.db")
"""
import sqlite3
import json
import argparse
import os
import sys


def load_db(items_jsonl_path, polls_jsonl_path, out=None):
    """
    Rebuild SQLite database from JSONL files.
    Returns path to created database.
    """
    if out is None:
        out = ":memory:"

    conn = sqlite3.connect(out)
    c = conn.cursor()

    # Create schema (per schema.sql)
    c.execute("""
        CREATE TABLE IF NOT EXISTS items (
          permalink    TEXT PRIMARY KEY,
          kind         TEXT NOT NULL,
          subreddit    TEXT NOT NULL,
          author       TEXT,
          title        TEXT,
          body         TEXT,
          parent_title TEXT,
          created_utc  TEXT NOT NULL,
          fetched_at   TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_items_sub_created ON items(subreddit, created_utc)
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS polls (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          subreddit     TEXT NOT NULL,
          kind          TEXT NOT NULL,
          polled_at     TEXT NOT NULL,
          http_status   INTEGER,
          items_seen    INTEGER DEFAULT 0,
          items_new     INTEGER DEFAULT 0,
          oldest_in_feed TEXT,
          newest_in_feed TEXT,
          gap_warning   INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_polls_sub ON polls(subreddit, kind, polled_at)
    """)

    # Load items from JSONL
    items_count = 0
    if os.path.exists(items_jsonl_path):
        try:
            with open(items_jsonl_path, 'r') as f:
                for line in f:
                    if line.strip():
                        try:
                            item = json.loads(line)
                            c.execute(
                                """
                                INSERT INTO items
                                (permalink, kind, subreddit, author, title, body, parent_title, created_utc, fetched_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    item.get('permalink'),
                                    item.get('kind'),
                                    item.get('subreddit'),
                                    item.get('author'),
                                    item.get('title'),
                                    item.get('body'),
                                    item.get('parent_title'),
                                    item.get('created_utc'),
                                    item.get('fetched_at'),
                                ),
                            )
                            items_count += 1
                        except (json.JSONDecodeError, ValueError) as e:
                            print(f"Warning: skipping malformed item line: {e}", file=sys.stderr)
        except IOError as e:
            print(f"Warning: could not read items file {items_jsonl_path}: {e}", file=sys.stderr)

    # Load polls from JSONL
    polls_count = 0
    if os.path.exists(polls_jsonl_path):
        try:
            with open(polls_jsonl_path, 'r') as f:
                for line in f:
                    if line.strip():
                        try:
                            poll = json.loads(line)
                            c.execute(
                                """
                                INSERT INTO polls
                                (subreddit, kind, polled_at, http_status, items_seen, items_new, oldest_in_feed, newest_in_feed, gap_warning)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    poll.get('subreddit'),
                                    poll.get('kind'),
                                    poll.get('polled_at'),
                                    poll.get('http_status'),
                                    poll.get('items_seen'),
                                    poll.get('items_new'),
                                    poll.get('oldest_in_feed'),
                                    poll.get('newest_in_feed'),
                                    poll.get('gap_warning'),
                                ),
                            )
                            polls_count += 1
                        except (json.JSONDecodeError, ValueError) as e:
                            print(f"Warning: skipping malformed poll line: {e}", file=sys.stderr)
        except IOError as e:
            print(f"Warning: could not read polls file {polls_jsonl_path}: {e}", file=sys.stderr)

    conn.commit()
    conn.close()

    if out != ":memory:":
        print(f"✓ Loaded {items_count} items and {polls_count} polls into {out}", file=sys.stderr)

    return out


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild SQLite database from JSONL files (items.jsonl and polls.jsonl)"
    )
    parser.add_argument(
        "--items-file",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/items.jsonl"),
        help="Path to items.jsonl (default: data/items.jsonl)"
    )
    parser.add_argument(
        "--polls-file",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/polls.jsonl"),
        help="Path to polls.jsonl (default: data/polls.jsonl)"
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Path to output SQLite database"
    )

    args = parser.parse_args()

    db_path = load_db(args.items_file, args.polls_file, out=args.out)
    print(db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
