#!/usr/bin/env python
"""Build tools/index.db, a SQLite FTS5 full-text index over three sources:

1. Prince.org forum posts, extracted from the raw HTML archived by the
   prince-org-archiver project (every forum and every date by default;
   --forum and --since restrict the run). Posts are deduplicated on their
   message ID: a few threads were archived with thousands of phantom pages
   repeating the same posts.
2. Discord message dumps stored as zips under discord/ (JSON exports only;
   every snapshot is read and messages are deduplicated on their ID, newest
   snapshot first so edited messages keep their latest text).
3. Reddit posts and comments downloaded by fetch_reddit.py into reddit/
   (JSON Lines from the Arctic Shift archive, deduplicated on their ID).

Run with the archiver's interpreter, which has BeautifulSoup and lxml:

    ~/Github/prince-org-archiver/.venv/bin/python tools/build_index.py

Options let you rebuild only one source or limit the run for testing.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARCHIVER = Path.home() / "Github" / "prince-org-archiver"
ARCHIVE_DB = ARCHIVER / "data" / "archive.db"
DISCORD_DIR = REPO / "discord"
REDDIT_DIR = REPO / "reddit"
INDEX_DB = REPO / "tools" / "index.db"

CONFIG_FILE = REPO / "tools" / "config.local.json"


def load_config() -> dict:
    """Local, git-ignored settings (see tools/config.example.json):

    - guilds: Discord server name (as in the zip file name) -> guild ID. Needed
      to build message links, since the JSON dumps only carry channel and
      message IDs.
    - private_servers: names of private conversations. They are indexed with
      private = 1 and must never be quoted or linked in public documents.
    - sanity_discord_ids / sanity_princeorg_msg_ids: optional message IDs
      (with a label) checked after the build.
    """
    if not CONFIG_FILE.exists():
        if __name__ == "__main__":
            print(f"[config] {CONFIG_FILE} not found: no Discord links, no private servers")
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


CONFIG = load_config()
GUILDS: dict[str, str] = CONFIG.get("guilds", {})
PRIVATE_SERVERS = set(CONFIG.get("private_servers", []))

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS princeorg USING fts5(
    thread_id UNINDEXED, page UNINDEXED, msg_id UNINDEXED, reply_no UNINDEXED,
    author, posted UNINDEXED, thread_title, subject, body, quoted, url UNINDEXED,
    forum_id UNINDEXED, forum UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS discord USING fts5(
    id UNINDEXED, server, guild_id UNINDEXED, channel_id UNINDEXED, channel,
    author, display_name, timestamp UNINDEXED, content, attachments,
    url UNINDEXED, private UNINDEXED, snapshot UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS reddit USING fts5(
    id UNINDEXED, kind UNINDEXED, subreddit, author, created UNINDEXED,
    title, body, link_id UNINDEXED, parent_id UNINDEXED, url UNINDEXED,
    score UNINDEXED, retrieved UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# ---------------------------------------------------------------------------
# Prince.org
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2}) (\d{1,2}):(\d{2})([ap]m)")
REPLY_RE = re.compile(r"Reply #(\d+)")


def _parse_date(text: str) -> str:
    m = DATE_RE.search(text)
    if not m:
        return ""
    mo, d, y, h, mi, ampm = m.groups()
    h = int(h) % 12 + (12 if ampm == "pm" else 0)
    century = "19" if int(y) >= 90 else "20"          # the archive starts in 1999
    return f"{century}{y}-{int(mo):02d}-{int(d):02d} {h:02d}:{mi}"


def parse_page(task: tuple[str, int, int, str, int | None]) -> list[tuple]:
    """Extract every post of one archived page. Runs in a worker process."""
    from bs4 import BeautifulSoup  # imported here so workers load it lazily

    path, thread_id, page, title, forum_id = task
    try:
        html = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    soup = BeautifulSoup(html, "lxml")
    forum = ""
    for link in soup.select("a[href^='/msg/']"):       # breadcrumb link to the forum, e.g. /msg/8
        m = re.fullmatch(r"/msg/(\d+)", link.get("href", ""))
        if m:
            forum = link.get_text(strip=True)
            forum_id = forum_id or int(m.group(1))
            break
    forum_id = forum_id or 7
    url = f"https://prince.org/msg/{forum_id}/{thread_id}" + (f"?&pg={page}" if page > 1 else "")
    rows = []
    for table in soup.select("table.msgpost"):
        body_td = table.select_one('td[class^="msgbody"]')
        if body_td is None:
            continue
        anchor = table.select_one('a[name^="msg_"]')
        msg_id = anchor["name"][4:] if anchor else ""
        auth = table.select_one(".msgauth a[href^='/profile/']")
        author = auth.get_text(strip=True) if auth else ""
        header = table.find_previous_sibling("span", class_="mpanchor")
        header_text = header.get_text(" ", strip=True) if header else ""
        posted = _parse_date(header_text)
        m = REPLY_RE.search(header_text)
        reply_no = int(m.group(1)) if m else 0
        subj = body_td.select_one(".msgsubj")
        subject = subj.get_text(" ", strip=True) if subj else ""
        if subj:
            subj.decompose()
        quotes = []
        for bq in body_td.select("div.bq"):
            quotes.append(bq.get_text(" ", strip=True))
            bq.decompose()
        for junk in body_td.select(".preq, .msgsig, script, style"):
            junk.decompose()
        body = re.sub(r"\s+", " ", body_td.get_text(" ", strip=True))
        rows.append((thread_id, page, msg_id, reply_no, author, posted, title,
                     subject, body, " | ".join(quotes), url, forum_id, forum))
    return rows


def index_princeorg(con: sqlite3.Connection, forum: int | None, since: str | None,
                    limit: int | None, workers: int) -> None:
    src = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    q = """
        SELECT t.thread_id, p.page_num, t.title, p.html_path, t.forum_id
        FROM threads t JOIN thread_pages p ON p.thread_id = t.thread_id
        WHERE p.html_path IS NOT NULL
    """
    params: list = []
    if forum is not None:
        q += " AND t.forum_id = ?"; params.append(forum)
    if since:
        q += " AND t.last_post_date >= ?"; params.append(since)
    q += " ORDER BY t.thread_id, p.page_num"
    tasks = []
    for thread_id, page, title, html_path, forum_id in src.execute(q, params):
        p = Path(html_path)
        if not p.is_absolute():
            p = ARCHIVER / p
        tasks.append((str(p), thread_id, page, title or "", forum_id))
    src.close()
    if limit:
        tasks = tasks[:limit]
    print(f"[princeorg] {len(tasks)} pages to parse with {workers} workers")
    con.execute("DROP TABLE IF EXISTS princeorg")       # the column set may have changed
    con.executescript(SCHEMA)
    t0 = time.time()
    n_posts = n_dup = 0
    seen: set = set()
    with Pool(workers) as pool:
        # imap keeps the (thread, page) order, so the first page holding a message wins
        for i, rows in enumerate(pool.imap(parse_page, tasks, chunksize=32), 1):
            keep = []
            for r in rows:
                mid = r[2]
                if mid:
                    key = int(mid) if mid.isdigit() else mid
                    if key in seen:
                        n_dup += 1
                        continue
                    seen.add(key)
                keep.append(r)
            if keep:
                con.executemany("INSERT INTO princeorg VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", keep)
                n_posts += len(keep)
            if i % 5000 == 0:
                con.commit()
                el = time.time() - t0
                print(f"[princeorg] {i}/{len(tasks)} pages, {n_posts} posts, {n_dup} duplicates skipped, "
                      f"{el:.0f}s (about {el / i * (len(tasks) - i) / 60:.0f} min left)", flush=True)
    con.commit()
    print(f"[princeorg] done: {n_posts} posts from {len(tasks)} pages in {time.time()-t0:.0f}s "
          f"({n_dup} duplicate copies skipped)", flush=True)
    build_princeorg_lookup(con)


def build_princeorg_lookup(con: sqlite3.Connection) -> None:
    """Plain table mapping FTS rowids to thread and message IDs, with B-tree indexes.

    FTS5 cannot index its UNINDEXED columns: without this table, fetching one
    thread or one message scans all 9 million posts (about 30 s).
    """
    t0 = time.time()
    con.executescript("""
        DROP TABLE IF EXISTS princeorg_lookup;
        CREATE TABLE princeorg_lookup (rowid INTEGER PRIMARY KEY, thread_id INTEGER, msg_id TEXT);
        INSERT INTO princeorg_lookup SELECT rowid, thread_id, msg_id FROM princeorg;
        CREATE INDEX princeorg_lookup_thread ON princeorg_lookup (thread_id);
        CREATE INDEX princeorg_lookup_msg ON princeorg_lookup (msg_id);
    """)
    con.commit()
    print(f"[princeorg] lookup table built in {time.time()-t0:.0f}s", flush=True)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

PAGE_RE = re.compile(r"^(?P<dir>[^/]+)/(?P<chan>.+)_page_(?P<n>\d+)\.json$")


def _snapshot_date(zip_path: Path) -> str:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", zip_path.parent.name)
    return m.group(1) if m else "0000-00-00"


def _server_name(zip_path: Path) -> str:
    return re.sub(r"\s*\((JSON|HTML)\)$", "", zip_path.stem).strip()


def _message_text(msg: dict) -> tuple[str, str]:
    content = msg.get("content") or ""
    extras = []
    for att in msg.get("attachments") or []:
        name = att.get("filename") or att.get("url") or ""
        if name:
            extras.append(name)
    for emb in msg.get("embeds") or []:
        for key in ("title", "description"):
            if emb.get(key):
                extras.append(str(emb[key]))
        if emb.get("url"):
            extras.append(emb["url"])
    return content, " | ".join(extras)


def index_discord(con: sqlite3.Connection, limit_zips: int | None) -> None:
    zips = [z for z in DISCORD_DIR.rglob("*.zip") if "(HTML)" not in z.name]
    zips.sort(key=lambda z: (_snapshot_date(z), z.name), reverse=True)
    if limit_zips:
        zips = zips[:limit_zips]
    con.execute("DELETE FROM discord")
    seen: set[str] = set()
    per_server: dict[str, int] = {}
    t0 = time.time()
    for z in zips:
        server = _server_name(z)
        snapshot = _snapshot_date(z)
        guild = GUILDS.get(server, "")
        private = 1 if server in PRIVATE_SERVERS else 0
        try:
            zf = zipfile.ZipFile(z)
        except zipfile.BadZipFile as e:
            print(f"[discord] skipping {z}: {e}")
            continue
        n_new = 0
        rows = []
        for name in zf.namelist():
            m = PAGE_RE.match(name)
            if not m:
                continue
            channel = m.group("chan")
            try:
                raw = zf.read(name)
            except zipfile.BadZipFile:
                # Some large exports have members Python's zipfile rejects
                # ("Bad magic number"); the unzip CLI reads them fine.
                r = subprocess.run(["unzip", "-p", str(z), name], capture_output=True)
                if not r.stdout:
                    print(f"[discord] unreadable member {z.name}:{name}")
                    continue
                raw = r.stdout
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError as e:
                print(f"[discord] bad JSON {z.name}:{name}: {e}")
                continue
            if isinstance(data, dict):
                data = data.get("messages", [])
            for msg in data:
                mid = str(msg.get("id", ""))
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                author = msg.get("author") or {}
                channel_id = str(msg.get("channel_id", ""))
                content, attachments = _message_text(msg)
                url = (f"https://discord.com/channels/{guild}/{channel_id}/{mid}"
                       if guild and not private else "")
                rows.append((mid, server, guild, channel_id, channel,
                             author.get("username", ""), author.get("global_name") or "",
                             (msg.get("timestamp") or "")[:19].replace("T", " "),
                             content, attachments, url, private, snapshot))
                n_new += 1
        if rows:
            con.executemany("INSERT INTO discord VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
        per_server[server] = per_server.get(server, 0) + n_new
        print(f"[discord] {snapshot} {server}: {n_new} new messages ({time.time()-t0:.0f}s)")
    print(f"[discord] done: {len(seen)} unique messages")
    for server, n in sorted(per_server.items(), key=lambda kv: -kv[1]):
        print(f"[discord]   {server}: {n}")


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

def _utc(ts) -> str:
    try:
        return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _text(value) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def index_reddit(con: sqlite3.Connection) -> None:
    """Posts first (their titles label the comments), then comments. A user
    file and a subreddit file can hold the same item: first one wins."""
    post_files = sorted(REDDIT_DIR.glob("*_posts.jsonl"))
    comment_files = sorted(REDDIT_DIR.glob("*_comments.jsonl"))
    if not post_files and not comment_files:
        print(f"[reddit] nothing under {REDDIT_DIR}, run fetch_reddit.py first")
        return
    con.execute("DELETE FROM reddit")
    titles: dict[str, str] = {}
    seen: set[str] = set()
    t0 = time.time()
    n_posts = n_comments = 0
    for path in post_files:
        rows = []
        for d in _read_jsonl(path):
            pid = d.get("id", "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            sub = d.get("subreddit") or ""
            titles[pid] = _text(d.get("title"))
            rows.append((pid, "post", sub, d.get("author") or "", _utc(d.get("created_utc")),
                         _text(d.get("title")), _text(d.get("selftext")), pid, "",
                         f"https://www.reddit.com/r/{sub}/comments/{pid}/",
                         d.get("score"), _utc(d.get("retrieved_on"))))
        con.executemany("INSERT INTO reddit VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        n_posts += len(rows)
        print(f"[reddit] {path.name}: {len(rows)} posts")
    for path in comment_files:
        rows = []
        for d in _read_jsonl(path):
            cid = d.get("id", "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            sub = d.get("subreddit") or ""
            link = (d.get("link_id") or "")[3:]
            rows.append((cid, "comment", sub, d.get("author") or "", _utc(d.get("created_utc")),
                         titles.get(link, ""), _text(d.get("body")), link,
                         (d.get("parent_id") or "")[3:],
                         f"https://www.reddit.com/r/{sub}/comments/{link}/_/{cid}/",
                         d.get("score"), _utc(d.get("retrieved_on"))))
        con.executemany("INSERT INTO reddit VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        n_comments += len(rows)
        print(f"[reddit] {path.name}: {len(rows)} comments")
    con.commit()
    print(f"[reddit] done: {n_posts} posts, {n_comments} comments in {time.time()-t0:.0f}s")


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------

def sanity(con: sqlite3.Connection) -> None:
    print("[sanity] princeorg posts:", con.execute("SELECT COUNT(*) FROM princeorg").fetchone()[0],
          "distinct msg_id:", con.execute("SELECT COUNT(DISTINCT msg_id) FROM princeorg").fetchone()[0])
    for fid, name, n in con.execute("SELECT forum_id, forum, COUNT(*) FROM princeorg GROUP BY forum_id "
                                    "ORDER BY 3 DESC LIMIT 12"):
        print(f"[sanity]   forum {fid} {name}: {n} posts")
    print("[sanity] discord messages:", con.execute("SELECT COUNT(*) FROM discord").fetchone()[0])
    print("[sanity] reddit items:", con.execute("SELECT COUNT(*) FROM reddit").fetchone()[0])
    for mid, who in CONFIG.get("sanity_discord_ids", {}).items():
        row = con.execute("SELECT server, channel, author, timestamp FROM discord WHERE id = ?", (mid,)).fetchone()
        print(f"[sanity] {who}: {'FOUND ' + str(row) if row else 'missing'}")
    for mid, who in CONFIG.get("sanity_princeorg_msg_ids", {}).items():
        row = con.execute("SELECT author, posted, url FROM princeorg WHERE rowid IN "
                          "(SELECT rowid FROM princeorg_lookup WHERE msg_id = ?)", (mid,)).fetchone()
        print(f"[sanity] {who}: {row or 'missing'}")
    row = con.execute("SELECT MIN(posted), MAX(posted) FROM princeorg WHERE posted != ''").fetchone()
    print(f"[sanity] princeorg date span: {row}")
    row = con.execute("SELECT MIN(timestamp), MAX(timestamp) FROM discord WHERE private = 0").fetchone()
    print(f"[sanity] discord date span: {row}")
    row = con.execute("SELECT MIN(created), MAX(created) FROM reddit WHERE created != ''").fetchone()
    print(f"[sanity] reddit date span: {row}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=INDEX_DB)
    ap.add_argument("--forum", type=int, help="only this Prince.org forum (default: all), e.g. 7")
    ap.add_argument("--since", help="only threads with a last post on/after this date (default: all)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit-pages", type=int, help="testing: only parse N Prince.org pages")
    ap.add_argument("--limit-zips", type=int, help="testing: only read N Discord zips")
    ap.add_argument("--skip-princeorg", action="store_true")
    ap.add_argument("--lookups-only", action="store_true",
                    help="only (re)build the Prince.org lookup table of an existing index, then exit")
    ap.add_argument("--skip-discord", action="store_true")
    ap.add_argument("--skip-reddit", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    if args.lookups_only:
        build_princeorg_lookup(con)
        return
    con.executescript(SCHEMA)
    if not args.skip_princeorg:
        index_princeorg(con, args.forum, args.since, args.limit_pages, args.workers)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('princeorg_built', ?)", (datetime.now().isoformat(timespec='seconds'),))
    if not args.skip_discord:
        index_discord(con, args.limit_zips)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('discord_built', ?)", (datetime.now().isoformat(timespec='seconds'),))
    if not args.skip_reddit:
        index_reddit(con)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('reddit_built', ?)", (datetime.now().isoformat(timespec='seconds'),))
    con.commit()
    sanity(con)
    con.close()


if __name__ == "__main__":
    main()
