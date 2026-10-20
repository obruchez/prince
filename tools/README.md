# Tools

Scripts used to mine fan reports for `official-releases-issues.md`.

## Sources

* Prince.org: the archive built by the `prince-org-archiver` project (`~/Github/prince-org-archiver/data`). Post text only exists in the raw HTML; `archive.db` has thread metadata. The index covers every forum since 1999. A few threads were archived with thousands of phantom pages repeating the same posts, so posts are deduplicated on their message ID: count with `COUNT(DISTINCT msg_id)`.
* Discord: JSON exports of Prince servers, one zip per server per snapshot date, under `discord/` (git-ignored, several GB). Snapshots overlap; messages are deduplicated on their ID.
* Reddit: posts and comments of chosen subreddits and users, downloaded by `fetch_reddit.py` from the [Arctic Shift](https://arctic-shift.photon-reddit.com) archive (free API, no key) into `reddit/` (git-ignored, JSON Lines). Runs are incremental: each file resumes from its newest item. Arctic Shift keeps content later deleted from Reddit, so a `[deleted]` body means it was already gone when archived.

## Usage

Both scripts need BeautifulSoup and lxml; the archiver's virtualenv has them:

```bash
PY=~/Github/prince-org-archiver/.venv/bin/python
$PY tools/build_index.py            # builds tools/index.db (git-ignored), about 15 min for Prince.org
$PY tools/search.py 'velvet kitty cat lossy'
$PY tools/search.py '"drop out" OR dropout OR glitch' --source princeorg --since 2017-06-01
$PY tools/search.py 'gett off sharp' --source princeorg --forum 7    # one forum, by ID or part of its name
$PY tools/search.py --queries queries.txt --limit 30
$PY tools/fetch_reddit.py                              # subreddits/users from config.local.json
$PY tools/fetch_reddit.py --subreddit PRINCE --user someone
$PY tools/build_index.py --skip-princeorg --skip-discord   # re-index Reddit only
$PY tools/search.py 'timeless "i wonder"' --source reddit
$PY tools/show.py post 1gp4677                         # a post with its comment tree
```

`build_index.py --help` lists options (restrict to one forum or a date cut-off, test limits). A plain `princeorg_lookup` table indexes thread and message IDs so that `show.py thread` stays instant on 9 million posts; it is rebuilt with the Prince.org index, or alone with `--lookups-only`.

## Local configuration

Copy `tools/config.example.json` to `tools/config.local.json` (git-ignored) and fill in:

* `guilds`: Discord server name (as in the zip file name) to guild ID, needed to build message links (the JSON dumps only carry channel and message IDs).
* `private_servers`: names of private conversations (e.g. `Direct Messages`, DM dumps). They are indexed with `private = 1` and hidden from `search.py` unless `--private` is given: never quote or link them publicly.
* `sanity_discord_ids`, `sanity_princeorg_msg_ids`: optional message IDs (with a label) checked after the build.
* `reddit_subreddits`, `reddit_users`: what `fetch_reddit.py` downloads when run without arguments.
