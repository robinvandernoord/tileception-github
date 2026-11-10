"""Query parsing, ranking and launcher rows."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .config import REPO_PATTERN
from .store import Frecency, Store, rank_bonus

URL = re.compile(
    r"https?://(?:www\.)?github\.com/(?P<repo>[^/\s]+/[^/\s#?]+)(?:/(?:pull|issues)/(?P<number>\d+))?\S*"
)
RANK_WEIGHT = 0.15
REFERENCE = re.compile(r"(?:(?P<hint>[^\s#]+?)(?:\s+|\s*#))?\s*#?(?P<number>\d{1,9})")


@dataclass(frozen=True)
class Query:
    text: str
    number: int | None = None
    # None searches every selected repository; a list restricts to these.
    repos: list[str] | None = None
    # Repositories named exactly (URL or owner/name), looked up live even when not selected.
    exact: bool = False
    hint: str = ""


def match_repos(hint: str, selected: list[str], fuzzy: bool = True) -> list[str]:
    """Selected repositories for a typed hint: exact, then substring, then fuzzy."""
    folded = hint.casefold()
    names = {repo: repo.casefold().split("/", 1)[1] for repo in selected}
    exact = [repo for repo, name in names.items() if folded in {repo.casefold(), name}]
    if exact:
        return exact
    contained = [repo for repo, name in names.items() if folded in repo.casefold()]
    if contained:
        return contained
    if not fuzzy:
        return []
    return [
        repo for repo, name in names.items() if len(folded) >= 3 and fuzz.partial_ratio(folded, name) >= 80
    ]


def parse(query: str, selected: list[str]) -> Query:
    text = query.strip()
    if match := URL.fullmatch(text):
        repo = match["repo"].removesuffix(".git")
        number = int(match["number"]) if match["number"] else None
        return Query(text, number, [canonical(repo, selected)], exact=True)
    if match := REFERENCE.fullmatch(text):
        number = int(match["number"])
        hint = match["hint"]
        if not hint:
            return Query(text, number)
        if REPO_PATTERN.fullmatch(hint):
            return Query(text, number, [canonical(hint, selected)], exact=True, hint=hint)
        # An empty list means the hint names no selected repository; callers fall back to text.
        return Query(text, number, match_repos(hint, selected), hint=hint)
    if REPO_PATTERN.fullmatch(text):
        return Query(text, repos=[canonical(text, selected)], exact=True, hint=text)
    return Query(text)


def canonical(repo: str, selected: list[str]) -> str:
    return next((item for item in selected if item.casefold() == repo.casefold()), repo)


def cached_results(store: Store, query: Query, selected: list[str], frecency: Frecency) -> list[dict]:
    if query.number is not None and query.repos == []:
        query = Query(query.text)  # "release 2024" names no repository; search it as text.
    if query.number is not None:
        items = store.by_number(query.number, selected if query.repos is None else query.repos)
        return sorted(items, key=lambda item: -rank_bonus(item, frecency))
    if query.repos is not None:
        return store.recent(query.repos)
    scored = {}
    first, _, rest = query.text.partition(" ")
    if rest.strip():
        # "tile lockscreen": a leading word naming repositories scopes the remaining words.
        # Only exact and substring names count, and the words must find something there.
        hinted = [canonical(first, selected)] if REPO_PATTERN.fullmatch(first) else []
        hinted = hinted or match_repos(first, selected, fuzzy=False)
        if hinted:
            scored = store.text_search(rest, hinted)
    scored = scored or store.text_search(query.text)
    # A lone word naming repositories also lists their recent activity.
    if repos := named_repos(query, selected):
        for item in store.recent(repos, limit=10):
            key = (item["repo"], item["number"])
            scored[key] = (max(scored.get(key, (0,))[0], 90), item)
    # Relevance decides; ranking signals only order equally relevant items.
    ranked = sorted(
        scored.values(), key=lambda pair: -(pair[0] + RANK_WEIGHT * rank_bonus(pair[1], frecency))
    )
    return [item for _, item in ranked[:32]]


def named_repos(query: Query, selected: list[str]) -> list[str]:
    """Repositories a query names without a number: owner/name, a URL or one word."""
    if query.number is not None:
        return []
    if query.repos is not None:
        return query.repos
    if not query.text or " " in query.text:
        return []
    repos = match_repos(query.text, selected)
    return repos if len(repos) <= 5 else []


def repo_row(repo: str, prefix: str, command: str) -> dict:
    url = f"https://github.com/{repo}"
    return {
        "title": repo,
        "detail": "Repository home page",
        "icon": "glyph:globe",
        "completion": f"{prefix} {repo} ",
        "actions": [{"title": "Open on GitHub", "type": "run", "command": command, "args": ["open", url]}],
        "alternate": {"title": "Copy URL", "type": "copy", "text": url},
    }


def row(item: dict, prefix: str, command: str, live: bool = False) -> dict:
    kind = "PR" if item["kind"] == "pr" else "Issue"
    detail = [
        f"{kind} #{item['number']}",
        item["repo"],
        item["state"],
        f"@{item['author']}",
        item["branch"],
        item["labels"],
        "live" if live else "",
    ]
    return {
        "title": item["title"],
        "detail": " · ".join(filter(None, detail)),
        "icon": "glyph:globe",
        # Tab narrows the search to this repository.
        "completion": f"{prefix} {item['repo']} ",
        "actions": [
            {"title": "Open on GitHub", "type": "run", "command": command, "args": ["open", item["url"]]}
        ],
        "alternate": {"title": "Copy URL", "type": "copy", "text": item["url"]},
    }
