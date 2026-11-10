"""Newline-delimited MCP, with one debounced live-lookup worker."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Config, Paths
from .github import GitHub
from .progress import read_progress
from .search import cached_results, named_repos, parse, repo_row, row
from .store import Frecency, Store, rank_bonus

SERVICE = "tileception-github-sync.service"
# One aliased GraphQL request; the most recently pushed repositories come first.
LIVE_LOOKUP_LIMIT = 40


def selected_repos(config: Config, store: Store | None) -> list[str]:
    """The whitelist, or the automatic selection of the last sync, minus the blacklist."""
    if config.repos:
        return config.selection([])
    try:
        recorded = json.loads(store.get("selection") or "[]") if store else []
    except ValueError, sqlite3.Error:
        recorded = []
    excluded = {repo.casefold() for repo in config.exclude_repos}
    return [repo for repo in recorded if isinstance(repo, str) and repo.casefold() not in excluded]


def tool(value: dict) -> dict:
    return {"structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}


def own_command() -> str:
    """Absolute path of this executable, for run actions that record visits."""
    current = Path(sys.argv[0])
    if current.name == "tileception-github" and current.exists():
        return str(current.absolute())
    return shutil.which("tileception-github") or "tileception-github"


class Server:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.command = own_command()
        self.output = threading.Lock()
        self.condition = threading.Condition()
        self.pending = None
        self.serial = 0
        self.closed = False
        self.worker = threading.Thread(target=self.lookup_worker, name="github-lookup", daemon=True)
        self.worker.start()

    def send(self, value: dict):
        with self.output:
            print(json.dumps(value, ensure_ascii=False), flush=True)

    def cancel(self):
        with self.condition:
            self.serial += 1
            self.pending = None
            self.condition.notify()

    def cache(self) -> Store | None:
        if not self.paths.database.exists():
            return None
        try:
            return Store(self.paths.database)
        except sqlite3.Error:
            return None

    def status(self) -> dict:
        status = {
            "last_success": None,
            "last_error": "",
            "items": 0,
            "repos": 0,
            "selected_repos": 0,
            "sync_progress": read_progress(self.paths),
        }
        try:
            config = Config.load(self.paths)
            store = self.cache()
            status["selected_repos"] = len(selected_repos(config, store))
            if store:
                try:
                    status.update(
                        last_success=store.get("last_success"),
                        last_error=store.get("last_error") or "",
                        items=store.count(),
                        repos=len(store.repos()),
                    )
                finally:
                    store.close()
        except (ValueError, OSError, sqlite3.Error) as error:
            status["last_error"] = str(error)
        return status

    def status_rows(self) -> list[dict]:
        status = self.status()
        last_success = status["last_success"]
        if last_success:
            last_success = datetime.fromisoformat(last_success).astimezone().strftime("%d %b %Y %H:%M %Z")
        rows = [
            {
                "title": "Sync GitHub now",
                "detail": "Refresh the local pull request and issue cache",
                "icon": "glyph:globe",
                "actions": [
                    {"title": "Sync GitHub now", "type": "tool", "name": "sync_now", "arguments": {}}
                ],
            },
            {
                "title": f"Last successful sync: {last_success or 'never'}",
                "detail": f"{status['items']} cached items from {status['repos']} of "
                f"{status['selected_repos']} selected repositories",
                "actions": [],
            },
        ]
        if not status["selected_repos"] and not status["last_success"]:
            rows.insert(
                0,
                {
                    "title": "Not synchronized yet",
                    "detail": "Sync to find your active repositories, or pick with tileception-github repos",
                    "actions": [],
                },
            )
        progress = status["sync_progress"]
        if progress and progress.get("state") != "complete":
            title = {"running": "Sync in progress", "failed": "Sync failed"}.get(
                progress.get("state"), "Sync interrupted"
            )
            rows.insert(1, {"title": title, "detail": progress["message"], "actions": []})
        running = progress and progress.get("state") == "running"
        if status["last_error"] and not running:
            rows.append({"title": "GitHub sync error", "detail": status["last_error"], "actions": []})
        return rows

    def search(self, arguments: dict) -> tuple[dict, tuple | None]:
        self.cancel()
        query = arguments.get("query", "")
        prefix = arguments.get("prefix") or "gh"
        if not isinstance(query, str) or len(query) > 4096:
            raise ValueError("Query must be text of at most 4096 characters")
        config = Config.load(self.paths)
        store = self.cache()
        repos = selected_repos(config, store)
        parsed = parse(query, repos)
        frecency = Frecency(self.paths.visits)
        items, me = [], None
        try:
            if store and not parsed.text:
                # Bare prefix: status first, then open work that involves you.
                items = store.recent(involved=True, limit=15)
                items.sort(key=lambda item: -rank_bonus(item, frecency))
            elif store:
                items = cached_results(store, parsed, repos, frecency)
                me = store.get("login")
        except sqlite3.Error:
            pass  # A cache being replaced must not break live lookups.
        finally:
            if store:
                store.close()
        if not parsed.text:
            return tool(
                {"rows": self.status_rows() + [row(item, prefix, self.command) for item in items]}
            ), None
        # Repositories named without a number come first, most often opened first.
        homes = sorted(named_repos(parsed, repos), key=lambda repo: -frecency.repos.get(repo.casefold(), 0))
        rows = [repo_row(repo, prefix, self.command) for repo in homes]
        rows += [row(item, prefix, self.command) for item in items]
        if parsed.number is not None and parsed.repos == []:
            rows.insert(
                0,
                {
                    "title": f"No synchronized repository matches {parsed.hint!r}",
                    "detail": f"Type owner/name#{parsed.number} to look it up directly; text matches follow",
                    "actions": [],
                },
            )
        pending = None
        generation = arguments.get("_meta", {}).get("tileception/generation")
        targets = (repos if parsed.repos is None else parsed.repos)[:LIVE_LOOKUP_LIMIT]
        if parsed.number is not None and config.live_lookup and generation is not None and targets:
            pending = (
                self.serial,
                time.monotonic() + config.debounce_ms / 1000,
                generation,
                parsed.number,
                targets,
                {(item["repo"].casefold(), item["number"]) for item in items},
                prefix,
                me,
            )
        if not rows and not pending:
            rows = [{"title": "No matching pull requests or issues", "detail": parsed.text, "actions": []}]
            if not store:
                rows += self.status_rows()
        return tool({"rows": rows}), pending

    def lookup_worker(self):
        client = GitHub()
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                job = self.pending
                remaining = job[1] - time.monotonic()
                if remaining > 0:
                    self.condition.wait(timeout=remaining)
                    continue
                self.pending = None
            serial, _, generation, number, targets, existing, prefix, me = job
            try:
                found = client.lookup(targets, number)
                for item in found:
                    item["involved"] = bool(me and me in item["people"])
                frecency = Frecency(self.paths.visits)
                found.sort(key=lambda item: -rank_bonus(item, frecency))
                rows = [
                    row(item, prefix, self.command, live=True)
                    for item in found
                    if (item["repo"].casefold(), item["number"]) not in existing
                ]
                if not found and not existing:
                    scope = targets[0] if len(targets) == 1 else f"{len(targets)} repositories"
                    rows = [{"title": f"No #{number} in {scope}", "actions": []}]
            except Exception as error:
                message = str(error) if isinstance(error, ValueError) else type(error).__name__
                rows = [{"title": "GitHub lookup failed", "detail": message, "actions": []}]
            with self.condition:
                if serial == self.serial and not self.closed and rows:
                    try:
                        self.send(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/tileception/launcher_append",
                                "params": {"generation": generation, "rows": rows},
                            }
                        )
                    except BrokenPipeError:
                        return

    def dispatch(self, request: dict) -> tuple[dict, tuple | None]:
        method = request.get("method")
        if method == "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tileception-github", "version": __version__},
            }, None
        if method == "ping":
            return {}, None
        if method == "tools/list":
            return {
                "tools": [
                    {"name": name, "description": description, "inputSchema": schema}
                    for name, description, schema in [
                        ("launcher_info", "GitHub search metadata", {"type": "object"}),
                        (
                            "launcher_search",
                            "Search pull requests and issues",
                            {
                                "type": "object",
                                "properties": {"prefix": {"type": "string"}, "query": {"type": "string"}},
                                "required": ["prefix", "query"],
                            },
                        ),
                        ("sync_now", "Start background GitHub synchronization", {"type": "object"}),
                        ("status", "Last synchronization time and errors", {"type": "object"}),
                    ]
                ]
            }, None
        if method != "tools/call":
            raise LookupError("Method not found")
        params = request.get("params", {})
        name = params.get("name")
        if name == "launcher_info":
            return tool(
                {
                    "name": "GitHub",
                    "description": "Pull request and issue search",
                    "prefixes": ["gh"],
                    "icon": "glyph:globe",
                }
            ), None
        if name == "launcher_search":
            return self.search(params.get("arguments", {}))
        if name == "status":
            return tool(self.status()), None
        if name == "sync_now":
            result = subprocess.run(
                ["systemctl", "--user", "start", "--no-block", SERVICE], capture_output=True, timeout=1.5
            )
            if result.returncode:
                raise ValueError(
                    "Could not start sync; install the user service with tileception-github install"
                )
            return tool(
                {"text": "GitHub synchronization started. Type gh followed by a space for status."}
            ), None
        raise ValueError("Unknown tool")

    def run(self):
        try:
            while line := sys.stdin.buffer.readline(1024 * 1024 + 1):
                if len(line) > 1024 * 1024:
                    raise ValueError("MCP message exceeded 1 MiB")
                request = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("MCP request must be an object")
                    if "id" not in request:
                        if request.get("method") == "notifications/cancelled":
                            self.cancel()
                        continue
                    result, pending = self.dispatch(request)
                    self.send({"jsonrpc": "2.0", "id": request["id"], "result": result})
                    # The initial response must precede any append.
                    if pending:
                        with self.condition:
                            self.pending = pending
                            self.condition.notify()
                except LookupError:
                    self.send(
                        {
                            "jsonrpc": "2.0",
                            "id": request.get("id"),
                            "error": {"code": -32601, "message": "Method not found"},
                        }
                    )
                except Exception as error:
                    message = str(error) if isinstance(error, (ValueError, OSError)) else type(error).__name__
                    if isinstance(request, dict) and request.get("method") == "tools/call":
                        self.send(
                            {
                                "jsonrpc": "2.0",
                                "id": request.get("id"),
                                "result": {"isError": True, "content": [{"type": "text", "text": message}]},
                            }
                        )
                    else:
                        self.send(
                            {
                                "jsonrpc": "2.0",
                                "id": request.get("id") if isinstance(request, dict) else None,
                                "error": {"code": -32600, "message": "Invalid request"},
                            }
                        )
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify()
