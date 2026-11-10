"""Single-writer incremental sync, one repository checkpoint at a time."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from .config import Config, Paths, private_directory
from .github import GitHub, NotFound
from .progress import Progress
from .store import Store

OVERLAP = timedelta(minutes=5)
RECONCILE_EVERY = timedelta(days=1)


@contextmanager
def writer_lock(paths: Paths):
    private_directory(paths.cache)
    with (paths.cache / "sync.lock").open("a") as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another GitHub synchronization is already running") from None
        yield


def sync_repo(store: Store, client: GitHub, repo: str, me: str, progress: Progress, counted: int) -> int:
    started = datetime.now(UTC)
    checkpoint = store.get(f"repo:{repo}:checkpoint")
    since = datetime.fromisoformat(checkpoint) - OVERLAP if checkpoint else None
    seen = set()
    for kind, label in (("issue", "issues"), ("pr", "pull requests")):
        for page in client.changes(repo, kind, since):
            with store.transaction():
                for item in page:
                    store.put(item, me)
            seen.update(item["number"] for item in page)
            progress.update(
                f"{repo}: {len(seen)} changed items saved, fetching {label}", items=counted + len(seen)
            )
    reconciled = store.get(f"repo:{repo}:reconciled")
    if since is None or not reconciled or started - datetime.fromisoformat(reconciled) >= RECONCILE_EVERY:
        # A full listing already names every item; otherwise list numbers to find deletions and transfers.
        numbers = seen if since is None else client.numbers(repo)
        with store.transaction():
            store.prune(repo, numbers)
            store.set(f"repo:{repo}:reconciled", started.isoformat())
    with store.transaction():
        store.set(f"repo:{repo}:checkpoint", started.isoformat())
    return len(seen)


def synchronize(paths: Paths, config: Config):
    with writer_lock(paths):
        store = Store(paths.database, writable=True)
        progress = Progress(paths)
        try:
            progress.update("Starting GitHub synchronization", force=True)
            client = GitHub()
            me = client.login()
            if not config.repos:
                progress.update("Listing your active repositories", phase="listing", force=True)
            repos = config.selection([] if config.repos else client.repositories())
            if not repos:
                raise ValueError("No repositories to synchronize; check tileception-github repos")
            selected = {repo.casefold() for repo in repos}
            with store.transaction():
                if store.get("login") != me:
                    # Another account may see other private repositories.
                    store.db.execute("DELETE FROM items")
                    store.delete_meta("repo:")
                    store.set("login", me)
                for repo in store.repos():
                    if repo.casefold() not in selected:
                        store.remove_repo(repo)
                # The launcher searches and looks up this list, most recently pushed first.
                store.set("selection", json.dumps(repos))
            total = len(repos)
            changed = 0
            missing = []
            for index, repo in enumerate(repos):
                progress.update(
                    f"Synchronizing {repo} ({index + 1}/{total})",
                    phase="fetching",
                    completed_repos=index,
                    total_repos=total,
                    force=True,
                )
                try:
                    changed += sync_repo(store, client, repo, me, progress, changed)
                except NotFound:
                    # One renamed or inaccessible repository must not block the others.
                    missing.append(repo)
            with store.transaction():
                store.set("last_success", datetime.now(UTC).isoformat())
                store.set(
                    "last_error", f"Not found or not accessible: {', '.join(missing)}" if missing else ""
                )
            progress.update(
                f"Synchronization complete: {changed} changed items in {total - len(missing)} repositories",
                state="complete",
                phase="complete",
                completed_repos=total,
                force=True,
            )
        except (Exception, KeyboardInterrupt) as error:
            message = (
                "Synchronization interrupted"
                if isinstance(error, KeyboardInterrupt)
                else (str(error) if isinstance(error, ValueError) else type(error).__name__)
            )
            with store.transaction():
                store.set("last_error", f"{datetime.now(UTC).isoformat()}: {message}")
            progress.update(
                f"{message}. Finished repositories are kept; rerun sync to continue.",
                state="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                force=True,
            )
            raise
        finally:
            store.close()
