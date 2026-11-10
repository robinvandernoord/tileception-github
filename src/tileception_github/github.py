"""Read-only GitHub GraphQL client, authenticated through the gh CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__

ENDPOINT = "https://api.github.com/graphql"
AFFILIATIONS = "[OWNER, COLLABORATOR, ORGANIZATION_MEMBER]"
BODY_LIMIT = 8000

COMMON = """
number title bodyText state url updatedAt
author { login }
labels(first: 10) { nodes { name } }
assignees(first: 10) { nodes { login } }
participants(first: 30) { nodes { login } }
"""
ISSUE = f"fragment IssueFields on Issue {{ {COMMON} }}"
PULL = f"""fragment PullFields on PullRequest {{ {COMMON} isDraft headRefName
reviewRequests(first: 10) {{ nodes {{ requestedReviewer {{ ... on User {{ login }} }} }} }} }}"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The bearer token must never follow a redirect anywhere.
        raise ValueError("GitHub API redirected unexpectedly")


class NotFound(ValueError):
    pass


def gh_token() -> str:
    gh = shutil.which("gh")
    if not gh:
        raise ValueError("The GitHub CLI (gh) is not installed")
    try:
        result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise ValueError("gh auth token timed out; is the keyring unlocked?") from None
    token = result.stdout.strip()
    if result.returncode or not token:
        raise ValueError("gh is not logged in; run gh auth login")
    return token


