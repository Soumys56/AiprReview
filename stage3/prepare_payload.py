import json
from pathlib import Path

MAX_PAYLOAD_CODE_LINES = 90
SEVERITY_ORDER = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}


def _norm_path(path: str) -> str:
    clean = Path(path.strip()).as_posix()
    return clean[2:] if clean.startswith("./") else clean


def normalise_and_dedupe_static(findings: list[dict]) -> list[dict]:
    """Collapse duplicate static findings before passing them to reviewers."""
    seen_keys: set[tuple] = set()
    seen_format_files: set[tuple[str, str]] = set()
    cleaned: list[dict] = []

    for raw in findings:
        file_path = _norm_path(raw.get("file", ""))
        if not file_path:
            continue

        line = max(1, int(raw.get("line") or 1))
        rule_id = raw.get("rule_id") or ""
        category = str(raw.get("category") or "lint").lower()
        severity = str(raw.get("severity") or "medium").lower()
        if severity not in SEVERITY_ORDER:
            severity = "medium"

        if category == "formatting" or rule_id in ("prettier/format", "ruff/format"):
            format_key = (file_path, rule_id or "format")
            if format_key in seen_format_files:
                continue
            seen_format_files.add(format_key)
            line = 1
            severity = "low"

        dedupe_key = (file_path, line, rule_id or category)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        cleaned.append({
            **raw,
            "file": file_path,
            "line": line,
            "severity": severity,
            "category": category,
        })

    cleaned.sort(
        key=lambda finding: (
            -SEVERITY_ORDER.get(finding["severity"], 0),
            finding["file"],
            finding["line"],
        )
    )
    return cleaned


def attach_findings_to_chunks(
    context_bundle: dict, static_findings: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Attach static findings to matching changed chunks when possible."""
    chunks = [{**chunk, "static_findings": []} for chunk in context_bundle.get("chunks", [])]
    unattached: list[dict] = []

    for finding in static_findings:
        file_path = finding["file"]
        line = finding["line"]
        matched_chunk = None

        for chunk in chunks:
            if chunk.get("file") != file_path:
                continue
            if chunk.get("chunk_type") == "symbol":
                start, end = chunk.get("lines", [1, 1])
                if start <= line <= end:
                    matched_chunk = chunk
                    break
            elif matched_chunk is None:
                matched_chunk = chunk

        if matched_chunk is None:
            for chunk in chunks:
                if chunk.get("chunk_type") != "symbol":
                    continue
                for caller in chunk.get("impact", {}).get("direct_callers", []):
                    if caller.get("file") == file_path and abs(int(caller.get("line", 0)) - line) <= 3:
                        matched_chunk = chunk
                        break
                if matched_chunk is not None:
                    break

        if matched_chunk is not None:
            matched_chunk["static_findings"].append({
                "rule_id": finding.get("rule_id"),
                "severity": finding["severity"],
                "file": file_path,
                "line": line,
                "title": finding.get("title", "Static finding"),
            })
        else:
            unattached.append(finding)

    return chunks, unattached


def _cap_lines(text: str | None, max_lines: int = MAX_PAYLOAD_CODE_LINES) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} lines omitted)"


def build_agent_payload(
    context_bundle: dict,
    static_findings: list[dict],
    user_story_and_bug_requirements: str = "",
) -> str:
    """Normalize findings and return a compact JSON payload for review agents."""
    cleaned_static = normalise_and_dedupe_static(static_findings)
    enriched_chunks, unattached = attach_findings_to_chunks(context_bundle, cleaned_static)

    trimmed_chunks = []
    for chunk in enriched_chunks:
        if chunk.get("chunk_type") == "symbol":
            impact = chunk.get("impact", {})
            trimmed_chunks.append({
                "file": chunk["file"],
                "symbol": chunk["symbol"],
                "kind": chunk["kind"],
                "visibility": chunk["visibility"],
                "change_type": chunk["change_type"],
                "old_signature": chunk.get("old_signature"),
                "new_signature": chunk.get("new_signature"),
                "lines": chunk.get("lines", [1, 1]),
                "code": _cap_lines(chunk.get("code")),
                "impact": {
                    "blast_radius": impact.get("blast_radius"),
                    "broken_callers_count": impact.get("broken_callers_count"),
                    "unupdated_callers_count": impact.get("unupdated_callers_count"),
                    "affected_tests": impact.get("affected_tests", []),
                    "direct_callers": impact.get("direct_callers", [])[:15],
                },
                "already_flagged_by_static_tools": chunk["static_findings"],
            })
        else:
            trimmed_chunks.append({
                "file": chunk.get("file", "unknown"),
                "chunk_type": chunk.get("chunk_type"),
                "change_type": chunk.get("change_type"),
                "reason": chunk.get("reason"),
                "patch": _cap_lines(chunk.get("patch"), max_lines=60),
                "already_flagged_by_static_tools": chunk["static_findings"],
            })

    payload = {
        "pr_number": context_bundle.get("pr_number"),
        "pr_title": context_bundle.get("pr_title") or context_bundle.get("title"),
        "pr_description": context_bundle.get("pr_description") or context_bundle.get("description"),
        "project_areas": context_bundle.get("project_areas", []),
        "user_story_and_bug_requirements": user_story_and_bug_requirements,
        "repo_conventions": context_bundle.get("repo_rules") or [],
        "cross_file_links": context_bundle.get("cross_file_links", []),
        "chunks": trimmed_chunks,
        "repo_level_static_findings": [
            {
                "file": finding["file"],
                "line": finding["line"],
                "rule_id": finding.get("rule_id"),
                "title": finding.get("title"),
            }
            for finding in unattached[:15]
        ],
    }
    return json.dumps(payload, indent=2)
