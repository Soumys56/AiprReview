import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from config.setting import settings
from service.symbol import extract, is_excluded, language_for


class IndexStore:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path or settings.INDEX_DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS indexed_commits (
                    commit_sha TEXT PRIMARY KEY,
                    indexed_at TEXT,
                    file_count INTEGER,
                    symbol_count INTEGER
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS symbols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    commit_sha TEXT,
                    symbol_id TEXT,
                    file_path TEXT,
                    name TEXT,
                    short_name TEXT,
                    kind TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    exported INTEGER
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS refs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    commit_sha TEXT,
                    file_path TEXT,
                    from_symbol TEXT,
                    to_name TEXT,
                    qualifier TEXT,
                    line INTEGER,
                    arg_count INTEGER,
                    resolved_symbol_id TEXT,
                    is_ambiguous INTEGER
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sym_sha_id ON symbols(commit_sha, symbol_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_refs_sha_res ON refs(commit_sha, resolved_symbol_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_refs_sha_name ON refs(commit_sha, to_name)")
            self.conn.commit()

    def is_indexed(self, commit_sha: str) -> bool:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT 1 FROM indexed_commits WHERE commit_sha = ? LIMIT 1", (commit_sha,))
            return cur.fetchone() is not None

    def build(self, mirror, commit_sha: str) -> dict:
        """Index all supported source files at commit_sha (cached per SHA)."""
        if self.is_indexed(commit_sha):
            with self._lock:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT file_count, symbol_count FROM indexed_commits WHERE commit_sha = ?",
                    (commit_sha,),
                )
                row = cur.fetchone()
            return {
                "commit_sha": commit_sha,
                "cached": True,
                "file_count": row["file_count"] if row else 0,
                "symbol_count": row["symbol_count"] if row else 0,
            }

        all_files = mirror.list_files(commit_sha)
        target_files = [
            f for f in all_files
            if not is_excluded(f) and language_for(f) is not None
        ]

        all_symbols: list[dict] = []
        all_refs: list[dict] = []

        for file_path in target_files:
            raw = mirror.read(commit_sha, file_path)
            if not raw or b"\0" in raw[:8000]:
                continue
            try:
                extracted = extract(file_path, raw)
            except Exception:
                continue
            all_symbols.extend(extracted["symbols"])
            all_refs.extend(extracted["refs"])

        # Build symbol lookup maps for deterministic call resolution
        by_file_qual: dict[tuple[str, str], str] = {}
        by_file_short: dict[tuple[str, str], list[str]] = {}
        by_short: dict[str, list[str]] = {}

        for s in all_symbols:
            by_file_qual[(s["file"], s["name"])] = s["symbol_id"]
            by_file_short.setdefault((s["file"], s["short_name"]), []).append(s["symbol_id"])
            by_short.setdefault(s["short_name"], []).append(s["symbol_id"])

        resolved_rows = []
        for r in all_refs:
            file_path = r["path"]
            to_name = r["to_name"]
            qualifier = r.get("qualifier")
            from_sym = r["from_symbol"]

            resolved_id: str | None = None
            is_ambiguous = 0

            # 1. Self/cls method call within the same class
            if qualifier in ("self", "cls") and "#" in from_sym:
                caller_qual = from_sym.split("#", 1)[1]
                if "." in caller_qual:
                    cls_name = caller_qual.rsplit(".", 1)[0]
                    resolved_id = by_file_qual.get((file_path, f"{cls_name}.{to_name}"))

            # 2. Unqualified call to a symbol defined in the same file
            if resolved_id is None and qualifier is None:
                local_matches = by_file_short.get((file_path, to_name), [])
                if len(local_matches) == 1:
                    resolved_id = local_matches[0]
                elif len(local_matches) > 1:
                    is_ambiguous = 1

            # 3. Repo-wide lookup by short_name
            if resolved_id is None and not is_ambiguous:
                repo_matches = by_short.get(to_name, [])
                if len(repo_matches) == 1:
                    resolved_id = repo_matches[0]
                elif len(repo_matches) > 1:
                    is_ambiguous = 1

            resolved_rows.append((
                commit_sha,
                file_path,
                from_sym,
                to_name,
                qualifier,
                r["line"],
                r["arg_count"],
                resolved_id,
                is_ambiguous,
            ))

        with self._lock:
            cur = self.conn.cursor()
            cur.executemany(
                """
                INSERT INTO symbols (
                    commit_sha, symbol_id, file_path, name, short_name,
                    kind, start_line, end_line, exported
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        commit_sha,
                        s["symbol_id"],
                        s["file"],
                        s["name"],
                        s["short_name"],
                        s["kind"],
                        s["start_line"],
                        s["end_line"],
                        int(s["exported"]),
                    )
                    for s in all_symbols
                ],
            )
            cur.executemany(
                """
                INSERT INTO refs (
                    commit_sha, file_path, from_symbol, to_name, qualifier,
                    line, arg_count, resolved_symbol_id, is_ambiguous
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                resolved_rows,
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO indexed_commits (commit_sha, indexed_at, file_count, symbol_count)
                VALUES (?, ?, ?, ?)
                """,
                (
                    commit_sha,
                    datetime.now(timezone.utc).isoformat(),
                    len(target_files),
                    len(all_symbols),
                ),
            )
            self.conn.commit()

        return {
            "commit_sha": commit_sha,
            "cached": False,
            "file_count": len(target_files),
            "symbol_count": len(all_symbols),
        }

    def callers(self, commit_sha: str, symbol_id: str, depth: int = 1) -> list[dict]:
        """Return level 1..depth callers for symbol_id in the format expected by Stage2Pipeline."""
        if depth <= 0:
            return []

        short_name = symbol_id.split("#", 1)[1].rsplit(".", 1)[-1]
        results: list[dict] = []
        visited_symbols = {symbol_id}
        frontier = [(symbol_id, short_name)]

        with self._lock:
            cur = self.conn.cursor()
            for level in range(1, depth + 1):
                next_frontier: list[tuple[str, str]] = []
                for target_id, target_short in frontier:
                    cur.execute(
                        """
                        SELECT file_path, from_symbol, line, arg_count
                        FROM refs
                        WHERE commit_sha = ?
                          AND (
                              resolved_symbol_id = ?
                              OR (resolved_symbol_id IS NULL AND is_ambiguous = 1 AND to_name = ?)
                          )
                        """,
                        (commit_sha, target_id, target_short),
                    )
                    for row in cur.fetchall():
                        results.append({
                            "file": row["file_path"],
                            "from_symbol": row["from_symbol"],
                            "line": row["line"],
                            "level": level,
                            "arg_count": row["arg_count"],
                        })
                        caller_sym = row["from_symbol"]
                        if (
                            level < depth
                            and caller_sym not in visited_symbols
                            and not caller_sym.endswith("#<module>")
                        ):
                            visited_symbols.add(caller_sym)
                            caller_short = caller_sym.split("#", 1)[1].rsplit(".", 1)[-1]
                            next_frontier.append((caller_sym, caller_short))
                frontier = next_frontier
                if not frontier:
                    break

        return results

    def unresolved_count(self, commit_sha: str, short_name: str) -> int:
        """Count ambiguous call sites for short_name at commit_sha."""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM refs
                WHERE commit_sha = ? AND to_name = ? AND is_ambiguous = 1
                """,
                (commit_sha, short_name),
            )
            row = cur.fetchone()
            return int(row["cnt"]) if row else 0