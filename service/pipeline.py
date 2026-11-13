import asyncio
import difflib

from components.github_fetchpr_client import fetch_pr_diff
from components.github_fetchpr_client import fetch_pr_diff, fetch_pull_request
from service.symbol import extract, is_excluded, is_test_file, language_for
MAX_CODE_LINES = 150


def _decode(b: bytes | None) -> str:
    return b.decode("utf-8", "replace") if b else ""


def changed_lines(old_text: str, new_text: str) -> tuple[set[int], set[int]]:
    """1-based changed line numbers on the old side and the new side."""
    sm = difflib.SequenceMatcher(None, old_text.splitlines(), new_text.splitlines(), autojunk=False)
    old_c, new_c = set(), set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            old_c.update(range(i1 + 1, i2 + 1))
            new_c.update(range(j1 + 1, j2 + 1))
    return old_c, new_c


def _touched(sym: dict, lines: set[int]) -> bool:
    return any(sym["start_line"] <= line <= sym["end_line"] for line in lines)


def classify(
    old_syms: list[dict],
    new_syms: list[dict],
    old_path: str,
    new_path: str,
    old_changed: set[int],
    new_changed: set[int],
) -> list[dict]:
    # Re-key old symbols to the new path so file renames don't look like delete + add
    old = {s["symbol_id"].replace(f"{old_path}#", f"{new_path}#", 1): s for s in old_syms}
    new = {s["symbol_id"]: s for s in new_syms}
    changes = []

    for key in new.keys() - old.keys():
        changes.append({"symbol": new[key], "old": None, "change_type": "added"})

    for key in old.keys() - new.keys():
        changes.append({"symbol": old[key], "old": old[key], "change_type": "deleted"})

    for key in old.keys() & new.keys():
        o, n = old[key], new[key]
        if not (_touched(n, new_changed) or _touched(o, old_changed)):
            continue
        if o["signature"] != n["signature"]:
            ct = "signature_changed"
        elif o["exported"] != n["exported"]:
            ct = "visibility_changed"
        elif n["kind"] == "class":
            continue  # body changes are reported on the methods themselves
        elif o["norm_hash"] == n["norm_hash"]:
            continue  # whitespace-only -> cosmetic, linters handle it
        else:
            ct = "body_only"
        changes.append({"symbol": n, "old": o, "change_type": ct})

    return changes


