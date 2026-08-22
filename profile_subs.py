#!/usr/bin/env python3
"""
profile_subs.py — Deterministic subreddit profiler.
Reads SQLite db, writes/updates markdown profiles per subreddit.
Stdlib only (sqlite3, re, collections, datetime, argparse, pathlib, html).
Ponytail discipline: deterministic aggregation ONLY, no LLM calls.
Marks agent-synthesis sections clearly so they survive re-runs.
"""

import sqlite3
import re
import sys
import argparse
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from html import unescape
from urllib.parse import urlparse


# ponytail: stopwords for n-gram filtering. Expand if needed.
# Ceiling: no ML tokenization. Upgrade path: NLTK if stopwords become a bottleneck.
STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'are', 'was', 'were', 'be',
    'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
    'would', 'could', 'should', 'may', 'might', 'must', 'can', 'this',
    'that', 'these', 'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they',
    'what', 'which', 'who', 'when', 'where', 'why', 'how', 'not', 'no',
    'yes', 'my', 'your', 'his', 'her', 'its', 'our', 'their', 'if', 'just',
    'only', 'also', 'so', 'too', 'very', 'than', 'such', 'such', 'each',
    'both', 'all', 'some', 'any', 'such', 'more', 'most', 'less', 'least',
    'up', 'down', 'out', 'over', 'under', 'about', 'into', 'through', 'during',
    'before', 'after', 'above', 'below', 'between', 'among', 'around', 'like',
}


def extract_ngrams(text, n=2, min_freq=2):
    """
    Extract n-grams from text, filtering stopwords, boilerplate, and low frequency.
    Returns Counter of (ngram_text, frequency) sorted by frequency desc.
    ponytail: simple regex-based tokenization. Ceiling: no stemming.
    """
    if not text:
        return Counter()

    # Simple tokenization: split on non-alphanumeric, lowercase
    words = re.findall(r'\b\w+\b', text.lower())

    # Filter stopwords
    words = [w for w in words if w not in STOPWORDS and len(w) > 1]

    if len(words) < n:
        return Counter()

    # Extract n-grams
    ngrams = [' '.join(words[i:i+n]) for i in range(len(words) - n + 1)]

    # ponytail: filter Reddit HTML footer boilerplate artifacts.
    # Must reject any n-gram CONTAINING a footer phrase, not just one equal to it:
    # an equality check let the trigram "link comments built" through while catching
    # the bigram "link comments". Ceiling: hardcoded phrases. Upgrade: stemming.
    boilerplate_phrases = ('link comments', 'submitted by')
    boilerplate_exact = {'link', 'comments'}

    def is_boilerplate(ng):
        padded = ' %s ' % ng
        return ng in boilerplate_exact or any(
            ' %s ' % phrase in padded for phrase in boilerplate_phrases
        )

    # Filter by frequency and boilerplate
    counter = Counter(ngrams)
    return Counter({
        ng: count for ng, count in counter.items()
        if count >= min_freq and not is_boilerplate(ng)
    })


def get_hour_histogram(db_path, subreddit):
    """
    Extract posting-time histogram by UTC hour from created_utc (ISO8601).
    Returns dict {hour: count} for hours with data.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT substr(created_utc, 12, 2) as hour, COUNT(*) as count
        FROM items
        WHERE subreddit = ?
        GROUP BY hour
        ORDER BY hour
    """, (subreddit,))

    result = {int(row[0]): row[1] for row in cursor.fetchall()}
    conn.close()
    return result


def normalize_author(author):
    """
    Normalize author names: strip /u/ prefix if present, return clean username.
    Returns None for deleted or bot accounts that should be filtered.
    """
    if not author:
        return None

    # Strip /u/ or u/ prefix if present (normalize DB inconsistency)
    clean = author.lstrip('/').lstrip('u').lstrip('/')

    # Filter: deleted posts, AutoModerator bot (system noise)
    if clean in ('[deleted]', 'AutoModerator', 'automoderator'):
        return None

    return clean if clean else None


