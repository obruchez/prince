#!/usr/bin/env python
"""Download Reddit posts and comments from the Arctic Shift archive into reddit/.

    python tools/fetch_reddit.py                         # everything listed in config.local.json
    python tools/fetch_reddit.py --subreddit PRINCE      # one subreddit
    python tools/fetch_reddit.py --user someone          # one user, across all subreddits
    python tools/fetch_reddit.py --subreddit PRINCE --since 2024-01-01

Arctic Shift (https://arctic-shift.photon-reddit.com) is a continuously updated
archive of Reddit; its API is free, needs no key, and returns items in
created_utc order, so a run resumes from the newest item already on disk.

Output: one JSON Lines file per scope and kind under reddit/ (git-ignored):
reddit/r_PRINCE_posts.jsonl, reddit/r_PRINCE_comments.jsonl, reddit/u_NAME_*.jsonl.
Items are deduplicated on their ID; rebuild the search index afterwards with
build_index.py --skip-princeorg --skip-discord.

Personal research use only: the data is not redistributed with the repository.
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "reddit"
CONFIG_FILE = REPO / "tools" / "config.local.json"
API = "https://arctic-shift.photon-reddit.com/api"
USER_AGENT = "prince-official-releases-research/0.1"

# Only the fields the API lets us select; links are rebuilt from id/link_id.
FIELDS = {
    "posts": "id,created_utc,retrieved_on,author,subreddit,title,selftext,url,score,num_comments,link_flair_text",
    "comments": "id,created_utc,retrieved_on,author,subreddit,body,link_id,parent_id,score",
}
PAUSE = 1.0          # seconds between requests: the service is run by one volunteer
RETRY_WAITS = (5, 10, 20, 40, 80)


def get(url: str) -> dict:
    """GET with retries on 422 (seen transiently), 429 and 5xx; other errors abort."""
    for attempt, wait in enumerate(RETRY_WAITS + (None,)):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (422, 429, 500, 502, 503, 504) and wait:
                detail = e.read(300).decode("utf-8", "replace").strip()
                print(f"  HTTP {e.code} {detail}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if wait:
                print(f"  {e}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def existing(path: Path) -> tuple[set[str], int]:
    """IDs already on disk and the newest created_utc, to resume from."""
    ids: set[str] = set()
    newest = 0
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ids.add(d["id"])
                newest = max(newest, int(d.get("created_utc") or 0))
    return ids, newest


def fetch(kind: str, scope_param: str, scope: str, since: int | None) -> None:
    path = OUT_DIR / f"{'r' if scope_param == 'subreddit' else 'u'}_{scope}_{kind}.jsonl"
    ids, newest = existing(path)
    # `after` is exclusive on the second, so back up one second and rely on the
    # ID set to skip what is already stored.
    after = max(since or 0, newest - 1 if newest else 0)
    print(f"[{kind}] {scope_param}={scope}: {len(ids)} on disk, resuming after "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(after)) if after else 'the beginning'}")
    n_new = 0
    t0 = time.time()
    with path.open("a", encoding="utf-8") as out:
        while True:
            params = {scope_param: scope, "sort": "asc", "limit": "auto", "fields": FIELDS[kind]}
            if after:
                params["after"] = str(after)
            data = get(f"{API}/{kind}/search?" + urllib.parse.urlencode(params)).get("data", [])
            if not data:
                break
            for item in data:
                if item["id"] in ids:
                    continue
                ids.add(item["id"])
                out.write(json.dumps(item, ensure_ascii=False) + "\n")
                n_new += 1
            out.flush()
            last = int(data[-1]["created_utc"])
            if last <= after:      # a whole page inside one second: step past it
                last = after + 1
            after = last
            print(f"  {n_new} new, at {time.strftime('%Y-%m-%d', time.gmtime(last))} "
                  f"({time.time()-t0:.0f}s)", end="\r", flush=True)
            time.sleep(PAUSE)
    print(f"\n[{kind}] {scope_param}={scope}: {n_new} new, {len(ids)} total -> {path.relative_to(REPO)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subreddit", action="append", default=[], help="subreddit name, repeatable")
    ap.add_argument("--user", action="append", default=[], help="user name, repeatable")
    ap.add_argument("--since", help="YYYY-MM-DD (UTC): ignore anything older. Only honoured when the\n"
                    "file does not exist yet: later runs resume from the newest item on disk")
    ap.add_argument("--kind", choices=["posts", "comments", "both"], default="both")
    args = ap.parse_args()

    subs, users = list(args.subreddit), list(args.user)
    if not subs and not users:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        subs = cfg.get("reddit_subreddits", [])
        users = cfg.get("reddit_users", [])
        if not subs and not users:
            ap.error("give --subreddit/--user or list reddit_subreddits/reddit_users in config.local.json")
    since = calendar.timegm(time.strptime(args.since, "%Y-%m-%d")) if args.since else None
    kinds = ["posts", "comments"] if args.kind == "both" else [args.kind]

    OUT_DIR.mkdir(exist_ok=True)
    for sub in subs:
        for kind in kinds:
            fetch(kind, "subreddit", sub, since)
    for user in users:
        for kind in kinds:
            fetch(kind, "author", user, since)


if __name__ == "__main__":
    main()
