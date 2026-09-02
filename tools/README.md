# Tools

Scripts used to mine fan reports for `official-releases-sound-quality-issues.md`.

## Sources

* Prince.org: the archive built by the `prince-org-archiver` project (`~/Github/prince-org-archiver/data`). Post text only exists in the raw HTML; `archive.db` has thread metadata.
* Discord: JSON exports of Prince servers, one zip per server per snapshot date, under `discord/` (git-ignored, several GB). Snapshots overlap; messages are deduplicated on their ID.

## Usage

Both scripts need BeautifulSoup and lxml; the archiver's virtualenv has them:

```bash
PY=~/Github/prince-org-archiver/.venv/bin/python
$PY tools/build_index.py            # builds tools/index.db (git-ignored), 10-30 min
$PY tools/search.py 'velvet kitty cat lossy'
$PY tools/search.py '"drop out" OR dropout OR glitch' --source princeorg --since 2017-06-01
$PY tools/search.py --queries queries.txt --limit 30
```

`build_index.py --help` lists options (forum, date cut-off, test limits).

## Local configuration

Copy `tools/config.example.json` to `tools/config.local.json` (git-ignored) and fill in:

* `guilds`: Discord server name (as in the zip file name) to guild ID, needed to build message links (the JSON dumps only carry channel and message IDs).
* `private_servers`: names of private conversations (e.g. `Direct Messages`, DM dumps). They are indexed with `private = 1` and hidden from `search.py` unless `--private` is given: never quote or link them publicly.
* `sanity_discord_ids`, `sanity_princeorg_msg_ids`: optional message IDs (with a label) checked after the build.
