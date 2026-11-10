"""Per-user paths and configuration."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")

CONFIG_EXAMPLE = """# Repositories as owner/name. Edit both lists with: tileception-github repos
# Empty repos: every repository you can reach that is not archived or a fork and
# was pushed within active_days (0: any age).
repos = []
exclude_repos = []  # always wins
active_days = 365
debounce_ms = 250
# Look up typed numbers on GitHub as well as in the cache.
live_lookup = true
"""


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError(f"Not an owned private directory: {path}")
    path.chmod(0o700)
    return path


@dataclass(frozen=True)
class Paths:
    config: Path
    cache: Path

    @classmethod
    def discover(cls) -> Paths:
        home = Path.home()
        return cls(
            Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "tileception-github",
            Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "tileception-github",
        )

    @property
    def config_file(self) -> Path:
        return self.config / "config.toml"

    @property
    def database(self) -> Path:
        return self.cache / "items.sqlite3"

    @property
    def visits(self) -> Path:
        return self.cache / "visits.sqlite3"


@dataclass(frozen=True)
class Config:
    repos: tuple[str, ...] = ()
    exclude_repos: tuple[str, ...] = ()
    active_days: int = 365
    debounce_ms: int = 250
    live_lookup: bool = True

    @classmethod
    def load(cls, paths: Paths) -> Config:
        filename = paths.config_file
        values = tomllib.loads(filename.read_text()) if filename.exists() else {}
        return cls(
            repos=repo_list(values, "repos"),
            exclude_repos=repo_list(values, "exclude_repos"),
            active_days=max(0, int(values.get("active_days", 365))),
            debounce_ms=max(50, min(2000, int(values.get("debounce_ms", 250)))),
            live_lookup=bool(values.get("live_lookup", True)),
        )

    def selection(self, available: list[dict]) -> list[str]:
        """Repositories to synchronize: the whitelist, or active ones, minus the blacklist.

        available is GitHub's repository list, most recently pushed first; it is only
        consulted without a whitelist.
        """
        excluded = {repo.casefold() for repo in self.exclude_repos}
        if self.repos:
            chosen = list(self.repos)
        else:
            chosen = [repo["nameWithOwner"] for repo in available if is_active(repo, self.active_days)]
        return [repo for repo in chosen if repo.casefold() not in excluded]


def repo_list(values: dict, key: str) -> tuple[str, ...]:
    repos = values.get(key, [])
    if not isinstance(repos, list) or not all(
        isinstance(repo, str) and REPO_PATTERN.fullmatch(repo) for repo in repos
    ):
        raise ValueError(f"{key} must be a list of owner/name strings")
    # GitHub names are case-insensitive; keep the configured spelling but drop duplicates.
    unique = {repo.casefold(): repo for repo in repos}
    return tuple(sorted(unique.values(), key=str.casefold))


def is_active(repo: dict, active_days: int) -> bool:
    """Whether a repository joins the automatic selection."""
    if repo.get("isArchived") or repo.get("isFork"):
        return False
    if not active_days:
        return True
    pushed = repo.get("pushedAt")
    if not pushed:
        return False
    return datetime.fromisoformat(pushed.replace("Z", "+00:00")) >= datetime.now(UTC) - timedelta(
        days=active_days
    )


def assignment_lines(lines: list[str], key: str) -> range | None:
    """Locate a top-level array assignment, including multi-line arrays with comments.

    Repository names contain neither brackets nor #, so everything after # is a comment.
    """
    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    for start, line in enumerate(lines):
        if not pattern.match(line):
            continue
        depth = 0
        for end in range(start, len(lines)):
            code = lines[end].split("#", 1)[0]
            depth += code.count("[") - code.count("]")
            if depth <= 0:
                return range(start, end + 1)
        raise ValueError(f"Unterminated {key} array in config.toml")
    return None


def format_list(key: str, values: list[str], notes: dict[str, str]) -> list[str]:
    if not values:
        return [f"{key} = []"]
    lines = [f"{key} = ["]
    for value in values:
        note = " ".join(notes.get(value, "").split())
        lines.append(f'    "{value}",  # {note}' if note else f'    "{value}",')
    return [*lines, "]"]


def replace_assignment(text: str, key: str, replacement: list[str]) -> str:
    lines = text.splitlines()
    found = assignment_lines(lines, key)
    if found is None:
        # Top-level keys must precede the first [table] header to stay top-level.
        table = next((index for index, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
        lines[table:table] = replacement
    else:
        lines[found.start : found.stop] = replacement
    return "\n".join(lines) + "\n"


def write_private(filename: Path, text: str):
    tomllib.loads(text)  # Never replace a working configuration with an unparsable one.
    temporary = filename.with_suffix(".tmp")
    with temporary.open("w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
    temporary.replace(filename)
