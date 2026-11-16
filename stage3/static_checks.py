import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from service.symbol import is_excluded
from stage3.schema import ReviewFinding

TOOL_TIMEOUT_SEC = 90
PY_EXTS = (".py", ".pyi")
JS_TS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
PRETTIER_EXTS = (*JS_TS_EXTS, ".html", ".htm", ".json", ".css", ".scss", ".md", ".yaml", ".yml")
PROJECT_AREA_EXTENSIONS = {
    "User frontend": (".html", ".htm", *JS_TS_EXTS, ".css", ".scss", ".json"),
    "Moderator frontend": (".html", ".htm", *JS_TS_EXTS, ".css", ".scss", ".json"),
    "User backend": JS_TS_EXTS,
    "Moderator backend": JS_TS_EXTS,
    "Chatbot": (*PY_EXTS, *JS_TS_EXTS, ".html", ".htm"),
    "AI PR Review (Python)": PY_EXTS,
}

ESLINT_CONFIG_NAMES = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
)

TSC_LINE_RE = re.compile(r"^(.+?)\((\d+),(\d+)\):\s+(error|warning)\s+(TS\d+):\s+(.+)$")

FALLBACK_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "hardcoded-secret",
        re.compile(
            r"""(?i)\b(?:api_key|secret_key|password|passwd|access_token)\b\s*=\s*['"][^'"]{12,}['"]"""
        ),
    ),
]


def _redact(secret: str | None) -> str:
    if not secret:
        return "***"
    return f"{secret[:4]}***" if len(secret) > 6 else "***"


