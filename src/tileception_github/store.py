"""SQLite caches: synchronized items (sync writes) and launcher visits (open writes)."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from rapidfuzz import fuzz

SEARCH_WORD = re.compile(r"[^\W_]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
    repo TEXT NOT NULL, number INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL, state TEXT NOT NULL, author TEXT NOT NULL, labels TEXT NOT NULL,
    branch TEXT NOT NULL, url TEXT NOT NULL, updated TEXT NOT NULL, involved INTEGER NOT NULL,
    PRIMARY KEY(repo, number)
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, repo, labels, branch, author, body, content='items', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid,title,repo,labels,branch,author,body)
    VALUES (new.rowid,new.title,new.repo,new.labels,new.branch,new.author,new.body);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts,rowid,title,repo,labels,branch,author,body)
    VALUES ('delete',old.rowid,old.title,old.repo,old.labels,old.branch,old.author,old.body);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts,rowid,title,repo,labels,branch,author,body)
    VALUES ('delete',old.rowid,old.title,old.repo,old.labels,old.branch,old.author,old.body);
    INSERT INTO items_fts(rowid,title,repo,labels,branch,author,body)
    VALUES (new.rowid,new.title,new.repo,new.labels,new.branch,new.author,new.body);
END;
"""

# Text relevance tiers; ranking signals add at most about 20 points within a tier.
TIER_ALL = 1000
TIER_ANY = 500
VISITS_SCHEMA = "CREATE TABLE IF NOT EXISTS visits (url TEXT NOT NULL, repo TEXT NOT NULL, at REAL NOT NULL)"
VISIT_HALF_LIFE_DAYS = 14
VISIT_RETENTION_DAYS = 180


