import re


FALLBACK_SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "generic_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)"
            r"\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{12,})"
        ),
    ),
]


def fallback_regex_secrets(context_bundle: dict) -> list[dict]:
    """Find common secret patterns in added lines of the PR diff."""
    findings = []
    for chunk in context_bundle.get("chunks", []):
        diff_text = chunk.get("symbol_diff") or chunk.get("patch") or ""
        current_line = chunk.get("lines", [1, 1])[0]
        print(current_line, diff_text)

        for raw_line in diff_text.splitlines():
            if raw_line.startswith("@@"):
                match = re.search(r"\+(\d+)", raw_line)
                if match:
                    current_line = int(match.group(1))
                continue

            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                added = raw_line[1:]
                print("added", added)
                for rule_name, pattern in FALLBACK_SECRET_PATTERNS:
                    if pattern.search(added):
                        findings.append(
                            {
                                "agent": "static_checks",
                                "file": chunk.get("file", "unknown"),
                                "line": current_line,
                                "symbol": chunk.get("symbol"),
                                "severity": "critical",
                                "confidence": "high",
                                "category": "secret_exposure",
                                "rule_id": f"secret/{rule_name}",
                                "title": f"Potential leaked secret ({rule_name})",
                                "message": "A secret or credential pattern was added in this diff.",
                                "suggestion": "Move this value to environment variables or a secret manager.",
                            }
                        )
                current_line += 1
            elif not raw_line.startswith("-"):
                current_line += 1

    return findings