async def _exec_cmd(
    cmd: list[str], cwd: Path, timeout: float = TOOL_TIMEOUT_SEC
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", f"Timed out after {timeout}s"

    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _changed_files_from_bundle(context_bundle: dict) -> list[dict]:
    by_path: dict[str, dict] = {}
    for ch in context_bundle.get("chunks", []):
        path = ch.get("file")
        if path and path not in by_path:
            by_path[path] = {"file": path, "change_type": ch.get("change_type", "modified")}
    return list(by_path.values())


def _tool_path(worktree_dir: Path, tool: str) -> str | None:
    """Resolve globally installed or worktree-local command line tools."""
    found = shutil.which(tool)
    if found:
        return found
    names = [tool]
    if os.name == "nt":
        names = [f"{tool}.exe", f"{tool}.cmd", tool]
    candidates = [
        worktree_dir / ".venv" / "bin" / name for name in names
    ] + [
        worktree_dir / ".venv" / "Scripts" / name for name in names
    ] + [
        worktree_dir / "node_modules" / ".bin" / name for name in names
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


# ---------------------------------------------------------------------------
# 0. Stage 2 Call-Graph Breakages
# ---------------------------------------------------------------------------
def _check_broken_callers(context_bundle: dict) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for ch in context_bundle.get("chunks", []):
        if ch.get("chunk_type") != "symbol":
            continue
        for caller in ch.get("impact", {}).get("direct_callers", []):
            status = caller.get("status")
            if status in ("incompatible_call", "broken_symbol_deleted"):
                detail = caller.get("detail") or f"Target `{ch['symbol']}` was deleted or changed."
                findings.append(
                    ReviewFinding(
                        agent="static_checks",
                        file=caller["file"],
                        line=caller["line"],
                        symbol=caller["symbol"],
                        severity="high",
                        confidence="high",
                        category="breaking_change",
                        rule_id=f"callgraph/{status}",
                        title=f"Broken call to `{ch['symbol']}`",
                        message=f"`{caller['symbol']}` calls `{ch['symbol']}` at line {caller['line']}: {detail}",
                        suggestion="Update the call site to match the new signature or remove the stale call.",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# 1. WHOLE WORKTREE: mypy + tsc --noEmit
# ---------------------------------------------------------------------------
async def _run_mypy(worktree_dir: Path, has_py: bool) -> list[ReviewFinding]:
    mypy = _tool_path(worktree_dir, "mypy")
    if not has_py or not mypy:
        return []

    cmd = [mypy, "--output=json", "--no-error-summary"]
    venv_python = worktree_dir / ".venv" / "bin" / "python"
    if venv_python.exists():
        cmd.extend(["--python-executable", str(venv_python)])
    else:
        cmd.append("--ignore-missing-imports")
    cmd.append(".")

    _, stdout, _ = await _exec_cmd(cmd, cwd=worktree_dir)
    findings: list[ReviewFinding] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("severity") != "error":
            continue
        code = item.get("code") or "error"
        findings.append(
            ReviewFinding(
                agent="static_checks",
                file=Path(item.get("file", "")).as_posix(),
                line=int(item.get("line") or 1),
                symbol=None,
                severity="high",
                confidence="high",
                category="type_error",
                rule_id=f"mypy/{code}",
                title=f"mypy [{code}]: {item.get('message', '')}",
                message=item.get("message", ""),
            )
        )
    return findings


async def _run_tsc(worktree_dir: Path, js_ts_files: list[str]) -> list[ReviewFinding]:
    tsc = _tool_path(worktree_dir, "tsc")
    if not js_ts_files or not tsc:
        return []

    if (worktree_dir / "tsconfig.json").exists():
        cmd = [tsc, "--noEmit", "--pretty", "false", "--skipLibCheck"]
    else:
        cmd = [
            tsc, "--noEmit", "--pretty", "false", "--skipLibCheck",
            "--allowJs", "--checkJs", "--target", "ES2022",
            "--moduleResolution", "bundler", *js_ts_files,
        ]

    _, stdout, _ = await _exec_cmd(cmd, cwd=worktree_dir)
    findings: list[ReviewFinding] = []
    for line in stdout.splitlines():
        m = TSC_LINE_RE.match(line.strip())
        if not m:
            continue
        rel_file, row, _, level, code, msg = m.groups()
        findings.append(
            ReviewFinding(
                agent="static_checks",
                file=Path(rel_file).as_posix(),
                line=int(row),
                symbol=None,
                severity="high" if level == "error" else "medium",
                confidence="high",
                category="type_error",
                rule_id=f"tsc/{code}",
                title=f"TypeScript {code}: {msg}",
                message=msg,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 2. CHANGED FILES INSIDE WORKTREE: Pylint, ESLint, Prettier, Bandit, Semgrep
# ---------------------------------------------------------------------------
async def _run_pylint(worktree_dir: Path, py_files: list[str]) -> list[ReviewFinding]:
    pylint = _tool_path(worktree_dir, "pylint")
    if not py_files or not pylint:
        return []

    _, stdout, _ = await _exec_cmd(
        [pylint, "--output-format=json", "--reports=no", *py_files],
        cwd=worktree_dir,
    )
    try:
        messages = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return []

    severity_map = {
        "fatal": "critical",
        "error": "high",
        "warning": "medium",
        "refactor": "low",
        "convention": "low",
        "information": "info",
        "info": "info",
    }
    findings: list[ReviewFinding] = []
    for issue in messages:
        path = Path(issue.get("path", ""))
        try:
            rel_file = path.relative_to(worktree_dir.resolve()).as_posix() if path.is_absolute() else path.as_posix()
        except ValueError:
            rel_file = path.as_posix()
        symbol = issue.get("obj") or None
        message_id = issue.get("message-id") or "unknown"
        findings.append(ReviewFinding(
            agent="static_checks",
            file=rel_file,
            line=max(1, int(issue.get("line") or 1)),
            symbol=symbol,
            severity=severity_map.get(str(issue.get("type", "warning")).lower(), "medium"),
            confidence="high",
            category="lint",
            rule_id=f"pylint/{message_id}",
            title=f"Pylint {message_id}: {issue.get('message', '')}",
            message=issue.get("message", ""),
        ))
    return findings


def _fallback_eslint_config() -> str | None:
    """Create a temp ESLint flat config outside `worktree_dir` so the worktree stays clean."""
    try:
        global_root = subprocess.check_output(["npm", "root", "-g"], text=True, timeout=5).strip()
    except Exception:
        return None
    if not global_root or not Path(global_root).exists():
        return None

    cfg_path = Path(tempfile.gettempdir()) / "aipr_fallback_eslint.config.mjs"
    if not cfg_path.exists():
        pkg_anchor = (Path(global_root) / "package.json").as_posix()
        cfg_path.write_text(
            f"""import {{ createRequire }} from "module";
const require = createRequire({json.dumps(pkg_anchor)});
const js = require("@eslint/js");
const tseslint = require("typescript-eslint");
export default [js.configs.recommended, ...tseslint.configs.recommended];
""",
            encoding="utf-8",
        )
    return str(cfg_path)


async def _run_eslint(worktree_dir: Path, js_files: list[str]) -> list[ReviewFinding]:
    eslint = _tool_path(worktree_dir, "eslint")
    if not js_files or not eslint:
        return []

    cmd = [eslint, "--format=json"]
    if not any((worktree_dir / name).exists() for name in ESLINT_CONFIG_NAMES):
        fallback_cfg = _fallback_eslint_config()
        if fallback_cfg:
            cmd.extend(["--config", fallback_cfg])
    cmd.extend(js_files)

    _, stdout, _ = await _exec_cmd(cmd, cwd=worktree_dir)
    if not stdout.strip():
        return []

    findings: list[ReviewFinding] = []
    try:
        reports = json.loads(stdout)
    except json.JSONDecodeError:
        return []

    for rep in reports:
        rel_file = Path(rep["filePath"]).relative_to(worktree_dir.resolve()).as_posix()
        for msg in rep.get("messages", []):
            rule = msg.get("ruleId") or "eslint"
            findings.append(
                ReviewFinding(
                    agent="static_checks",
                    file=rel_file,
                    line=int(msg.get("line") or 1),
                    symbol=None,
                    severity="high" if msg.get("severity") == 2 else "medium",
                    confidence="high",
                    category="lint",
                    rule_id=f"eslint/{rule}",
                    title=f"ESLint ({rule}): {msg.get('message', '')}",
                    message=msg.get("message", ""),
                )
            )
    return findings


async def _run_prettier(worktree_dir: Path, target_files: list[str]) -> list[ReviewFinding]:
    prettier = _tool_path(worktree_dir, "prettier")
    if not target_files or not prettier:
        return []

    rc, stdout, _ = await _exec_cmd(
        [prettier, "--list-different", *target_files], cwd=worktree_dir
    )
    if rc == 0 or not stdout.strip():
        return []

    return [
        ReviewFinding(
            agent="static_checks",
            file=line.strip(),
            line=1,
            symbol=None,
            severity="low",
            confidence="high",
            category="formatting",
            rule_id="prettier/format",
            title="File is not formatted with Prettier",
            message=f"`{line.strip()}` has formatting differences according to Prettier.",
            suggestion=f"Run `prettier --write {line.strip()}`.",
        )
        for line in stdout.splitlines()
        if line.strip() and (worktree_dir / line.strip()).exists()
    ]


async def _run_bandit(worktree_dir: Path, py_files: list[str]) -> list[ReviewFinding]:
    bandit = _tool_path(worktree_dir, "bandit")
    if not py_files or not bandit:
        return []

    _, stdout, _ = await _exec_cmd([bandit, "-f", "json", "-q", *py_files], cwd=worktree_dir)
    if not stdout.strip():
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []

    sev_map = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
    return [
        ReviewFinding(
            agent="static_checks",
            file=item.get("filename", "").lstrip("./"),
            line=int(item.get("line_number") or 1),
            symbol=None,
            severity=sev_map.get(item.get("issue_severity", "MEDIUM"), "medium"),
            confidence=sev_map.get(item.get("issue_confidence", "MEDIUM"), "medium"),
            category="sast",
            rule_id=f"bandit/{item.get('test_id', 'B000')}",
            title=f"Bandit {item.get('test_id', 'B000')}: {item.get('test_name', 'Security issue')}",
            message=item.get("issue_text", ""),
            suggestion=item.get("more_info"),
        )
        for item in data.get("results", [])
    ]


async def _run_semgrep(worktree_dir: Path, changed_files: list[str]) -> list[ReviewFinding]:
    semgrep = _tool_path(worktree_dir, "semgrep")
    if not changed_files or not semgrep:
        return []

    _, stdout, _ = await _exec_cmd(
        [semgrep, "scan", "--config", "auto", "--json", "--quiet", "--metrics=off", *changed_files],
        cwd=worktree_dir,
    )
    if not stdout.strip():
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []

    sev_map = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
    findings: list[ReviewFinding] = []
    for item in data.get("results", []):
        extra = item.get("extra", {})
        check_id = item.get("check_id", "semgrep")
        findings.append(
            ReviewFinding(
                agent="static_checks",
                file=item.get("path", ""),
                line=int(item.get("start", {}).get("line") or 1),
                symbol=None,
                severity=sev_map.get(extra.get("severity", "WARNING"), "medium"),
                confidence="high",
                category="sast",
                rule_id=f"semgrep/{check_id}",
                title=f"Semgrep: {check_id.split('.')[-1]}",
                message=extra.get("message", ""),
                suggestion=extra.get("fix"),
            )
        )
    return findings


async def _run_pytest_suite(worktree_dir: Path, test_files: list[str]) -> dict:
    """Run the PR's existing pytest suite and convert failed test cases to bugs."""
    if not test_files:
        return {"status": "skipped (no Python tests found)", "findings": []}

    venv_python = worktree_dir / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    command = [python, "-m", "pytest", "-q", "--disable-warnings", "--maxfail=25"]
    rc, stdout, stderr = await _exec_cmd(command, cwd=worktree_dir, timeout=180)
    output = stdout + "\n" + stderr
    if "No module named pytest" in output:
        return {"status": "skipped (pytest is not installed in the PR environment)", "findings": []}
    if rc == -1:
        return {"status": "timed out after 180 seconds", "findings": []}
    if rc == 0:
        summary_match = re.search(r"(\d+ passed(?:, \d+ [\w ]+)? in [\d.]+s)", output)
        summary = summary_match.group(1) if summary_match else "all discovered tests passed"
        return {"status": f"passed ({summary})", "findings": []}
    if rc == 5:
        return {"status": "skipped (pytest collected no tests)", "findings": []}

    failure_lines = re.findall(r"^FAILED\s+(.+?)(?:\s+-\s+.*)?$", output, flags=re.MULTILINE)
    findings: list[ReviewFinding] = []
    for failure in failure_lines:
        test_id, _, short_reason = failure.partition(" - ")
        file_path = test_id.split("::", 1)[0].strip()
        if not file_path:
            file_path = test_files[0]
        test_name = test_id.split("::")[-1].split("[")[0]
        failure_detail = ""
        for block in re.split(r"(?m)(?=^_{3,} )", output):
            first_line = block.splitlines()[0] if block.splitlines() else ""
            if test_name and test_name in first_line:
                failure_detail = block.split("short test summary info", 1)[0].strip()
                break
        message = "\n".join(part for part in (short_reason, failure_detail[-2400:]) if part)
        line_match = re.search(r"(?:^|\s)([^\s:]+\.py):(\d+):", failure_detail)
        test_line = int(line_match.group(2)) if line_match else 1
        findings.append(ReviewFinding(
            agent="pytest",
            file=file_path,
            line=test_line,
            symbol=test_name or None,
            severity="high",
            confidence="medium",
            category="test_failure",
            rule_id="pytest/failed-test",
            title=f"Bug: PR test failed — {test_id}",
            message=message or "An existing PR test failed, but pytest did not provide a concise assertion message.",
            root_cause="The existing pytest assertion or expected behavior did not match the PR behavior. See the failure detail above.",
            suggestion="Fix the behavior causing the test failure and rerun the PR test suite.",
        ))
    if not findings:
        findings.append(ReviewFinding(
            agent="pytest",
            file=test_files[0],
            line=1,
            symbol=None,
            severity="medium",
            confidence="medium",
            category="test_environment_error",
            rule_id="pytest/suite-error",
            title="Pytest collection or setup error",
            message=(output.strip() or f"pytest exited with status {rc}")[-3000:],
            root_cause="Pytest exited before reporting individual failed tests, so this output alone does not establish a product bug.",
            suggestion="Fix the test collection or environment setup, then rerun the suite to check for product regressions.",
        ))
    return {"status": f"failed ({len(findings)} failing test(s))", "findings": findings}


# ---------------------------------------------------------------------------
# 3. DIFF ONLY: Secret Scan (gitleaks / trufflehog / regex fallback)
# ---------------------------------------------------------------------------
def _fallback_regex_secrets(context_bundle: dict) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for ch in context_bundle.get("chunks", []):
        diff_text = ch.get("symbol_diff") or ch.get("patch") or ""
        current_line = ch.get("lines", [1, 1])[0]
        for raw_line in diff_text.splitlines():
            if raw_line.startswith("@@"):
                m = re.search(r"\+(\d+)", raw_line)
                if m:
                    current_line = int(m.group(1))
                continue
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                added = raw_line[1:]
                for rule_name, pattern in FALLBACK_SECRET_PATTERNS:
                    if pattern.search(added):
                        findings.append(
                            ReviewFinding(
                                agent="static_checks",
                                file=ch["file"],
                                line=current_line,
                                symbol=ch.get("symbol"),
                                severity="critical",
                                confidence="high",
                                category="secret_exposure",
                                rule_id=f"secret/{rule_name}",
                                title=f"Potential leaked secret ({rule_name})",
                                message="A secret or credential pattern was added in this diff.",
                                suggestion="Move this value to environment variables or a secret manager.",
                            )
                        )
                current_line += 1
            elif not raw_line.startswith("-"):
                current_line += 1
    return findings


async def _run_diff_secret_scan(context_bundle: dict, mirror) -> list[ReviewFinding]:
    changed_paths = [f["file"] for f in _changed_files_from_bundle(context_bundle)]
    if not changed_paths:
        return []

    has_gitleaks = bool(shutil.which("gitleaks"))
    has_trufflehog = bool(shutil.which("trufflehog"))
    if mirror is None or (not has_gitleaks and not has_trufflehog):
        return _fallback_regex_secrets(context_bundle)

    def _build_two_commit_repo(repo_dir: Path) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "review-bot",
            "GIT_AUTHOR_EMAIL": "bot@local",
            "GIT_COMMITTER_NAME": "review-bot",
            "GIT_COMMITTER_EMAIL": "bot@local",
        }
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, env=env, check=True)
        for sha, msg in [(context_bundle["base_sha"], "base"), (context_bundle["head_sha"], "head")]:
            for child in list(repo_dir.iterdir()):
                if child.name != ".git":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
            for rel in changed_paths:
                if is_excluded(rel):
                    continue
                raw = mirror.read(sha, rel)
                if raw and b"\0" not in raw[:8000]:
                    dest = repo_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(raw)
            subprocess.run(["git", "add", "-A"], cwd=repo_dir, env=env, check=True)
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", msg], cwd=repo_dir, env=env, check=True)

    with tempfile.TemporaryDirectory(prefix="pr_secrets_") as tmp_dir:
        repo_dir = Path(tmp_dir)
        await asyncio.to_thread(_build_two_commit_repo, repo_dir)

        if has_gitleaks:
            report_path = repo_dir / ".gitleaks-report.json"
            await _exec_cmd(
                [
                    "gitleaks", "detect", "--source", ".",
                    "--log-opts=HEAD~1..HEAD", "--report-format", "json",
                    "--report-path", str(report_path), "--no-banner", "--exit-code", "0",
                ],
                cwd=repo_dir,
            )
            if not report_path.exists():
                return []
            leaks = json.loads(report_path.read_text("utf-8") or "[]")
            return [
                ReviewFinding(
                    agent="static_checks",
                    file=leak.get("File", ""),
                    line=int(leak.get("StartLine") or 1),
                    symbol=None,
                    severity="critical",
                    confidence="high",
                    category="secret_exposure",
                    rule_id=f"gitleaks/{leak.get('RuleID', 'secret')}",
                    title=f"Leaked secret ({leak.get('Description', leak.get('RuleID', 'secret'))})",
                    message=f"Matched `{_redact(leak.get('Secret'))}` in PR diff.",
                    suggestion="Remove the secret, rotate it immediately, and load it from env/vault.",
                )
                for leak in leaks
            ]

        _, stdout, _ = await _exec_cmd(
            ["trufflehog", "git", f"file://{repo_dir}", "--since-commit", "HEAD~1", "--json", "--no-update"],
            cwd=repo_dir,
        )
        findings: list[ReviewFinding] = []
        for line in stdout.splitlines():
            if not line.strip().startswith("{"):
                continue
            item = json.loads(line)
            git_meta = item.get("SourceMetadata", {}).get("Data", {}).get("Git", {})
            detector = item.get("DetectorName", "secret")
            findings.append(
                ReviewFinding(
                    agent="static_checks",
                    file=git_meta.get("file", ""),
                    line=int(git_meta.get("line") or 1),
                    symbol=None,
                    severity="critical",
                    confidence="high" if item.get("Verified") else "medium",
                    category="secret_exposure",
                    rule_id=f"trufflehog/{detector}",
                    title=f"Leaked {detector} credential",
                    message=f"TruffleHog flagged `{_redact(item.get('Raw'))}` in PR diff.",
                    suggestion="Revoke/rotate this credential immediately.",
                )
            )
        return findings


# ---------------------------------------------------------------------------
# Entrypoint called by Stage3Pipeline
# ---------------------------------------------------------------------------
async def run_static_checks(
    context_bundle: dict, mirror=None, worktree_dir: Path | None = None
) -> list[ReviewFinding]:
    """Run deterministic checks and return their findings as one list."""
    report = await run_static_checks_by_check(context_bundle, mirror, worktree_dir)
    return [finding for result in report.values() for finding in result["findings"]]


async def run_static_checks_by_check(
    context_bundle: dict,
    mirror=None,
    worktree_dir: Path | None = None,
    project_areas: list[str] | None = None,
) -> dict[str, dict]:
    """Run checks separately and retain each check's status and findings."""
    if project_areas:
        allowed_extensions = {
            extension
            for area in project_areas
            for extension in PROJECT_AREA_EXTENSIONS.get(area, ())
        }
        context_bundle = {
            **context_bundle,
            "chunks": [
                chunk for chunk in context_bundle.get("chunks", [])
                if Path(chunk.get("file", "")).suffix.lower() in allowed_extensions
            ],
        }
    else:
        allowed_extensions = None

    report = {
        "Call graph impact": {"status": "completed", "findings": _check_broken_callers(context_bundle)},
        "Python type check (mypy)": {"status": "skipped (worktree unavailable)", "findings": []},
        "TypeScript check (tsc)": {"status": "skipped (worktree unavailable)", "findings": []},
        "Python lint (Pylint)": {"status": "skipped (worktree unavailable)", "findings": []},
        "JavaScript lint (ESLint)": {"status": "skipped (worktree unavailable)", "findings": []},
        "Formatting (Prettier)": {"status": "skipped (worktree unavailable)", "findings": []},
        "Python security (Bandit)": {"status": "skipped (worktree unavailable)", "findings": []},
        "Security scan (Semgrep)": {"status": "skipped (worktree unavailable)", "findings": []},
        "Secret scan": {"status": "skipped (worktree unavailable)", "findings": []},
        "PR pytest suite": {"status": "skipped (worktree unavailable)", "findings": []},
    }
    if worktree_dir is None or not worktree_dir.exists():
        report["Secret scan"] = {
            "status": "completed (regex fallback)",
            "findings": _fallback_regex_secrets(context_bundle),
        }
        return report

    active_paths = [
        f["file"]
        for f in _changed_files_from_bundle(context_bundle)
        if f["change_type"] != "removed" and (worktree_dir / f["file"]).exists()
    ]
    all_repo_files = (
        await asyncio.to_thread(mirror.list_files, context_bundle["head_sha"])
        if mirror is not None
        else active_paths
    )
    if project_areas:
        all_repo_files = [
            path for path in all_repo_files
            if Path(path).suffix.lower() in allowed_extensions
        ]
    py_changed = [f for f in active_paths if Path(f).suffix.lower() in PY_EXTS]
    js_changed = [f for f in active_paths if Path(f).suffix.lower() in JS_TS_EXTS]
    fmt_changed = [f for f in active_paths if Path(f).suffix.lower() in PRETTIER_EXTS]
    has_py = any(Path(f).suffix.lower() in PY_EXTS and not is_excluded(f) for f in all_repo_files)
    all_js_ts = [f for f in all_repo_files if Path(f).suffix.lower() in JS_TS_EXTS and not is_excluded(f)]
    pytest_files = [
        path for path in all_repo_files
        if Path(path).suffix.lower() in PY_EXTS
        and not is_excluded(path)
        and (
            "/tests/" in f"/{path}"
            or Path(path).name.startswith("test_")
            or Path(path).name.endswith("_test.py")
        )
    ]

    check_names = [
        "Python type check (mypy)",
        "TypeScript check (tsc)",
        "Python lint (Pylint)",
        "JavaScript lint (ESLint)",
        "Formatting (Prettier)",
        "Python security (Bandit)",
        "Security scan (Semgrep)",
        "Secret scan",
    ]
    results = await asyncio.gather(
        _run_mypy(worktree_dir, has_py),
        _run_tsc(worktree_dir, all_js_ts),
        _run_pylint(worktree_dir, py_changed),
        _run_eslint(worktree_dir, js_changed),
        _run_prettier(worktree_dir, fmt_changed),
        _run_bandit(worktree_dir, py_changed),
        _run_semgrep(worktree_dir, active_paths),
        _run_diff_secret_scan(context_bundle, mirror),
        _run_pytest_suite(worktree_dir, pytest_files),
    )
    check_tools = ["mypy", "tsc", "pylint", "eslint", "prettier", "bandit", "semgrep", None]
    applicable = [
        has_py,
        bool(all_js_ts),
        bool(py_changed),
        bool(js_changed),
        bool(fmt_changed),
        bool(py_changed),
        bool(active_paths),
        bool(active_paths),
    ]
    for name, findings, tool, applies in zip(check_names, results[:-1], check_tools, applicable):
        if not applies:
            status = "skipped (no matching changed project files)"
        elif tool and not _tool_path(worktree_dir, tool):
            status = f"skipped ({tool} not installed)"
        elif name == "Secret scan" and not shutil.which("gitleaks") and not shutil.which("trufflehog"):
            status = "completed (regex fallback)"
        else:
            status = "completed"
        report[name] = {"status": status, "findings": findings}
    report["PR pytest suite"] = results[-1]
    return report
