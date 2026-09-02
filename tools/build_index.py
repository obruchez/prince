#!/usr/bin/env python
"""Build tools/index.db, a SQLite FTS5 full-text index over two sources:

1. Prince.org forum posts, extracted from the raw HTML archived by the
   prince-org-archiver project (forum 7 "Prince: Music and More" by default,
   threads active since 2016-04-21).
2. Discord message dumps stored as zips under discord/ (JSON exports only;
   every snapshot is read and messages are deduplicated on their ID, newest
   snapshot first so edited messages keep their latest text).

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
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS discord USING fts5(
    id UNINDEXED, server, guild_id UNINDEXED, channel_id UNINDEXED, channel,
    author, display_name, timestamp UNINDEXED, content, attachments,
    url UNINDEXED, private UNINDEXED, snapshot UNINDEXED,
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
    return f"20{y}-{int(mo):02d}-{int(d):02d} {h:02d}:{mi}"


def parse_page(task: tuple[str, int, int, str]) -> list[tuple]:
    """Extract every post of one archived page. Runs in a worker process."""
    from bs4 import BeautifulSoup  # imported here so workers load it lazily

    path, thread_id, page, title = task
    try:
        html = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    soup = BeautifulSoup(html, "lxml")
    url = f"https://prince.org/msg/7/{thread_id}" + (f"?&pg={page}" if page > 1 else "")
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
                     subject, body, " | ".join(quotes), url))
    return rows


def index_princeorg(con: sqlite3.Connection, forum: int, since: str,
                    limit: int | None, workers: int) -> None:
    src = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    q = """
        SELECT t.thread_id, p.page_num, t.title, p.html_path
        FROM threads t JOIN thread_pages p ON p.thread_id = t.thread_id
        WHERE t.status = 'complete' AND t.forum_id = ? AND t.last_post_date >= ?
          AND p.html_path IS NOT NULL
        ORDER BY t.thread_id, p.page_num
    """
    tasks = []
    for thread_id, page, title, html_path in src.execute(q, (forum, since)):
        p = Path(html_path)
        if not p.is_absolute():
            p = ARCHIVER / p
        tasks.append((str(p), thread_id, page, title or ""))
    src.close()
    if limit:
        tasks = tasks[:limit]
    print(f"[princeorg] {len(tasks)} pages to parse with {workers} workers")
    con.execute("DELETE FROM princeorg")
    t0 = time.time()
    n_posts = 0
    with Pool(workers) as pool:
        for i, rows in enumerate(pool.imap_unordered(parse_page, tasks, chunksize=16), 1):
            if rows:
                con.executemany("INSERT INTO princeorg VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
                n_posts += len(rows)
            if i % 2000 == 0:
                con.commit()
                print(f"[princeorg] {i}/{len(tasks)} pages, {n_posts} posts, {time.time()-t0:.0f}s")
    con.commit()
    print(f"[princeorg] done: {n_posts} posts from {len(tasks)} pages in {time.time()-t0:.0f}s")


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
# Sanity checks
# ---------------------------------------------------------------------------

def sanity(con: sqlite3.Connection) -> None:
    print("[sanity] princeorg posts:", con.execute("SELECT COUNT(*) FROM princeorg").fetchone()[0])
    print("[sanity] discord messages:", con.execute("SELECT COUNT(*) FROM discord").fetchone()[0])
    for mid, who in CONFIG.get("sanity_discord_ids", {}).items():
        row = con.execute("SELECT server, channel, author, timestamp FROM discord WHERE id = ?", (mid,)).fetchone()
        print(f"[sanity] {who}: {'FOUND ' + str(row) if row else 'missing'}")
    for mid, who in CONFIG.get("sanity_princeorg_msg_ids", {}).items():
        row = con.execute("SELECT author, posted, url FROM princeorg WHERE msg_id = ?", (mid,)).fetchone()
        print(f"[sanity] {who}: {row or 'missing'}")
    row = con.execute("SELECT MIN(posted), MAX(posted) FROM princeorg WHERE posted != ''").fetchone()
    print(f"[sanity] princeorg date span: {row}")
    row = con.execute("SELECT MIN(timestamp), MAX(timestamp) FROM discord WHERE private = 0").fetchone()
    print(f"[sanity] discord date span: {row}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=INDEX_DB)
    ap.add_argument("--forum", type=int, default=7)
    ap.add_argument("--since", default="2016-04-21", help="threads with last post on/after this date")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit-pages", type=int, help="testing: only parse N Prince.org pages")
    ap.add_argument("--limit-zips", type=int, help="testing: only read N Discord zips")
    ap.add_argument("--skip-princeorg", action="store_true")
    ap.add_argument("--skip-discord", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    if not args.skip_princeorg:
        index_princeorg(con, args.forum, args.since, args.limit_pages, args.workers)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('princeorg_built', ?)", (datetime.now().isoformat(timespec='seconds'),))
    if not args.skip_discord:
        index_discord(con, args.limit_zips)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('discord_built', ?)", (datetime.now().isoformat(timespec='seconds'),))
    con.commit()
    sanity(con)
    con.close()


if __name__ == "__main__":
    main()