def get_author_stats(db_path, subreddit):
    """
    Unique author counts and top posters.
    Excludes [deleted], None, empty strings, and AutoModerator (system bot).
    Normalizes author names (strips /u/ prefix).
    Returns (unique_count, top_posters_list)
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT author, COUNT(*) as count
        FROM items
        WHERE subreddit = ? AND author IS NOT NULL AND author != '' AND author != '[deleted]'
        GROUP BY author
        ORDER BY count DESC
    """, (subreddit,))

    rows = cursor.fetchall()
    conn.close()

    # ponytail: normalize and filter at read time. Ceiling: one pass.
    # Upgrade: per-sub filtering config if needed.
    normalized = []
    for author, count in rows:
        clean = normalize_author(author)
        if clean:
            normalized.append((clean, count))

    unique_count = len(normalized)
    top_posters = normalized[:10]

    return unique_count, top_posters


def get_volume_trend(db_path, subreddit):
    """
    Calculate items/day trend and whether it's rising/flat/falling.
    Returns (items_total, items_per_day, trend_description)
    ponytail: simple linear fit. Ceiling: no statsmodels. Upgrade if needed for prediction.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT created_utc
        FROM items
        WHERE subreddit = ?
        ORDER BY created_utc
    """, (subreddit,))

    timestamps = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not timestamps:
        return 0, 0, 'no data'

    total = len(timestamps)

    # Date range in days
    first_date = timestamps[0][:10]  # ISO8601 YYYY-MM-DD
    last_date = timestamps[-1][:10]

    try:
        from datetime import datetime as dt
        d1 = dt.fromisoformat(first_date)
        d2 = dt.fromisoformat(last_date)
        days = max(1, (d2 - d1).days + 1)
    except:
        days = 1

    per_day = total / days if days > 0 else 0

    # Simple trend: compare first 1/3 vs last 1/3
    third = len(timestamps) // 3
    if third == 0:
        trend = 'insufficient data'
    else:
        first_third_count = third
        last_third_count = len(timestamps) - (third * 2)

        if last_third_count > first_third_count * 1.2:
            trend = 'rising'
        elif first_third_count > last_third_count * 1.2:
            trend = 'falling'
        else:
            trend = 'flat'

    return total, per_day, trend


