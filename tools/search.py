#!/usr/bin/env python
"""Query tools/index.db (built by build_index.py).

    python tools/search.py 'velvet kitty cat lossy'
    python tools/search.py 'gett off sharp' --source princeorg --forum 7   # one forum (ID or part of its name)
    python tools/search.py '"drop out" OR dropout' --source princeorg --since 2017-06-01
    python tools/search.py 'girl NEAR/5 slower' --source discord --author someone
    python tools/search.py 'timeless "i wonder"' --source reddit --since 2026-01-01
    python tools/search.py --queries queries.txt --limit 30 > hits.txt

The query uses SQLite FTS5 syntax (AND is implicit, OR, NOT, "phrases",
prefix*, NEAR/N). Matching is diacritics-insensitive and case-insensitive.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

INDEX_DB = Path(__file__).resolve().parent / "index.db"


def run(con: sqlite3.Connection, query: str, source: str, since: str | None,
        until: str | None, author: str | None, limit: int, private: bool, width: int,
        forum: str | None = None) -> int:
    n = 0
    if source in ("all", "princeorg"):
        sql = """SELECT posted, author, thread_title, subject,
                        snippet(princeorg, 8, '[', ']', ' … ', ?) AS snip, url, msg_id, thread_id,
                        forum_id, forum
                 FROM princeorg WHERE princeorg MATCH ?"""
        params: list = [width, query]
        if forum:
            if forum.isdigit():
                sql += " AND forum_id = ?"; params.append(int(forum))
            else:
                sql += " AND forum LIKE ?"; params.append(f"%{forum}%")
        if since:
            sql += " AND posted >= ?"; params.append(since)
        if until:
            sql += " AND posted <= ?"; params.append(until + " 99")
        if author:
            sql += " AND author = ? COLLATE NOCASE"; params.append(author)
        sql += " ORDER BY posted LIMIT ?"; params.append(limit)
        for posted, auth, title, subject, snip, url, msg_id, tid, fid, fname in con.execute(sql, params):
            n += 1
            where = f"  [{fname}]" if fname and str(fid) != "7" else ""
            print(f"[org] {posted} | {auth} | {title[:70]}{where}")
            print(f"      {snip}")
            print(f"      {url}#msg_{msg_id}")
    if source in ("all", "discord"):
        sql = """SELECT timestamp, server, channel, author, display_name,
                        snippet(discord, 8, '[', ']', ' … ', ?) AS snip, url, id, private
                 FROM discord WHERE discord MATCH ?"""
        params = [width, query]
        if not private:
            sql += " AND private = 0"
        if since:
            sql += " AND timestamp >= ?"; params.append(since)
        if until:
            sql += " AND timestamp <= ?"; params.append(until + " 99")
        if author:
            sql += " AND (author = ? COLLATE NOCASE OR display_name = ? COLLATE NOCASE)"; params += [author, author]
        sql += " ORDER BY timestamp LIMIT ?"; params.append(limit)
        for ts, server, channel, auth, disp, snip, url, mid, priv in con.execute(sql, params):
            n += 1
            who = f"{disp} ({auth})" if disp and disp != auth else auth
            print(f"[dc]  {ts} | {who} | {server} #{channel}{' [PRIVATE]' if priv else ''}")
            print(f"      {snip}")
            print(f"      {url or 'id=' + mid}")
    if source in ("all", "reddit"):
        sql = """SELECT created, author, subreddit, kind, title,
                        snippet(reddit, 6, '[', ']', ' … ', ?) AS snip, url
                 FROM reddit WHERE reddit MATCH ?"""
        params = [width, query]
        if since:
            sql += " AND created >= ?"; params.append(since)
        if until:
            sql += " AND created <= ?"; params.append(until + " 99")
        if author:
            sql += " AND author = ? COLLATE NOCASE"; params.append(author)
        sql += " ORDER BY created LIMIT ?"; params.append(limit)
        for created, auth, sub, kind, title, snip, url in con.execute(sql, params):
            n += 1
            print(f"[rd]  {created[:16]} | {auth} | r/{sub} | {title[:70]}{'' if kind == 'post' else ' (comment)'}")
            print(f"      {snip}")
            print(f"      {url}")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="FTS5 query")
    ap.add_argument("--queries", type=Path, help="file with one query per line (# comments allowed)")
    ap.add_argument("--db", type=Path, default=INDEX_DB)
    ap.add_argument("--source", choices=["all", "princeorg", "discord", "reddit"], default="all")
    ap.add_argument("--since"); ap.add_argument("--until"); ap.add_argument("--author")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--width", type=int, default=40, help="snippet size in tokens")
    ap.add_argument("--private", action="store_true", help="include private conversations")
    ap.add_argument("--forum", help="Prince.org forum: ID (e.g. 7) or part of its name")
    args = ap.parse_args()
    if not args.query and not args.queries:
        ap.error("give a query or --queries FILE")
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    queries = [args.query] if args.query else [
        q.strip() for q in args.queries.read_text().splitlines() if q.strip() and not q.startswith("#")]
    for q in queries:
        if len(queries) > 1:
            print(f"\n===== {q} =====")
        try:
            n = run(con, q, args.source, args.since, args.until, args.author, args.limit, args.private, args.width,
                    args.forum)
        except sqlite3.OperationalError as e:
            print(f"query error: {e}", file=sys.stderr)
            continue
        print(f"-- {n} hit(s) for: {q}")


if __name__ == "__main__":
    main()