class Store:
    def __init__(self, filename: Path, writable: bool = False):
        if writable:
            self.db = sqlite3.connect(filename, timeout=5)
            self.db.execute("PRAGMA journal_mode=WAL")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
                # Reindex existing caches so branch components separated by '-' or '_'
                # are searchable too. Keep the items and their sync checkpoints intact.
                self.db.executescript(
                    "BEGIN; DROP TABLE IF EXISTS items_fts;"
                    + SCHEMA
                    + "INSERT INTO items_fts(items_fts) VALUES ('rebuild');"
                    "PRAGMA user_version=1; COMMIT;"
                )
            else:
                self.db.executescript(SCHEMA)
            filename.chmod(0o600)
        else:
            self.db = sqlite3.connect(f"{filename.as_uri()}?mode=ro", uri=True, timeout=0.2)
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def delete_meta(self, prefix: str):
        self.db.execute("DELETE FROM meta WHERE key LIKE ? ESCAPE '\\'", (escape_like(prefix) + "%",))

    def put(self, item: dict, me: str):
        self.db.execute(
            """
            INSERT INTO items VALUES (
                :repo,:number,:kind,:title,:body,:state,:author,:labels,:branch,:url,:updated,:involved
            )
            ON CONFLICT(repo,number) DO UPDATE SET kind=excluded.kind, title=excluded.title,
            body=excluded.body, state=excluded.state, author=excluded.author, labels=excluded.labels,
            branch=excluded.branch, url=excluded.url, updated=excluded.updated, involved=excluded.involved
            """,
            {**item, "involved": int(me in item["people"])},
        )

    def repos(self) -> set[str]:
        """Repositories with cached items or sync state."""
        found = {row[0] for row in self.db.execute("SELECT DISTINCT repo FROM items")}
        for (key,) in self.db.execute("SELECT key FROM meta WHERE key LIKE 'repo:%:checkpoint'"):
            found.add(key.removeprefix("repo:").removesuffix(":checkpoint"))
        return found

    def remove_repo(self, repo: str):
        self.db.execute("DELETE FROM items WHERE repo=?", (repo,))
        self.delete_meta(f"repo:{repo}:")

    def prune(self, repo: str, numbers: set[int]):
        for row in self.db.execute("SELECT number FROM items WHERE repo=?", (repo,)).fetchall():
            if row[0] not in numbers:
                self.db.execute("DELETE FROM items WHERE repo=? AND number=?", (repo, row[0]))

    def count(self) -> int:
        return self.db.execute("SELECT count(*) FROM items").fetchone()[0]

    def by_number(self, number: int, repos: list[str]) -> list[dict]:
        if not repos:
            return []
        marks = ",".join("?" * len(repos))
        return [
            dict(row)
            for row in self.db.execute(
                f"SELECT * FROM items WHERE number=? AND repo COLLATE NOCASE IN ({marks})", (number, *repos)
            )
        ]

    def recent(self, repos: list[str] | None = None, involved: bool = False, limit: int = 24) -> list[dict]:
        conditions, values = [], []
        if repos is not None:
            if not repos:
                return []
            conditions.append(f"repo COLLATE NOCASE IN ({','.join('?' * len(repos))})")
            values += repos
        if involved:
            conditions.append("involved=1 AND state IN ('open','draft')")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return [
            dict(row)
            for row in self.db.execute(
                f"SELECT * FROM items {where} ORDER BY updated DESC LIMIT ?", (*values, limit)
            )
        ]

    def text_search(
        self, query: str, repos: list[str] | None = None, limit: int = 100
    ) -> dict[tuple[str, int], tuple[float, dict]]:
        """Candidates with a text relevance score; callers add ranking signals as tie-breakers.

        Metadata containing every word outranks metadata containing some, followed by fuzzy
        matches. Bodies are excluded to keep templates and incidental mentions out of results.
        Without the tiers, one common word (such as part of the repository name)
        lets open, recent items drown out the item that actually mentions the rest.
        """
        ranked: dict[tuple[str, int], tuple[float, dict | None]] = {}
        scope, values = "", []
        if repos is not None:
            if not repos:
                return {}
            scope = f" AND i.repo COLLATE NOCASE IN ({','.join('?' * len(repos))})"
            values = list(repos)
        terms = ['"' + token + '"*' for token in SEARCH_WORD.findall(query)[:24]]
        hits = []
        for operator, tier in ((" AND ", TIER_ALL), (" OR ", TIER_ANY)):
            if not terms:
                break
            hits = self.db.execute(
                f"""SELECT i.* FROM items_fts JOIN items i ON i.rowid=items_fts.rowid
                WHERE items_fts MATCH ?{scope} ORDER BY bm25(items_fts,8,4,3,3,2,1) LIMIT ?""",
                ("{title repo labels branch author} : (" + operator.join(terms) + ")", *values, limit),
            ).fetchall()
            for index, row in enumerate(hits):
                ranked[(row["repo"], row["number"])] = (tier - index, dict(row))
            if hits or len(terms) == 1:
                break
        if len(hits) < 5:
            # Typo tolerance for short metadata, only while exact words find little: every
            # typed word must closely resemble some word of the title, branch or labels.
            words = SEARCH_WORD.findall(query.casefold())[:24]
            sql = "SELECT repo,number,title,branch,labels FROM items i WHERE 1=1" + scope
            for row in self.db.execute(sql, values) if words else ():
                text = f"{row['title']} {row['branch']} {row['labels']}".casefold()
                candidates = set(SEARCH_WORD.findall(text))
                score = min(
                    max((fuzz.ratio(word, other) for other in candidates), default=0) for word in words
                )
                key = (row["repo"], row["number"])
                if score >= 80 and key not in ranked:
                    ranked[key] = (score, None)
        output = {}
        for key, (score, row) in sorted(ranked.items(), key=lambda pair: -pair[1][0])[:limit]:
            if row is None:
                row = dict(self.db.execute("SELECT * FROM items WHERE repo=? AND number=?", key).fetchone())
            output[key] = (score, row)
        return output


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def record_visit(filename: Path, url: str, repo: str):
    db = sqlite3.connect(filename, timeout=2)
    try:
        filename.chmod(0o600)
        with db:
            db.execute(VISITS_SCHEMA)
            db.execute("INSERT INTO visits VALUES (?,?,?)", (url, repo, time.time()))
            db.execute("DELETE FROM visits WHERE at < ?", (time.time() - VISIT_RETENTION_DAYS * 86400,))
    finally:
        db.close()


class Frecency:
    """Exponentially decaying visit counts per repository and per item URL."""

    def __init__(self, filename: Path):
        self.repos: dict[str, float] = {}
        self.urls: dict[str, float] = {}
        if not filename.exists():
            return
        try:
            db = sqlite3.connect(f"{filename.as_uri()}?mode=ro", uri=True, timeout=0.2)
            try:
                rows = db.execute("SELECT url,repo,at FROM visits").fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            return  # Ranking must never break search.
        now = time.time()
        for url, repo, at in rows:
            weight = 0.5 ** ((now - at) / 86400 / VISIT_HALF_LIFE_DAYS)
            self.repos[repo.casefold()] = self.repos.get(repo.casefold(), 0) + weight
            self.urls[url] = self.urls.get(url, 0) + weight


def rank_bonus(item: dict, frecency: Frecency) -> float:
    """Signals for choosing between equally good matches, such as every repository's #105."""
    # Open work outranks involvement and recency combined.
    bonus = {"open": 50, "draft": 45}.get(item["state"], 0)
    bonus += 20 if item.get("involved") else 0
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(item["updated"].replace("Z", "+00:00"))).days
        bonus += 20 * 0.5 ** (max(0, age) / 30)
    except ValueError:
        pass
    bonus += min(25, 6 * frecency.repos.get(item["repo"].casefold(), 0))
    bonus += min(15, 8 * frecency.urls.get(item["url"], 0))
    return bonus