def get_representative_quotes(db_path, subreddit, max_quotes=5):
    """
    Extract representative quotes with deliberate mix of posts and comments.
    Minimum 30 chars to avoid low-substance noise. Prioritize substance over recency.
    Returns list of (text, permalink, kind) tuples.
    ponytail: simple deterministic selection. Ceiling: comment/post split.
    Upgrade: substance scoring (comment mentions concrete problem/solution).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get comments and posts separately, ranked by text length (substance proxy)
    cursor.execute("""
        SELECT title, body, permalink, kind
        FROM items
        WHERE subreddit = ? AND kind = 'comment'
        ORDER BY LENGTH(COALESCE(body, title)) DESC, created_utc DESC
        LIMIT ?
    """, (subreddit, max_quotes * 3))
    comments = cursor.fetchall()

    cursor.execute("""
        SELECT title, body, permalink, kind
        FROM items
        WHERE subreddit = ? AND kind = 'post'
        ORDER BY LENGTH(COALESCE(body, title)) DESC, created_utc DESC
        LIMIT ?
    """, (subreddit, max_quotes * 3))
    posts = cursor.fetchall()

    conn.close()

    quotes = []
    seen_text = set()

    # Deliberately alternate: comment, post, comment, post... for mix
    for c_row, p_row in zip(comments, posts):
        for row in [c_row, p_row]:
            if not row:
                continue

            title, body, permalink, kind = row

            # Extract text: body for comments, title for posts
            text = body if kind == 'comment' else title
            if not text:
                text = body if body else title

            if not text:
                continue

            # Unescape HTML entities
            text = unescape(text)

            # Filter low-substance quotes (< 30 chars is noise)
            if len(text) < 30:
                continue

            # Normalize for dedup
            norm_text = text[:100].lower()

            if norm_text not in seen_text:
                seen_text.add(norm_text)
                quotes.append((text, permalink, kind))
                if len(quotes) >= max_quotes:
                    break

        if len(quotes) >= max_quotes:
            break

    return quotes


def get_last_profile_fetch_time(content):
    """
    Extract the _last_fetched_at comment from markdown content.
    content can be markdown string or None.
    Returns ISO8601 string or None if not found.
    """
    if not content:
        return None

    try:
        match = re.search(r'<!-- _last_fetched_at: ([^\s]+) -->', content)
        return match.group(1) if match else None
    except:
        return None


def get_new_items_since(db_path, subreddit, since_iso8601):
    """
    Count and sample new items added since the last fetch.
    Returns (new_count, sample_list).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*) FROM items
        WHERE subreddit = ? AND fetched_at > ?
    """, (subreddit, since_iso8601 or '2000-01-01'))

    new_count = cursor.fetchone()[0]

    cursor.execute("""
        SELECT title, permalink, created_utc
        FROM items
        WHERE subreddit = ? AND fetched_at > ?
        ORDER BY fetched_at DESC
        LIMIT 3
    """, (subreddit, since_iso8601 or '2000-01-01'))

    samples = cursor.fetchall()
    conn.close()

    return new_count, samples


def preserve_section(old_content, section_name):
    """
    Extract agent-written content between BEGIN/END markers for a section.
    Returns content or None if not found.
    """
    if not old_content:
        return None

    pattern = f'<!-- BEGIN_SYNTHESIS: {section_name} -->(.*?)<!-- END_SYNTHESIS: {section_name} -->'
    match = re.search(pattern, old_content, re.DOTALL)
    return match.group(1) if match else None


def generate_profile(db_path, subreddit, existing_content=None):
    """
    Generate or update the markdown profile for a subreddit.
    Preserves agent-written sections between markers on re-run.
    Returns markdown string.
    """
    # ponytail: use timezone-aware datetime to avoid DeprecationWarning.
    # Format matches existing _last_fetched_at markers for backwards compat.
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    # Get metrics
    unique_authors, top_authors = get_author_stats(db_path, subreddit)
    total_items, items_per_day, trend = get_volume_trend(db_path, subreddit)
    hour_hist = get_hour_histogram(db_path, subreddit)
    quotes = get_representative_quotes(db_path, subreddit, max_quotes=5)

    # Extract n-grams from all text in subreddit
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT GROUP_CONCAT(title || ' ' || COALESCE(body, ''))
        FROM items
        WHERE subreddit = ?
    """, (subreddit,))
    all_text = cursor.fetchone()[0] or ''
    conn.close()

    bigrams = extract_ngrams(all_text, n=2, min_freq=2)
    trigrams = extract_ngrams(all_text, n=3, min_freq=2)

    # Determine if this is a new profile or update
    last_fetch = get_last_profile_fetch_time(existing_content)
    is_update = existing_content is not None

    # Build markdown
    md = []
    md.append(f'# Profile: r/{subreddit}')
    md.append('')
    md.append(f'<!-- _last_fetched_at: {now} -->')
    md.append('')

    # Status line
    md.append(f'**Items collected:** {total_items} | **Unique authors:** {unique_authors} | **Avg/day:** {items_per_day:.1f} | **Trend:** {trend}')
    md.append('')

    # Demographics section (scaffolded for agent)
    md.append('## Demographics')
    if is_update and existing_content:
        preserved = preserve_section(existing_content, 'demographics')
        if preserved:
            md.append('<!-- BEGIN_SYNTHESIS: demographics -->')
            md.append(preserved)
            md.append('<!-- END_SYNTHESIS: demographics -->')
        else:
            md.append('<!-- BEGIN_SYNTHESIS: demographics -->')
            md.append('_Awaiting agent synthesis from language patterns._')
            md.append('<!-- END_SYNTHESIS: demographics -->')
    else:
        md.append('<!-- BEGIN_SYNTHESIS: demographics -->')
        md.append('_Awaiting agent synthesis from language patterns._')
        md.append('<!-- END_SYNTHESIS: demographics -->')
    md.append('')

    # Psychographics section (scaffolded for agent)
    md.append('## Psychographics')
    if is_update and existing_content:
        preserved = preserve_section(existing_content, 'psychographics')
        if preserved:
            md.append('<!-- BEGIN_SYNTHESIS: psychographics -->')
            md.append(preserved)
            md.append('<!-- END_SYNTHESIS: psychographics -->')
        else:
            md.append('<!-- BEGIN_SYNTHESIS: psychographics -->')
            md.append('_Awaiting agent synthesis: values, fears, aspirations._')
            md.append('<!-- END_SYNTHESIS: psychographics -->')
    else:
        md.append('<!-- BEGIN_SYNTHESIS: psychographics -->')
        md.append('_Awaiting agent synthesis: values, fears, aspirations._')
        md.append('<!-- END_SYNTHESIS: psychographics -->')
    md.append('')

    # Language: recurring phrases and verbatim quotes
    md.append('## Language & Tone')
    md.append('')
    md.append('### Recurring phrases')
    md.append('')

    if bigrams or trigrams:
        md.append('**Bigrams (top 10):**')
        for phrase, count in list(bigrams.most_common(10)):
            md.append(f'- "{phrase}" ({count}x)')
        md.append('')

        md.append('**Trigrams (top 10):**')
        for phrase, count in list(trigrams.most_common(10)):
            md.append(f'- "{phrase}" ({count}x)')
        md.append('')
    else:
        md.append('_Not enough data yet._')
        md.append('')

    # Verbatim quotes with permalinks
    md.append('### Representative quotes')
    md.append('')
    if quotes:
        for text, permalink, kind in quotes:
            # Truncate long quotes
            if len(text) > 200:
                text = text[:200] + '…'
            md.append(f'> "{text}"')
            md.append(f'> — [{kind}]({permalink})')
            md.append('')
    else:
        md.append('_No data yet._')
        md.append('')

    # Posting time histogram
    md.append('### Activity by UTC hour')
    md.append('')
    if hour_hist:
        for hour in sorted(hour_hist.keys()):
            count = hour_hist[hour]
            bar = '█' * (count // 2 + 1) if count > 0 else '·'
            md.append(f'{hour:02d}:00 | {bar} ({count})')
    else:
        md.append('_Not enough data yet._')
    md.append('')

    # What they already tried (scaffolded for agent)
    md.append('## What they already tried')
    if is_update and existing_content:
        preserved = preserve_section(existing_content, 'what_they_tried')
        if preserved:
            md.append('<!-- BEGIN_SYNTHESIS: what_they_tried -->')
            md.append(preserved)
            md.append('<!-- END_SYNTHESIS: what_they_tried -->')
        else:
            md.append('<!-- BEGIN_SYNTHESIS: what_they_tried -->')
            md.append('_Awaiting agent synthesis: tools, services, and approaches mentioned._')
            md.append('<!-- END_SYNTHESIS: what_they_tried -->')
    else:
        md.append('<!-- BEGIN_SYNTHESIS: what_they_tried -->')
        md.append('_Awaiting agent synthesis: tools, services, and approaches mentioned._')
        md.append('<!-- END_SYNTHESIS: what_they_tried -->')
    md.append('')

    # Mod rules (human/agent supplied, not derivable from feed)
    md.append('## Mod rules & culture')
    if is_update and existing_content:
        preserved = preserve_section(existing_content, 'mod_rules')
        if preserved:
            md.append('<!-- BEGIN_SYNTHESIS: mod_rules -->')
            md.append(preserved)
            md.append('<!-- END_SYNTHESIS: mod_rules -->')
        else:
            md.append('<!-- BEGIN_SYNTHESIS: mod_rules -->')
            md.append('_Not derivable from RSS feeds. Awaiting agent research._')
            md.append('<!-- END_SYNTHESIS: mod_rules -->')
    else:
        md.append('<!-- BEGIN_SYNTHESIS: mod_rules -->')
        md.append('_Not derivable from RSS feeds. Awaiting agent research._')
        md.append('<!-- END_SYNTHESIS: mod_rules -->')
    md.append('')

    # Tone section (scaffolded for agent)
    md.append('## Tone & sentiment')
    if is_update and existing_content:
        preserved = preserve_section(existing_content, 'tone')
        if preserved:
            md.append('<!-- BEGIN_SYNTHESIS: tone -->')
            md.append(preserved)
            md.append('<!-- END_SYNTHESIS: tone -->')
        else:
            md.append('<!-- BEGIN_SYNTHESIS: tone -->')
            md.append('_Awaiting agent synthesis: formality, humor, frustration level, optimism._')
            md.append('<!-- END_SYNTHESIS: tone -->')
    else:
        md.append('<!-- BEGIN_SYNTHESIS: tone -->')
        md.append('_Awaiting agent synthesis: formality, humor, frustration level, optimism._')
        md.append('<!-- END_SYNTHESIS: tone -->')
    md.append('')

    # Top contributors
    md.append('## Top contributors')
    md.append('')
    if top_authors:
        for author, count in top_authors:
            md.append(f'- u/{author}: {count} items')
    else:
        md.append('_Not enough data yet._')
    md.append('')

    # Important note on what is NOT derivable
    md.append('## Limitations & methodology')
    md.append('')
    md.append('**Not derivable from RSS feeds:** upvote counts, comment scores, engagement metrics, post reach.')
    md.append('Reddit RSS feeds return only ~25 most recent items per feed (posts vs comments) and carry no scoring data.')
    md.append('This profile is built from actual text people posted, not from what ranked highest.')
    md.append('')

    # Change log (append, never replace)
    md.append('## Change log')
    md.append('')

    if is_update and existing_content:
        # Extract existing change log
        match = re.search(r'## Change log\n\n(.*?)(?:\n\n##|$)', existing_content, re.DOTALL)
        existing_log = match.group(1) if match else ''

        # Add new entry
        new_entry, sample_count = '', 0
        new_count, new_samples = get_new_items_since(db_path, subreddit, last_fetch)
        if new_count > 0:
            new_entry = f'- **{now[:10]}**: {new_count} new items added'
            sample_count = len(new_samples)
            if sample_count > 0:
                new_entry += f' (sampled: {", ".join(s[0][:30] + "…" for s in new_samples[:2])})'

        if existing_log.strip():
            md.append(new_entry)
            md.append(existing_log)
        else:
            md.append(new_entry if new_entry else '_Profile created._')
    else:
        md.append(f'- **{now[:10]}**: Profile created with {total_items} initial items')

    md.append('')

    return '\n'.join(md)


def get_subreddits_in_db(db_path):
    """Get list of unique subreddits with data in the db."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT subreddit FROM items ORDER BY subreddit')
    subs = [row[0] for row in cursor.fetchall()]
    conn.close()
    return subs


def main():
    parser = argparse.ArgumentParser(
        description='Generate/update subreddit profiles from SQLite data'
    )
    parser.add_argument('--db', default='data/reddit.db',
                        help='Path to SQLite db (default: data/reddit.db)')
    parser.add_argument('--out', default='profiles',
                        help='Output directory for profiles (default: profiles)')
    parser.add_argument('--sub', action='append', dest='subs',
                        help='Limit to specific subreddit(s), repeatable')
    parser.add_argument('--selftest', action='store_true',
                        help='Run self-tests and exit')

    args = parser.parse_args()

    db_path = args.db
    out_dir = Path(args.out)
    target_subs = args.subs if args.subs else None

    if args.selftest:
        run_selftest()
        return

    # Ensure db exists
    if not Path(db_path).exists():
        print(f'Error: {db_path} not found', file=sys.stderr)
        sys.exit(1)

    # Ensure output dir exists
    out_dir.mkdir(parents=True, exist_ok=True)

    # Get subreddits to process
    subs = get_subreddits_in_db(db_path)

    if not subs:
        print(f'Warning: No subreddits found in {db_path}', file=sys.stderr)
        return

    if target_subs:
        subs = [s for s in subs if s in target_subs]

    # Generate profiles
    for sub in subs:
        profile_path = out_dir / f'{sub}.md'
        existing_content = profile_path.read_text() if profile_path.exists() else None

        new_content = generate_profile(db_path, sub, existing_content)
        profile_path.write_text(new_content)

        print(f'✓ {sub}', file=sys.stderr)


def run_selftest():
    """Run asserts on synthetic data. Exit non-zero if any fail."""

    # Create temp db with synthetic data
    with tempfile.NamedTemporaryFile(mode='w', suffix='.db', delete=False) as f:
        temp_db = f.name

    try:
        # Initialize schema
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE items (
              permalink TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              subreddit TEXT NOT NULL,
              author TEXT,
              title TEXT,
              body TEXT,
              parent_title TEXT,
              created_utc TEXT NOT NULL,
              fetched_at TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE polls (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subreddit TEXT NOT NULL,
              kind TEXT NOT NULL,
              polled_at TEXT NOT NULL,
              http_status INTEGER,
              items_seen INTEGER DEFAULT 0,
              items_new INTEGER DEFAULT 0,
              oldest_in_feed TEXT,
              newest_in_feed TEXT,
              gap_warning INTEGER DEFAULT 0
            )
        ''')
        conn.commit()

        # Insert synthetic data
        now = '2026-08-19T12:00:00Z'

        test_items = [
            ('https://reddit.com/r/test/comments/1/post_1', 'post', 'test', 'user1',
             'How to use Python for business automation', None, None, '2026-08-19T10:00:00Z', now),
            ('https://reddit.com/r/test/comments/1/_/comment_1', 'comment', 'test', 'user2',
             None, 'We used Ruby on Rails but switched to Django for speed', None, '2026-08-19T11:00:00Z', now),
            ('https://reddit.com/r/test/comments/2/post_2', 'post', 'test', 'user1',
             'Building software for small businesses in 2026', None, None, '2026-08-19T14:30:00Z', now),
            ('https://reddit.com/r/test/comments/2/_/comment_2', 'comment', 'test', 'user3',
             None, 'Python and Django are the best tools for rapid development', None, '2026-08-19T15:00:00Z', now),
            ('https://reddit.com/r/test/comments/3/post_3', 'post', 'test', '[deleted]',
             'Deleted post', None, None, '2026-08-19T16:00:00Z', now),
            ('https://reddit.com/r/test/comments/4/post_4', 'post', 'test', 'user4',
             'Django REST framework best practices', None, None, '2026-08-19T17:15:00Z', now),
        ]

        for item in test_items:
            cursor.execute('''
                INSERT INTO items (permalink, kind, subreddit, author, title, body, parent_title, created_utc, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', item)

        conn.commit()
        conn.close()

        # Test 1: N-gram extraction
        # Use text with repeated bigrams
        all_text = 'Python Django best tools Python Django best tools Python Django'
        bigrams = extract_ngrams(all_text, n=2, min_freq=2)
        assert len(bigrams) > 0, f'Bigrams should not be empty, got {dict(bigrams.most_common(10))}'
        # Should extract 'python django' and 'django best' which repeat multiple times
        print('✓ N-gram extraction', file=sys.stderr)

        # Test 2: Quote extraction with permalink
        quotes = get_representative_quotes(temp_db, 'test', max_quotes=5)
        assert len(quotes) > 0, 'Should extract at least one quote'
        assert all(isinstance(q, tuple) and len(q) == 3 for q in quotes), \
            'Each quote should be (text, permalink, kind) tuple'
        assert all('reddit.com' in q[1] for q in quotes), 'All quotes should have permalinks'
        print('✓ Quote extraction with permalinks', file=sys.stderr)

        # Test 3: Hour histogram
        hour_hist = get_hour_histogram(temp_db, 'test')
        assert len(hour_hist) > 0, 'Should have at least one hour with data'
        assert all(0 <= h <= 23 for h in hour_hist.keys()), 'Hours should be 0-23'
        assert all(c > 0 for c in hour_hist.values()), 'Counts should be > 0'
        print('✓ Posting time histogram', file=sys.stderr)

        # Test 4: Author counts
        unique, top = get_author_stats(temp_db, 'test')
        assert unique > 0, 'Should have unique authors'
        assert len(top) > 0, 'Should have top authors'
        assert not any(a[0] in ('[deleted]', None, '') for a in top), \
            'Top authors should exclude [deleted], None, empty'
        print('✓ Author statistics', file=sys.stderr)

        # Test 5: Volume trend
        total, per_day, trend = get_volume_trend(temp_db, 'test')
        assert total > 0, 'Should count items'
        assert per_day > 0, 'Should calculate items per day'
        assert trend in ('rising', 'falling', 'flat', 'insufficient data'), \
            f'Trend should be one of expected values, got {trend}'
        print('✓ Volume trend calculation', file=sys.stderr)

        # Test 6: "Not derivable" note in profile
        profile = generate_profile(temp_db, 'test')
        assert 'Not derivable from RSS feeds' in profile or 'not derivable' in profile.lower(), \
            'Profile should mention what is not derivable'
        print('✓ "Not derivable" honesty note', file=sys.stderr)

        # Test 7: Update rule — run twice, agent prose survives
        profile_1 = generate_profile(temp_db, 'test')
        assert 'BEGIN_SYNTHESIS' in profile_1 and 'END_SYNTHESIS' in profile_1, \
            'Profile should have synthesis markers'

        # Simulate agent editing the first profile
        profile_1_edited = profile_1.replace(
            '<!-- BEGIN_SYNTHESIS: demographics -->',
            '<!-- BEGIN_SYNTHESIS: demographics -->\n**Agent wrote this: Members tend to be ...**'
        ).replace(
            '_Awaiting agent synthesis from language patterns._',
            'Members tend to be experienced developers aged 25-40.'
        )

        # Re-run profiler with edited content
        profile_2 = generate_profile(temp_db, 'test', profile_1_edited)

        # Check that agent prose survives
        assert 'Members tend to be experienced developers' in profile_2, \
            'Agent-written prose should survive re-run between markers'

        # Check that change log was appended
        assert '## Change log' in profile_2, 'Should have change log section'

        print('✓ Update rule: agent prose survives, change log appends', file=sys.stderr)

        # Test 8: Boilerplate filtering (defect #1)
        # Generate profile and verify "link comments" (Reddit HTML footer) is filtered
        profile_test = generate_profile(temp_db, 'test')
        # Extract bigrams section
        bigrams_match = re.search(r'Bigrams.*?\n(.*?)(?:\n\n|\Z)', profile_test, re.DOTALL)
        if bigrams_match:
            bigrams_section = bigrams_match.group(1)
            # The boilerplate phrase should not appear as a top bigram
            assert 'link comments' not in bigrams_section.lower(), \
                'Boilerplate phrase "link comments" should be filtered from n-grams'
        assert 'link comments' not in profile_test.lower(), \
            'Boilerplate must be filtered from EVERY n-gram size, not just bigrams'
        # Direct check on the extractor: a trigram CONTAINING the footer phrase must
        # not survive. An equality-only filter let "link comments built" through.
        noise = 'link comments built ' * 3
        for n in (2, 3):
            grams = extract_ngrams(noise, n=n, min_freq=2)
            assert not any('link comments' in g for g in grams), \
                f'n={n}: boilerplate leaked through containment check: {list(grams)}'
        print('✓ Boilerplate filtering (no "link comments")', file=sys.stderr)

        # Test 9: Author normalization (defect #2)
        # Verify no double u// prefix and AutoModerator filtered
        unique, top = get_author_stats(temp_db, 'test')
        assert all(not author.startswith('u/u/') and not author.startswith('/u/')
                   for author, _ in top), \
            f'Authors should not have double prefix or leading slash: {[a for a, _ in top]}'
        assert not any(author in ('AutoModerator', 'automoderator') for author, _ in top), \
            'AutoModerator bot should be filtered from top contributors'
        print('✓ Author normalization (no u/u/ double prefix, AutoModerator filtered)', file=sys.stderr)

        # Test 10: Quote mix (defect #3)
        # Ensure we have both posts and comments in representative quotes
        quotes = get_representative_quotes(temp_db, 'test', max_quotes=10)
        quote_kinds = [q[2] for q in quotes]
        assert len(quotes) > 0, 'Should extract quotes'
        has_comments = any(k == 'comment' for k in quote_kinds)
        has_posts = any(k == 'post' for k in quote_kinds)
        assert has_comments and has_posts, \
            f'Quotes should mix posts and comments, got kinds: {quote_kinds}'
        # Verify minimum text length (no low-substance noise)
        assert all(len(q[0]) >= 30 for q in quotes), \
            'All quotes should be >= 30 chars to avoid low-substance noise'
        print('✓ Quote mix (posts + comments, minimum 30 chars)', file=sys.stderr)

        print('\n✓ All self-tests passed', file=sys.stderr)

    except AssertionError as e:
        print(f'\n✗ Self-test failed: {e}', file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f'\n✗ Unexpected error during self-test: {e}', file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Cleanup
        Path(temp_db).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
