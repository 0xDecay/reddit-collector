-- ponytail: sqlite, stdlib only. no ORM, no migrations tool.
-- One row per Reddit item, keyed by permalink so re-polling is idempotent.
CREATE TABLE IF NOT EXISTS items (
  permalink    TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,            -- 'post' | 'comment'
  subreddit    TEXT NOT NULL,
  author       TEXT,
  title        TEXT,
  body         TEXT,
  parent_title TEXT,                     -- comments: the post it sits on
  created_utc  TEXT NOT NULL,            -- ISO8601 from feed <updated>
  fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_sub_created ON items(subreddit, created_utc);

-- One row per HTTP poll. This is what makes the 25-item feed window auditable:
-- if a poll's OLDEST item is newer than the previous poll's NEWEST item, the
-- window advanced past us and items were silently lost -> gap_warning.
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
);
CREATE INDEX IF NOT EXISTS idx_polls_sub ON polls(subreddit, kind, polled_at);
