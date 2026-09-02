#!/usr/bin/env python
"""Print context from tools/index.db.

    python tools/show.py thread 443883            # every post of a Prince.org thread, in order
    python tools/show.py thread 443883 --page 2
    python tools/show.py msg 1300849682700046449  # a Discord message with its neighbours
    python tools/show.py msg 1300849682700046449 --before 5 --after 30
"""

from __future__ import annotations

import argparse
import sqlite3
import textwrap
from pathlib import Path

INDEX_DB = Path(__file__).resolve().parent / "index.db"


def wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(text, width=110, initial_indent=indent, subsequent_indent=indent)


def show_thread(con, thread_id: int, page: int | None, grep: str | None) -> None:
    sql = "SELECT page, reply_no, author, posted, subject, body, quoted, url, msg_id FROM princeorg WHERE thread_id = ?"
    params: list = [thread_id]
    if page:
        sql += " AND page = ?"; params.append(page)
    sql += " ORDER BY page, reply_no"
    rows = con.execute(sql, params).fetchall()
    if not rows:
        print("no posts (thread not indexed?)"); return
    title = con.execute("SELECT thread_title FROM princeorg WHERE thread_id = ? LIMIT 1", (thread_id,)).fetchone()[0]
    print(f"### {title}  (thread {thread_id}, {len(rows)} posts)")
    for pg, no, author, posted, subject, body, quoted, url, msg_id in rows:
        if grep and grep.lower() not in (body + " " + quoted).lower():
            continue
        print(f"\n--- #{no} {posted} {author}  {url}#msg_{msg_id}")
        if subject and subject != title:
            print(f"    [{subject}]")
        if quoted:
            print(wrap("> " + quoted[:600], "    "))
        print(wrap(body))


def show_msg(con, mid: str, before: int, after: int) -> None:
    row = con.execute("SELECT channel_id, timestamp, server, channel FROM discord WHERE id = ?", (mid,)).fetchone()
    if not row:
        print("message not indexed"); return
    channel_id, ts, server, channel = row
    print(f"### {server} #{channel}")
    prev = con.execute("""SELECT timestamp, author, display_name, content, attachments, url, id FROM discord
                          WHERE channel_id = ? AND timestamp < ? ORDER BY timestamp DESC LIMIT ?""",
                       (channel_id, ts, before)).fetchall()[::-1]
    nxt = con.execute("""SELECT timestamp, author, display_name, content, attachments, url, id FROM discord
                         WHERE channel_id = ? AND timestamp >= ? ORDER BY timestamp LIMIT ?""",
                      (channel_id, ts, after + 1)).fetchall()
    for t, author, disp, content, att, url, i in prev + nxt:
        mark = ">>> " if i == mid else "    "
        who = f"{disp} ({author})" if disp and disp != author else author
        print(f"\n{mark}{t} {who}  {url}")
        print(wrap(content))
        if att:
            print(wrap("[attachments/embeds] " + att[:300]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("thread"); t.add_argument("thread_id", type=int); t.add_argument("--page", type=int)
    t.add_argument("--grep", help="only posts containing this text")
    m = sub.add_parser("msg"); m.add_argument("message_id"); m.add_argument("--before", type=int, default=5)
    m.add_argument("--after", type=int, default=15)
    ap.add_argument("--db", type=Path, default=INDEX_DB)
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    if args.cmd == "thread":
        show_thread(con, args.thread_id, args.page, args.grep)
    else:
        show_msg(con, args.message_id, args.before, args.after)


if __name__ == "__main__":
    main()