class Stage2Pipeline:
    def __init__(self, store, mirror, owner: str, repo: str, repo_rules: list[str] | str | None = None):
        self.store = store
        self.mirror = mirror
        self.owner = owner
        self.repo = repo
        self.repo_rules = repo_rules

    async def run(self, pr_number: int) -> dict:
        pr = await fetch_pull_request(self.owner, self.repo, pr_number)
        files = await fetch_pr_diff(self.owner, self.repo, pr_number)
        return await asyncio.to_thread(self._run_sync, pr, files)

    # ---------------------------------------------------------------- core
    def _run_sync(self, pr: dict, files: list[dict]) -> dict:
        base, head = pr["base_sha"], pr["head_sha"]
        self.mirror.ensure(base, head, pr["number"])
        index_info = self.store.build(self.mirror, base)  # Phase A (cached per sha)

        chunks, excluded, warnings = [], [], []
        ctx = {
            "base": base,
            "head": head,
            "head_cache": {},
            "text_cache": {},
            "pr_paths": set(),
            "removed": set(),
            "renamed": {},
            "warnings": warnings,
        }

        for f in files:
            ctx["pr_paths"].add(f["filename"])
            prev = f.get("previous_filename")
            if prev:
                ctx["pr_paths"].add(prev)
                ctx["renamed"][prev] = f["filename"]
            if f["status"] == "removed":
                ctx["removed"].add(f["filename"])

        for f in files:
            path, status = f["filename"], f["status"]
            old_path = f.get("previous_filename") or path

            if is_excluded(path) or is_excluded(old_path):
                excluded.append({"file": path, "reason": "lockfile/vendor/generated"})
                continue

            old_src = None if status == "added" else self.mirror.read(base, old_path)
            new_src = None if status == "removed" else self.mirror.read(head, path)

            if any(s and b"\0" in s[:8000] for s in (old_src, new_src)):
                excluded.append({"file": path, "reason": "binary"})
                continue

            if not language_for(path):
                chunks.append(self._window_chunk(f, "unsupported_language"))
                continue

            try:
                old_ex = extract(old_path, old_src) if old_src else {"symbols": []}
                new_ex = extract(path, new_src) if new_src else {"symbols": []}
            except Exception as e:
                warnings.append(f"parse failed for {path}: {e}")
                chunks.append(self._window_chunk(f, "parse_error"))
                continue

            old_c, new_c = changed_lines(_decode(old_src), _decode(new_src))
            changes = classify(old_ex["symbols"], new_ex["symbols"], old_path, path, old_c, new_c)

            if not changes and (old_c or new_c):
                # module-level code (imports, constants, top-level statements)
                chunks.append(self._window_chunk(f, "module_level_change"))

            for ch in changes:
                chunks.append(self._build_chunk(ch, old_src, new_src, ctx))

        result = {
            "pr_number": pr["number"],
            "base_sha": base,
            "head_sha": head,
            "index": index_info,
            "excluded_files": excluded,
            "chunks": chunks,
            "cross_file_links": self._links(chunks),
            "warnings": warnings,
        }
        if self.repo_rules is not None:
            result["repo_rules"] = self.repo_rules
        return result

    # ---------------------------------------------------------------- chunk
    def _build_chunk(self, ch: dict, old_src: bytes | None, new_src: bytes | None, ctx: dict) -> dict:
        sym, old, ct = ch["symbol"], ch["old"], ch["change_type"]

        if ct == "added":
            depth = 0
        elif ct in ("signature_changed", "deleted", "visibility_changed"):
            depth = 2
        else:
            depth = 1

        rows = self.store.callers(ctx["base"], old["symbol_id"], depth) if depth and old else []
        if ct == "body_only" and not sym["exported"]:
            rows = [r for r in rows if r["file"] == old["file"]]

        callers = [self._eval_caller(r, sym, ct, ctx) for r in rows]
        direct = [c for c in callers if c["level"] == 1]
        broken = [c for c in direct if c["status"] in ("incompatible_call", "broken_symbol_deleted")]
        unresolved = self.store.unresolved_count(ctx["base"], sym["short_name"]) if depth else 0

        if broken:
            blast = "high"
        elif ct in ("signature_changed", "deleted") and direct:
            blast = "medium"
        elif len(callers) > 5:
            blast = "medium"
        else:
            blast = "low" if callers else "none"

        src_lines = _decode(old_src if ct == "deleted" else new_src).splitlines()
        code = src_lines[sym["start_line"] - 1 : sym["end_line"]]
        truncated = len(code) > MAX_CODE_LINES
        old_code = _decode(old_src).splitlines()[old["start_line"] - 1 : old["end_line"]] if old else []

        return {
            "chunk_type": "symbol",
            "chunk_id": sym["symbol_id"],
            "file": sym["file"],
            "language": language_for(sym["file"]),
            "symbol": sym["name"],
            "kind": sym["kind"],
            "scope_breadcrumb": f"{sym['file']} > " + " > ".join(sym["name"].split(".")),
            "visibility": "exported" if sym["exported"] else "private",
            "change_type": ct,
            "old_signature": old["signature"] if old else None,
            "new_signature": None if ct == "deleted" else sym["signature"],
            "lines": [sym["start_line"], sym["end_line"]],
            "code": "\n".join(code[:MAX_CODE_LINES]) + ("\n... (truncated)" if truncated else ""),
            "symbol_diff": "\n".join(difflib.unified_diff(old_code, code, "before", "after", lineterm="")),
            "impact": {
                "has_external_impact": bool(callers),
                "direct_callers": direct,
                "indirect_callers": [c for c in callers if c["level"] > 1][:20],
                "broken_callers_count": len(broken),
                "unupdated_callers_count": sum(not c["updated_in_pr"] for c in direct),
                "affected_tests": sorted({c["file"] for c in callers if is_test_file(c["file"])}),
                "blast_radius": blast,
                "possible_unresolved_references": unresolved,
                "confidence": "high" if unresolved == 0 else "medium",
            },
        }

    def _eval_caller(self, row: dict, sym: dict, ct: str, ctx: dict) -> dict:
        orig_file = row["file"]
        head_file = ctx["renamed"].get(orig_file, orig_file)
        caller_sym_name = row["from_symbol"].split("#", 1)[1]

        c = {
            "symbol": caller_sym_name,
            "file": head_file,
            "line": row["line"],
            "level": row["level"],
            "updated_in_pr": orig_file in ctx["pr_paths"] or head_file in ctx["pr_paths"],
            "status": "ok",
        }

        if row["level"] > 1:
            c["status"] = "indirect"
            return c

        if orig_file in ctx["removed"]:
            c["status"] = "caller_file_deleted"
            return c

        if c["updated_in_pr"]:
            # Caller file changed/renamed in PR -> inspect the call site at head
            ex = self._head_extract(head_file, ctx)
            if ex is None:
                c["status"] = "caller_parse_error"
                c["snippet"] = self._line(head_file, c["line"], ctx)
                return c

            expected_from = f"{head_file}#{caller_sym_name}"
            calls = [
                r
                for r in ex.get("refs", [])
                if r["from_symbol"] == expected_from and r["to_name"] == sym["short_name"]
            ]
            if not calls:
                c["status"] = "call_removed_in_pr"
                return c

            target_call = calls[0]
            if ct == "deleted":
                c["line"] = target_call["line"]
                c["status"] = "broken_symbol_deleted"
            elif ct == "signature_changed":
                lo, hi = sym["min_args"], sym["max_args"]
                bad_calls = [
                    r
                    for r in calls
                    if r["arg_count"] is not None
                    and (r["arg_count"] < lo or (hi is not None and r["arg_count"] > hi))
                ]
                unknown_calls = [r for r in calls if r["arg_count"] is None]
                if bad_calls:
                    target_call = bad_calls[0]
                    c["line"] = target_call["line"]
                    c["status"] = "incompatible_call"
                    c["detail"] = (
                        f"called with {target_call['arg_count']} args, "
                        f"new signature accepts {lo}..{hi if hi is not None else 'n'}"
                    )
                elif unknown_calls:
                    target_call = unknown_calls[0]
                    c["line"] = target_call["line"]
                    c["status"] = "unknown_arg_count"
                else:
                    c["line"] = target_call["line"]
            else:
                c["line"] = target_call["line"]
        else:
            if ct == "deleted":
                c["status"] = "broken_symbol_deleted"
            elif ct == "signature_changed":
                lo, hi = sym["min_args"], sym["max_args"]
                arg_count = row["arg_count"]
                if arg_count is not None and (arg_count < lo or (hi is not None and arg_count > hi)):
                    c["status"] = "incompatible_call"
                    c["detail"] = (
                        f"called with {arg_count} args, "
                        f"new signature accepts {lo}..{hi if hi is not None else 'n'}"
                    )
                elif arg_count is None:
                    c["status"] = "unknown_arg_count"

        c["snippet"] = self._line(head_file, c["line"], ctx)
        return c

    # ---------------------------------------------------------------- helpers
    def _head_text(self, path: str, ctx: dict) -> str:
        if path not in ctx["text_cache"]:
            ctx["text_cache"][path] = _decode(self.mirror.read(ctx["head"], path))
        return ctx["text_cache"][path]

    def _head_extract(self, path: str, ctx: dict) -> dict | None:
        if path not in ctx["head_cache"]:
            if not language_for(path):
                ctx["head_cache"][path] = None
                return None
            src = self._head_text(path, ctx).encode("utf-8")
            if not src:
                ctx["head_cache"][path] = None
                return None
            try:
                ctx["head_cache"][path] = extract(path, src)
            except Exception as e:
                ctx["warnings"].append(f"parse failed for caller {path}: {e}")
                ctx["head_cache"][path] = None
        return ctx["head_cache"][path]

    def _line(self, path: str, line: int, ctx: dict) -> str:
        lines = self._head_text(path, ctx).splitlines()
        return lines[line - 1].strip() if 0 < line <= len(lines) else ""

    @staticmethod
    def _window_chunk(f: dict, reason: str) -> dict:
        return {
            "chunk_type": "file_window",
            "chunk_id": f"{f['filename']}#<file>",
            "file": f["filename"],
            "change_type": f["status"],
            "reason": reason,
            "patch": f.get("patch"),
        }

    @staticmethod
    def _links(chunks: list[dict]) -> list[dict]:
        ids = {c["chunk_id"] for c in chunks}
        links = []
        for c in chunks:
            for caller in c.get("impact", {}).get("direct_callers", []):
                cid = f"{caller['file']}#{caller['symbol']}"
                if cid in ids:
                    links.append({
                        "from": cid,
                        "to": c["chunk_id"],
                        "relation": "calls, both changed in this PR",
                    })
        return links