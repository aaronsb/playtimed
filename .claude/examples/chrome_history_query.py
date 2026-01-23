#!/usr/bin/env python3
"""
Chrome History Database Query Examples for playtimed

These examples demonstrate how to query Chrome's History SQLite database
to get recent URLs. Validated on brick (Anders' machine) on 2026-01-22.

Context:
- Chrome locks its History DB while running
- Must copy the file first, then query the copy
- History shows URLs visited, not currently open tabs
- Useful as fallback when window title parsing fails

Database Location:
    ~/.config/google-chrome/Default/History

Important Tables:
    - urls: URL metadata (url, title, visit_count, last_visit_time)
    - visits: Individual visit records

Timestamp Format:
    Chrome uses WebKit timestamps: microseconds since 1601-01-01
    Convert to Unix: (webkit_timestamp / 1000000) - 11644473600

Usage:
    # Copy history first (Chrome locks the file)
    sudo cp /home/anders/.config/google-chrome/Default/History /tmp/chrome_history

    # Then query
    python3 chrome_history_query.py /tmp/chrome_history

Related: ADR-001-browser-domain-tracking.md
"""

import sqlite3
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse


def copy_chrome_history(chrome_profile: Path) -> Path:
    """
    Copy Chrome's History DB to a temp file (avoids lock issues).

    Args:
        chrome_profile: Path to Chrome profile (e.g., ~/.config/google-chrome/Default)

    Returns:
        Path to temporary copy of History DB
    """
    history_path = chrome_profile / 'History'
    if not history_path.exists():
        raise FileNotFoundError(f"History not found at {history_path}")

    temp_path = Path(tempfile.mktemp(suffix='.db'))
    shutil.copy2(history_path, temp_path)
    return temp_path


def webkit_to_datetime(webkit_timestamp: int) -> datetime:
    """Convert WebKit timestamp to Python datetime."""
    # WebKit: microseconds since 1601-01-01
    # Unix: seconds since 1970-01-01
    unix_timestamp = (webkit_timestamp / 1000000) - 11644473600
    return datetime.fromtimestamp(unix_timestamp)


def get_recent_urls(history_db: Path, limit: int = 30) -> list[dict]:
    """
    Get recently visited URLs from Chrome History.

    Args:
        history_db: Path to History SQLite file (or copy)
        limit: Maximum number of URLs to return

    Returns:
        List of dicts with url, title, domain, visit_time
    """
    conn = sqlite3.connect(history_db)
    cursor = conn.execute("""
        SELECT url, title, last_visit_time
        FROM urls
        ORDER BY last_visit_time DESC
        LIMIT ?
    """, (limit,))

    results = []
    for url, title, visit_time in cursor:
        parsed = urlparse(url)
        results.append({
            'url': url,
            'title': title,
            'domain': parsed.netloc,
            'visit_time': webkit_to_datetime(visit_time),
        })

    conn.close()
    return results


def get_domain_visit_counts(history_db: Path, days: int = 7) -> dict[str, int]:
    """
    Get visit counts per domain for the last N days.

    Args:
        history_db: Path to History SQLite file
        days: Number of days to look back

    Returns:
        Dict mapping domain -> visit count
    """
    # Calculate cutoff timestamp
    cutoff = datetime.now().timestamp() + 11644473600
    cutoff = int((cutoff - (days * 86400)) * 1000000)

    conn = sqlite3.connect(history_db)
    cursor = conn.execute("""
        SELECT url, COUNT(*) as visits
        FROM urls
        WHERE last_visit_time > ?
        GROUP BY url
    """, (cutoff,))

    domain_counts = {}
    for url, count in cursor:
        parsed = urlparse(url)
        domain = parsed.netloc
        domain_counts[domain] = domain_counts.get(domain, 0) + count

    conn.close()
    return domain_counts


def lookup_domain_for_title(history_db: Path, title_fragment: str) -> Optional[str]:
    """
    Look up domain for a page title (fallback for window title parsing).

    Args:
        history_db: Path to History SQLite file
        title_fragment: Part of the page title to search for

    Returns:
        Domain string if found, None otherwise
    """
    conn = sqlite3.connect(history_db)
    cursor = conn.execute("""
        SELECT url FROM urls
        WHERE title LIKE ?
        ORDER BY last_visit_time DESC
        LIMIT 1
    """, (f'%{title_fragment}%',))

    row = cursor.fetchone()
    conn.close()

    if row:
        parsed = urlparse(row[0])
        return parsed.netloc

    return None


# -----------------------------------------------------------------------------
# Example usage
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 chrome_history_query.py <history_db_path>")
        print()
        print("Example:")
        print("  sudo cp ~/.config/google-chrome/Default/History /tmp/h.db")
        print("  python3 chrome_history_query.py /tmp/h.db")
        sys.exit(1)

    db_path = Path(sys.argv[1])

    print("=== Recent URLs ===")
    for item in get_recent_urls(db_path, limit=15):
        print(f"  {item['visit_time'].strftime('%H:%M')} | {item['domain'][:30]:<30} | {item['title'][:40]}")

    print()
    print("=== Domain Visit Counts (7 days) ===")
    counts = get_domain_visit_counts(db_path, days=7)
    for domain, count in sorted(counts.items(), key=lambda x: -x[1])[:20]:
        print(f"  {count:4d} | {domain}")
