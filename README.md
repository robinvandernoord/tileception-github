# tileception-github

GitHub pull request and issue search for Tileception. Type `gh 105` to find
#105 in every repository you follow, or `gh` followed by a space for sync status
and open work that involves you. Enter opens the item on GitHub; Ctrl+Enter copies
its URL.

Requires Linux, Python 3.14+, systemd user services and the
[GitHub CLI](https://cli.github.com). The plugin reuses your `gh` login and only
reads from GitHub.

## Install

In an activated Python 3.14+ virtual environment:

```sh
uv pip install .
tileception-github install
```

Installation runs `gh auth login` when needed, installs the manifest and user
units, runs the first sync and offers the timer. Then run
`tileception msg plugins reload`. For unattended setup, log in with `gh` first
and run `tileception-github install --enable-timer`.

## Choosing repositories

Without configuration, sync covers every repository you own, collaborate on or
reach through an organization that is not archived or a fork and was pushed in
the last `active_days`. A whitelist replaces that automatic selection; the
blacklist always wins.

```toml
repos = []                          # whitelist; empty: automatic
exclude_repos = ["org/noisy-repo"]  # blacklist
active_days = 365                   # automatic selection only; 0: any age
debounce_ms = 250
live_lookup = true                  # also look typed numbers up on GitHub
```

`tileception-github repos` edits both lists. It shows every repository you can
reach, most recently pushed first, with `active` marking the automatic ones.
Space or Tab cycles `[ ]` automatic, `[x]` only these and `[-]` never. Type to
filter, Ctrl-A marks every shown repository `[x]`, Ctrl-X resets all, Enter
saves and Esc cancels. It offers to sync right away. The configuration lives in
`~/.config/tileception-github/config.toml`.

## Searching

| Query                                | Finds                                             |
| ------------------------------------ | ------------------------------------------------- |
| `gh 105`, `gh #105`                  | #105 in every synchronized repository             |
| `gh tile 105`, `gh tile#105`         | #105 in repositories whose name matches `tile`    |
| `gh owner/repo#105`, a GitHub URL    | exactly that item, even outside your selection    |
| `gh owner/repo`, `gh tile`           | the repository home page, then recent activity    |
| `gh login bug`                       | text in titles, branches, labels, authors, bodies |

Tab on a result narrows the search to its repository. Number queries answer
from the cache at once, then add live GitHub results the cache does not have
yet, from up to 40 of the most recently pushed repositories. Matches rank open
items first, then items involving you, recent updates and repositories you open
often. That last signal comes from `gh` results you open
through the launcher, recorded only locally.

## Sync

```sh
tileception-github sync
tileception-github status
```

The timer runs every 10 minutes and fetches only items updated since the last
sync. Each repository is checked daily for deleted or transferred items.
Repositories that leave the selection are removed from the cache at the next
sync.

```sh
systemctl --user enable --now tileception-github-sync.timer
```

The service asks `gh auth token` for a token on every run and never stores it,
so the `gh` keyring must be reachable from your user session.

| Purpose          | Default path                                                       |
| ---------------- | ------------------------------------------------------------------ |
| Configuration    | `~/.config/tileception-github/config.toml`                         |
| Cache and visits | `~/.cache/tileception-github/`                                     |
| Manifest         | `~/.local/share/tileception/plugins/github.toml`                   |
| Systemd units    | `~/.config/systemd/user/tileception-github-sync.{service,timer}`   |

## Uninstall

```sh
systemctl --user disable --now tileception-github-sync.timer
rm ~/.local/share/tileception/plugins/github.toml
rm ~/.config/systemd/user/tileception-github-sync.{service,timer}
systemctl --user daemon-reload
tileception msg plugins reload
uv pip uninstall tileception-github
```

## Development

```sh
uv venv --python 3.14 venv
uv pip install --python venv/bin/python -e '.[dev]'
venv/bin/ruff check src
venv/bin/ruff format --check src
```
