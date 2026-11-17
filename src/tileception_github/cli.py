"""Command line and user installation, with no Tileception build dependency."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .config import (
    CONFIG_EXAMPLE,
    Config,
    Paths,
    format_list,
    is_active,
    private_directory,
    replace_assignment,
    write_private,
)

SERVICE = """[Unit]
Description=Update private GitHub search cache
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart={command} sync
Nice=15
IOSchedulingClass=idle
UMask=0077
NoNewPrivileges=true
TimeoutStartSec=1h
"""

TIMER = """[Unit]
Description=Refresh GitHub launcher search every 10 minutes

[Timer]
OnCalendar=*:0/10
RandomizedDelaySec=1m
Persistent=true
Unit=tileception-github-sync.service

[Install]
WantedBy=timers.target
"""

GITHUB_URL = re.compile(r"https://github\.com/([^/\s?#]+/[^/\s?#]+)(?:[/?#]\S*)?")


def unit_quote(value: str) -> str:
    # ExecStart is parsed by systemd, including specifier and environment expansion.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def units_directory(paths: Paths) -> Path:
    return paths.config.parent / "systemd/user"


def ensure_gh_login(non_interactive: bool = False):
    gh = shutil.which("gh")
    if not gh:
        raise ValueError("Install the GitHub CLI first: https://cli.github.com")
    status = subprocess.run([gh, "auth", "status", "--hostname", "github.com"], capture_output=True)
    if not status.returncode:
        print("Using your GitHub CLI login", flush=True)
        return
    if non_interactive or not sys.stdin.isatty():
        raise ValueError("gh is not logged in; run gh auth login in a terminal first")
    print("Log in to GitHub with the GitHub CLI; the plugin reuses that login.", flush=True)
    subprocess.run([gh, "auth", "login", "--hostname", "github.com"], check=True)


def initial_sync(paths: Paths):
    """Wait for the user service while forwarding its private progress."""
    from .progress import read_progress

    command = ["systemctl", "--user", "start", "tileception-github-sync.service"]
    previous = read_progress(paths)
    previous_update = previous.get("updated") if previous else None
    last_output = 0.0
    with subprocess.Popen(command) as process:
        while True:
            try:
                result = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                result = None
            progress = read_progress(paths)
            now = time.monotonic()
            if (
                progress
                and progress.get("updated") != previous_update
                and (result is not None or now - last_output >= 1)
            ):
                print(progress["message"], flush=True)
                previous_update = progress.get("updated")
                last_output = now
            if result is not None:
                if result:
                    raise subprocess.CalledProcessError(result, command)
                return


def run_sync(paths: Paths):
    """Prefer the limited user service; fall back to this process before install."""
    if (units_directory(paths) / "tileception-github-sync.service").exists():
        try:
            initial_sync(paths)
        except subprocess.CalledProcessError:
            raise ValueError(
                "Synchronization failed. Check tileception-github status or "
                "journalctl --user -u tileception-github-sync.service"
            ) from None
    else:
        from .sync import synchronize

        synchronize(paths, Config.load(paths))


def choose_repos(paths: Paths):
    """Edit the repository whitelist and blacklist in a picker."""
    from .github import GitHub
    from .picker import AUTO, EXCLUDE, INCLUDE, Entry, pick

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("The repository picker needs an interactive terminal; edit config.toml instead")
    private_directory(paths.config)
    settings = Config.load(paths)
    print("Fetching your repositories from GitHub...", flush=True)
    available = GitHub().repositories()
    configured = {repo.casefold(): INCLUDE for repo in settings.repos}
    configured |= {repo.casefold(): EXCLUDE for repo in settings.exclude_repos}
    entries = []
    for repo in available:
        notes = ["active"] if is_active(repo, settings.active_days) else []
        notes += [
            label
            for label, flag in (("private", "isPrivate"), ("fork", "isFork"), ("archived", "isArchived"))
            if repo[flag]
        ]
        if repo.get("pushedAt"):
            notes.append(f"pushed {repo['pushedAt'][:10]}")
        entries.append(Entry(repo["nameWithOwner"], repo["nameWithOwner"], ", ".join(notes)))
    known = {entry.key.casefold() for entry in entries}
    entries += [
        Entry(repo, repo, "not found or no access")
        for repo in (*settings.repos, *settings.exclude_repos)
        if repo.casefold() not in known
    ]
    # Configured repositories first, then by most recent push.
    entries.sort(key=lambda entry: entry.key.casefold() not in configured)
    before = {entry.key: configured.get(entry.key.casefold(), AUTO) for entry in entries}
    print(
        "Repositories marked active are synchronized automatically while nothing is marked [x].",
        flush=True,
    )
    chosen = pick("Repositories", entries, before)
    if chosen is None or chosen == before:
        print("Repository selection unchanged")
        return
    filename = paths.config_file
    text = filename.read_text() if filename.exists() else CONFIG_EXAMPLE
    for key, state in (("repos", INCLUDE), ("exclude_repos", EXCLUDE)):
        values = sorted((repo for repo, value in chosen.items() if value == state), key=str.casefold)
        text = replace_assignment(text, key, format_list(key, values, {}))
    write_private(filename, text)
    settings = Config.load(paths)
    if settings.repos:
        print(f"Saved: only {len(settings.repos)} repositories, {len(settings.exclude_repos)} excluded")
    else:
        count = len(settings.selection(available))
        print(f"Saved: {count} active repositories, {len(settings.exclude_repos)} excluded")
    if input("Synchronize now to apply the selection? [Y/n] ").strip().lower() in {"", "y", "yes"}:
        run_sync(paths)
    else:
        print("The next sync applies the new selection.")


def install(paths: Paths, enable_timer: bool, non_interactive: bool = False):
    non_interactive = non_interactive or not sys.stdin.isatty()
    command = shutil.which("tileception-github")
    if not command:
        raise ValueError("Install the package first, for example with uv pip install .")
    command = str(Path(command).absolute())
    ensure_gh_login(non_interactive=non_interactive)
    for directory in (paths.config, paths.cache):
        private_directory(directory)
    if not paths.config_file.exists():
        write_private(paths.config_file, CONFIG_EXAMPLE)
    Config.load(paths)  # Fail early on an invalid configuration.
    units = units_directory(paths)
    units.mkdir(parents=True, exist_ok=True)
    (units / "tileception-github-sync.service").write_text(SERVICE.format(command=unit_quote(command)))
    (units / "tileception-github-sync.timer").write_text(TIMER)
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    manifests = data_home / "tileception/plugins"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "github.toml").write_text(
        f'command = {json.dumps(command)}\nargs = ["serve"]\n[launcher]\nprefixes = ["gh"]\n'
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if non_interactive:
        subprocess.run(
            ["systemctl", "--user", "start", "--no-block", "tileception-github-sync.service"], check=True
        )
        print("Initial GitHub sync queued in the background. Use tileception-github status for progress.")
    else:
        print("Running the initial GitHub synchronization.", flush=True)
        run_sync(paths)
    if not enable_timer and not non_interactive:
        enable_timer = input("Enable the 10-minute GitHub sync timer now? [y/N] ").strip().lower() in {
            "y",
            "yes",
        }
    if enable_timer:
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "tileception-github-sync.timer"], check=True
        )
    print("GitHub plugin installed. Run tileception msg plugins reload, or restart Tileception.")
    print("Use tileception-github repos to always include or exclude repositories.")


def open_url(paths: Paths, url: str):
    match = GITHUB_URL.fullmatch(url)
    if not match:
        raise ValueError("Only https://github.com URLs can be opened")
    from .store import record_visit

    try:
        private_directory(paths.cache)
        record_visit(paths.visits, url, match[1])
    except OSError, ValueError, sqlite3.Error:
        pass  # Opening matters more than ranking.
    os.execvp("xdg-open", ["xdg-open", url])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="GitHub pull request and issue launcher search")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "sync", "status"):
        sub.add_parser(name)
    sub.add_parser("repos", help="always include or exclude repositories")
    installer = sub.add_parser("install")
    installer.add_argument("--enable-timer", action="store_true")
    installer.add_argument(
        "--non-interactive", action="store_true", help="skip prompts and queue the first sync"
    )
    opener = sub.add_parser("open", help="open a GitHub URL and remember it for ranking")
    opener.add_argument("url")
    args = parser.parse_args()
    paths = Paths.discover()
    try:
        if args.command == "serve":
            from .server import Server

            Server(paths).run()
        elif args.command == "sync":
            from .sync import synchronize

            os.nice(10)
            synchronize(paths, Config.load(paths))
        elif args.command == "repos":
            choose_repos(paths)
        elif args.command == "install":
            install(paths, enable_timer=args.enable_timer, non_interactive=args.non_interactive)
        elif args.command == "open":
            open_url(paths, args.url)
        elif args.command == "status":
            from .server import Server

            print(json.dumps(Server(paths).status(), indent=2))
    except EOFError, KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"tileception-github: {error}", file=sys.stderr)
        raise SystemExit(1) from None