def split_repo(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    return owner, name


class GitHub:
    def __init__(self):
        self.token: str | None = None
        self.opener = build_opener(NoRedirect())

    def query(self, text: str, variables: dict | None = None, allow_missing: bool = False) -> dict:
        """Run a GraphQL query; with allow_missing, NOT_FOUND parts come back as null."""
        for attempt in range(2):
            self.token = self.token or gh_token()
            request = Request(
                ENDPOINT,
                data=json.dumps({"query": text, "variables": variables or {}}).encode(),
                headers={
                    "Authorization": f"bearer {self.token}",
                    "Content-Type": "application/json",
                    "User-Agent": f"tileception-github/{__version__}",
                },
                method="POST",
            )
            try:
                with self.opener.open(request, timeout=30) as response:
                    raw = response.read(32 * 1024 * 1024 + 1)
            except HTTPError as error:
                if error.code == 401 and attempt == 0:
                    self.token = None  # gh may have rotated the token; ask again once.
                    continue
                if error.code in {403, 429}:
                    raise ValueError("GitHub rate limit or permission error; try again later") from None
                raise ValueError(f"GitHub API returned HTTP {error.code}") from None
            except URLError:
                raise ValueError("Could not reach GitHub; cached search is still available") from None
            break
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("GitHub response exceeded 32 MiB")
        try:
            result = json.loads(raw)
        except UnicodeError, json.JSONDecodeError:
            raise ValueError("GitHub returned invalid JSON") from None
        errors = result.get("errors") or []
        missing = [error for error in errors if error.get("type") == "NOT_FOUND"]
        if errors and (len(missing) < len(errors) or not allow_missing):
            if len(missing) == len(errors):
                raise NotFound(missing[0].get("message", "Not found"))
            message = next(error.get("message") for error in errors if error.get("type") != "NOT_FOUND")
            raise ValueError(f"GitHub API error: {message}")
        if not isinstance(result.get("data"), dict):
            raise ValueError("GitHub returned an invalid response")
        return result["data"]

    def login(self) -> str:
        return self.query("query { viewer { login } }")["viewer"]["login"]

    def repositories(self) -> list[dict]:
        """Every repository the viewer owns, collaborates on or reaches through an org."""
        output = []
        cursor = None
        while True:
            data = self.query(
                f"""query($after: String) {{ viewer {{ repositories(first: 100, after: $after,
                affiliations: {AFFILIATIONS}, ownerAffiliations: {AFFILIATIONS},
                orderBy: {{field: PUSHED_AT, direction: DESC}}) {{
                  pageInfo {{ hasNextPage endCursor }}
                  nodes {{ nameWithOwner isArchived isPrivate isFork pushedAt }} }} }} }}""",
                {"after": cursor},
            )["viewer"]["repositories"]
            output += data["nodes"]
            if not data["pageInfo"]["hasNextPage"]:
                return output
            cursor = data["pageInfo"]["endCursor"]

    def changes(self, repo: str, kind: str, since: datetime | None) -> Iterator[list[dict]]:
        """Pages of issues or pull requests, newest update first, down to since."""
        owner, name = split_repo(repo)
        connection, fragment, spread = (
            ("pullRequests", PULL, "PullFields") if kind == "pr" else ("issues", ISSUE, "IssueFields")
        )
        cursor = None
        while True:
            data = self.query(
                f"""query($owner: String!, $name: String!, $after: String) {{
                repository(owner: $owner, name: $name) {{ {connection}(first: 50, after: $after,
                  orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
                    pageInfo {{ hasNextPage endCursor }} nodes {{ ...{spread} }} }} }} }} {fragment}""",
                {"owner": owner, "name": name, "after": cursor},
            )["repository"][connection]
            nodes = [node for node in data["nodes"] if node]
            fresh = [node for node in nodes if since is None or parse_time(node["updatedAt"]) >= since]
            if fresh:
                yield [normalize(repo, kind, node) for node in fresh]
            if len(fresh) < len(nodes) or not data["pageInfo"]["hasNextPage"]:
                return
            cursor = data["pageInfo"]["endCursor"]

    def numbers(self, repo: str) -> set[int]:
        """All current issue and pull request numbers, for pruning deletions and transfers."""
        owner, name = split_repo(repo)
        found = set()
        for connection in ("issues", "pullRequests"):
            cursor = None
            while True:
                data = self.query(
                    f"""query($owner: String!, $name: String!, $after: String) {{
                    repository(owner: $owner, name: $name) {{ {connection}(first: 100, after: $after) {{
                      pageInfo {{ hasNextPage endCursor }} nodes {{ number }} }} }} }}""",
                    {"owner": owner, "name": name, "after": cursor},
                )["repository"][connection]
                found.update(node["number"] for node in data["nodes"] if node)
                if not data["pageInfo"]["hasNextPage"]:
                    break
                cursor = data["pageInfo"]["endCursor"]
        return found

    def lookup(self, repos: list[str], number: int) -> list[dict]:
        """Fetch one number from several repositories in a single request."""
        if not repos:
            return []
        parts = []
        variables: dict[str, Any] = {"number": number}
        for index, repo in enumerate(repos):
            variables[f"o{index}"], variables[f"n{index}"] = split_repo(repo)
            parts.append(
                f"r{index}: repository(owner: $o{index}, name: $n{index}) {{ nameWithOwner "
                "issueOrPullRequest(number: $number) { __typename ...IssueFields ...PullFields } }"
            )
        declarations = ", ".join(f"$o{index}: String!, $n{index}: String!" for index in range(len(repos)))
        data = self.query(
            f"query($number: Int!, {declarations}) {{ {' '.join(parts)} }} {ISSUE} {PULL}",
            variables,
            allow_missing=True,
        )
        output = []
        for index in range(len(repos)):
            repository = data.get(f"r{index}")
            node = repository and repository.get("issueOrPullRequest")
            if node:
                kind = "pr" if node["__typename"] == "PullRequest" else "issue"
                output.append(normalize(repository["nameWithOwner"], kind, node))
        return output


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def logins(connection: dict | None, key: str = "login") -> set[str]:
    return {node[key] for node in (connection or {}).get("nodes", []) if node and node.get(key)}


def normalize(repo: str, kind: str, node: dict) -> dict:
    author = (node.get("author") or {}).get("login") or "ghost"
    people = {author} | logins(node.get("assignees")) | logins(node.get("participants"))
    for request in (node.get("reviewRequests") or {}).get("nodes", []):
        if request and (reviewer := request.get("requestedReviewer")) and reviewer.get("login"):
            people.add(reviewer["login"])
    state = node["state"].lower()
    if kind == "pr" and state == "open" and node.get("isDraft"):
        state = "draft"
    return {
        "repo": repo,
        "number": node["number"],
        "kind": kind,
        "title": node["title"] or "Untitled",
        "body": " ".join((node.get("bodyText") or "").split())[:BODY_LIMIT],
        "state": state,
        "author": author,
        "labels": " ".join(sorted(logins(node.get("labels"), "name"))),
        "branch": node.get("headRefName") or "",
        "url": node["url"],
        "updated": node["updatedAt"],
        # Kept only to compute "involves me"; logins are not stored.
        "people": people,
    }
