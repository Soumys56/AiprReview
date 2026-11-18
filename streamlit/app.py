from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import importlib
import streamlit as st
from config.setting import settings
from components.github_fetchpr_client import githubFetch, GitHubPRDiff
from components.index_store import IndexStore
from components.repo_mirror import RepoMirror
from service.offline_branch_pr import build_offline_branch_pr, extract_zip_to_map
import service.pipeline
importlib.reload(service.pipeline)
from service.pipeline import Stage2Pipeline

if not hasattr(Stage2Pipeline, "run_manual"):
    async def _run_manual(self, pr: dict, files: list[dict]) -> dict:
        return await asyncio.to_thread(self._run_sync, pr, files)
    Stage2Pipeline.run_manual = _run_manual

from stage3.pipeline import Stage3Pipeline
from stage3.spec_parser import parse_user_story_attachment
from utilitis.utitis import separate_before_after


def source_excerpt(
    worktree_dir: str | None,
    file_path: str,
    line_number: int,
    radius: int = 2,
) -> tuple[str, str] | None:
    """Return numbered source context around a finding line, constrained to the PR worktree."""
    if not worktree_dir or not file_path or file_path == "PR requirements":
        return None
    root = Path(worktree_dir).resolve()
    source_path = (root / file_path).resolve()
    if not source_path.is_relative_to(root) or not source_path.is_file():
        return None
    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    target = min(max(1, int(line_number)), len(lines))
    start = max(1, target - radius)
    end = min(len(lines), target + radius)
    snippet = "\n".join(
        f"{'>' if number == target else ' '} {number:>5} | {lines[number - 1]}"
        for number in range(start, end + 1)
    )
    language_by_extension = {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".html": "html",
        ".htm": "html",
        ".json": "json",
        ".css": "css",
        ".scss": "css",
    }
    return snippet, language_by_extension.get(source_path.suffix.lower(), "text")


st.set_page_config(
    page_title="AI PR REVIEW",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.title("🚀 AI Pull Request Review")


@st.cache_data(ttl=120, show_spinner="Fetching pull requests...")
def get_all_prs(state: str):
    return asyncio.run(
        githubFetch(settings.REPO, settings.OWNER).github_fetch_all_pr()
    )


@st.cache_data(ttl=120, show_spinner="Fetching changed files...")
def get_pr_changes(pr_number: int):
    return asyncio.run(
        GitHubPRDiff(settings.REPO, settings.OWNER, pr_number).github_fetch_pr_diff()
    )


@st.cache_resource
def get_pipeline():
    cache_dir = getattr(settings, "REPO_CACHE_DIR", ".cache/repos")
    git_host = getattr(settings, "GIT_HOST", "github.com")
    store = IndexStore(settings.INDEX_DB_PATH)
    mirror = RepoMirror(
        settings.OWNER,
        settings.REPO,
        settings.GITHUB_TOKEN,
        cache_dir,
        git_host,
    )
    return Stage2Pipeline(store, mirror, settings.OWNER, settings.REPO)


# -----------------------------
# Mode Selector: Offline Branch Upload vs GitHub PR
# -----------------------------
has_github_token = bool(settings.GITHUB_TOKEN and settings.GITHUB_TOKEN.strip())
default_mode_index = 1 if has_github_token else 0

selected_mode = st.radio(
    "Review Mode",
    ["Offline Branch Upload (No Token)", "GitHub PR"],
    index=default_mode_index,
    horizontal=True,
    help="Select 'Offline Branch Upload' to review local branch code without a GitHub token.",
)

is_offline = selected_mode == "Offline Branch Upload (No Token)"

# -----------------------------
# Mode 1: Offline Branch Upload Panel
# -----------------------------
if is_offline:
    st.caption("📦 **Offline Mode:** Upload `main` and your PR branch `.zip` (or individual files) to compute Git diffs locally and run Stage 2 + Stage 3 reviews.")

    with st.container(border=True):
        col_base, col_head = st.columns(2)

        with col_base:
            st.markdown("#### 1️⃣ Base Branch (`main`)")
            base_uploads = st.file_uploader(
                "Upload base branch archive (.zip) or files",
                type=["zip", "py", "js", "ts", "tsx", "jsx", "json", "html", "css", "md", "txt", "toml", "yaml", "yml"],
                accept_multiple_files=True,
                key="offline_base_upload",
                help="Upload a .zip containing the base/main branch or select multiple files.",
            )

        with col_head:
            st.markdown("#### 2️⃣ PR Branch")
            head_uploads = st.file_uploader(
                "Upload PR branch archive (.zip) or files",
                type=["zip", "py", "js", "ts", "tsx", "jsx", "json", "html", "css", "md", "txt", "toml", "yaml", "yml"],
                accept_multiple_files=True,
                key="offline_head_upload",
                help="Upload a .zip containing the PR branch or select multiple modified/new files.",
            )

        col_meta1, col_meta2 = st.columns([1, 2])
        with col_meta1:
            offline_project_name = st.text_input(
                "Project name",
                value=st.session_state.get("offline_project_name", "offline-repo"),
                key="offline_input_project_name",
            )
        with col_meta2:
            offline_pr_title = st.text_input(
                "PR title / description",
                value=st.session_state.get("offline_pr_title", "Offline Branch PR Review"),
                key="offline_input_pr_title",
            )

        if st.button("Build Diff & Load Offline PR", type="primary", key="btn_build_offline_pr"):
            if not base_uploads and not head_uploads:
                st.error("Please upload at least one archive (.zip) or file for base and/or PR branch.")
            else:
                with st.spinner("Extracting archives, initializing bare Git mirror, and computing diff..."):
                    try:
                        def _collect_files(uploads) -> dict[str, bytes]:
                            res: dict[str, bytes] = {}
                            for up in uploads or []:
                                if up.name.lower().endswith(".zip"):
                                    res.update(extract_zip_to_map(up.getvalue()))
                                else:
                                    res[up.name] = up.getvalue()
                            return res

                        base_map = _collect_files(base_uploads)
                        head_map = _collect_files(head_uploads)

                        cache_dir = getattr(settings, "REPO_CACHE_DIR", ".cache/repos")
                        mirror, pr_meta, changed_files = build_offline_branch_pr(
                            base_files=base_map,
                            head_files=head_map,
                            project_name=offline_project_name,
                            cache_dir=cache_dir,
                            pr_title=offline_pr_title,
                        )

                        st.session_state["offline_mirror"] = mirror
                        st.session_state["offline_pr_meta"] = pr_meta
                        st.session_state["offline_changed_files"] = changed_files
                        st.session_state["offline_project_name"] = offline_project_name
                        st.session_state["offline_pr_title"] = offline_pr_title
                        st.session_state["offline_pr_loaded"] = True

                        # Clear old Stage 2 / Stage 3 session state entries
                        for k in list(st.session_state.keys()):
                            if k.startswith(("stage2_offline", "stage3_offline")):
                                del st.session_state[k]

                        st.success(
                            f"✅ Successfully built offline diff: {len(changed_files)} changed file(s) "
                            f"(base `{pr_meta['base_sha'][:7]}` → head `{pr_meta['head_sha'][:7]}`)."
                        )
                    except Exception as exc:
                        st.error(f"Failed to build offline PR diff: {exc}")

    if not st.session_state.get("offline_pr_loaded"):
        st.info("👆 Upload your base and PR branch files above and click **'Build Diff & Load Offline PR'** to proceed with the review.")
        st.stop()

    active_mirror = st.session_state["offline_mirror"]
    active_pr = st.session_state["offline_pr_meta"]
    active_changed_files = st.session_state["offline_changed_files"]
    active_pr_number = active_pr["number"]
    pr_state_key = f"offline_{active_pr['head_sha'][:8]}"
    st.info(f"📂 Active Offline PR: **{active_pr['title']}** (`{active_pr['base_sha'][:7]}` → `{active_pr['head_sha'][:7]}`)")

# -----------------------------
# Mode 2: GitHub PR Selector
# -----------------------------
else:
    st.caption(f"Repository: {settings.OWNER}/{settings.REPO}")
    if not has_github_token:
        st.error("GITHUB_TOKEN is missing from `.env`. Please add your token or switch to 'Offline Branch Upload (No Token)' mode.")
        st.stop()

    state = st.radio("PR state", ["open"], horizontal=True)
    try:
        prs = get_all_prs(state)
    except Exception as e:
        st.error(f"Failed to fetch Pull Requests: {e}")
        st.stop()

    if not prs:
        st.warning("No Pull Requests found.")
        st.stop()

    pr_options = {f"PR #{p['number']} - {p['title']}": p["number"] for p in prs}
    selected = st.selectbox("Choose your Pull Request:", list(pr_options))
    active_pr_number = pr_options[selected]
    active_mirror = get_pipeline().mirror
    active_pr = {"number": active_pr_number, "owner": settings.OWNER, "repo": settings.REPO}
    try:
        active_changed_files = get_pr_changes(active_pr_number)
    except Exception as e:
        st.error(f"Failed to fetch PR changes: {e}")
        st.stop()

    pr_state_key = str(active_pr_number)


# -----------------------------
# Tabs: Changed files, Stage 2 context, Stage 3 review
# -----------------------------
tab_diff, tab_stage2, tab_stage3 = st.tabs(
    ["📂 Changed files", "🧠 Stage 2 context", "🤖 Stage 3 review"]
)

# -----------------------------
# Tab 1: Changed files (Before vs After)
# -----------------------------
with tab_diff:
    st.subheader("📂 Changed Files")

    if not active_changed_files:
        st.warning("No changed files found.")
    else:
        files_list = [active_changed_files] if isinstance(active_changed_files, dict) else active_changed_files

        for file in files_list:
            filename = file.get("filename", "Unknown file")
            st.markdown(f"### 📄 `{filename}`")
            st.caption(
                f"Status: **{file.get('status', 'unknown')}** | "
                f"Additions: +{file.get('additions', 0)} | "
                f"Deletions: -{file.get('deletions', 0)}"
            )

            patch = file.get("patch")
            if file.get("before_changes") is not None:
                cmp = file
            elif patch:
                cmp = separate_before_after(patch, filename)
            else:
                cmp = {"before_changes": "", "after_changes": ""}

            before = cmp.get("before_changes", "")
            after = cmp.get("after_changes", "")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("#### 🔴 Before Changes")
                st.code(before or "No previous content", language=source_excerpt(None, filename, 1, 1)[1] if source_excerpt(None, filename, 1, 1) else "python")
            with c2:
                st.markdown("#### 🟢 After Changes")
                st.code(after or "No updated content", language=source_excerpt(None, filename, 1, 1)[1] if source_excerpt(None, filename, 1, 1) else "python")
            st.divider()

# -----------------------------
# Tab 2: Stage 2 Context (Impact Analysis)
# -----------------------------
with tab_stage2:
    st.subheader("🧠 Stage 2 Semantic & Impact Analysis")
    stage2_state_key = f"stage2_{pr_state_key}"

    if st.button("Run Stage 2 (impact analysis)", type="primary", key=f"btn_stage2_{stage2_state_key}"):
        with st.spinner("Indexing base commit and analysing symbol impacts..."):
            try:
                if is_offline:
                    store = IndexStore(settings.INDEX_DB_PATH)
                    pipeline = Stage2Pipeline(
                        store=store,
                        mirror=active_mirror,
                        owner="local",
                        repo=active_pr.get("repo", "offline-repo"),
                    )
                    if hasattr(pipeline, "run_manual"):
                        result = asyncio.run(pipeline.run_manual(active_pr, active_changed_files))
                    else:
                        result = asyncio.run(asyncio.to_thread(pipeline._run_sync, active_pr, active_changed_files))
                else:
                    pipeline = get_pipeline()
                    result = asyncio.run(pipeline.run(active_pr_number))

                st.session_state[stage2_state_key] = result
            except Exception as e:
                st.error(f"Stage 2 failed: {e}")
                st.stop()

    result = st.session_state.get(stage2_state_key)
    if result:
        if result.get("warnings"):
            for w in result["warnings"]:
                st.warning(w)

        if not result.get("chunks"):
            st.info("No semantic or file-window chunks generated for this PR.")

        blast_icons = {"high": "🔴", "medium": "🟠", "low": "🟡", "none": "🟢"}

        for ch in result.get("chunks", []):
            if ch.get("chunk_type") != "symbol":
                st.info(f"{ch.get('file', 'Unknown')}: {ch.get('reason', '')} (sent as raw patch)")
                continue

            imp = ch.get("impact", {})
            icon = blast_icons.get(imp.get("blast_radius"), "⚪")

            with st.expander(f"{icon} {ch.get('symbol')} · {ch.get('change_type')} · `{ch.get('file')}`"):
                if ch.get("old_signature") != ch.get("new_signature"):
                    st.write(f"`{ch.get('old_signature')}` → `{ch.get('new_signature')}`")

                for c in imp.get("direct_callers", []):
                    st.write(
                        f"- {c['status']} · `{c['symbol']}` ({c['file']}:{c['line']}) "
                        f"{'updated' if c.get('updated_in_pr') else 'NOT in PR'} {c.get('detail', '')}"
                    )

                st.code(ch.get("symbol_diff") or ch.get("code") or "", language="diff")

        st.download_button(
            "Download chunks JSON",
            json.dumps(result, indent=2),
            file_name=f"pr_{active_pr_number}_stage2.json",
            mime="application/json",
            key=f"download_stage2_{stage2_state_key}",
        )

# -----------------------------
# Tab 3: Stage 3 Review (Static Checks + Parallel LLMs)
# -----------------------------
with tab_stage3:
    st.subheader("🤖 User Story / Bug Alignment + Parallel Review")
    st.write("Add story or bug requirements to assess PR alignment and run independent reviewers.")

    selected_project_areas = st.multiselect(
        "Which project area(s) does this PR concern? Select before analysis.",
        [
            "User frontend",
            "Moderator frontend",
            "User backend",
            "Moderator backend",
            "Chatbot",
            "AI PR Review (Python)",
        ],
        key=f"project_areas_{pr_state_key}",
        default=["AI PR Review (Python)"],
        help="The selection routes files to language-appropriate checks. Frontend/backend JS/TS use ESLint and TypeScript; Python uses Ruff, Bandit, and Python linters.",
    )

    stage3_state_key = f"stage3_{pr_state_key}_{'_'.join(sorted(selected_project_areas)) or 'unselected'}"

    uploaded_spec = st.file_uploader(
        "Upload a User Story / Bug file",
        type=["xml", "txt", "md", "xlsx", "html", "htm", "json", "csv"],
        key=f"spec_upload_{pr_state_key}",
    )
    pasted_requirements = st.text_area(
        "Or paste acceptance criteria / bug details",
        height=150,
        key=f"spec_text_{pr_state_key}",
        placeholder="As a user, I want…\nAcceptance criteria…\nBug reproduction steps…",
    )

    parsed_spec = ""
    if uploaded_spec is not None:
        try:
            parsed_spec = parse_user_story_attachment(
                uploaded_spec.name,
                uploaded_spec.getvalue(),
            )
            st.markdown("**Parsed attachment preview**")
            st.text_area("Parsed specification", parsed_spec, height=200, disabled=True)
        except (ValueError, RuntimeError) as exc:
            st.error(f"Could not parse the specification: {exc}")

    spec_parts = [part.strip() for part in (parsed_spec, pasted_requirements) if part.strip()]
    requirements_text = "\n\n## Additional pasted criteria\n".join(spec_parts)
    if not requirements_text:
        st.info("You can run security, logic, and test reviewers without a spec; alignment scoring needs story or bug requirements.")

    if st.button(
        "Run Stage 3",
        type="primary",
        key=f"btn_run_stage3_{stage3_state_key}",
        disabled=not selected_project_areas,
    ):
        try:
            with st.spinner("Running static checks and parallel LLM reviewers…"):
                stage2_result = st.session_state.get(stage2_state_key)
                if not stage2_result:
                    if is_offline:
                        store = IndexStore(settings.INDEX_DB_PATH)
                        pipe = Stage2Pipeline(
                            store=store,
                            mirror=active_mirror,
                            owner="local",
                            repo=active_pr.get("repo", "offline-repo"),
                        )
                        if hasattr(pipe, "run_manual"):
                            stage2_result = asyncio.run(pipe.run_manual(active_pr, active_changed_files))
                        else:
                            stage2_result = asyncio.run(asyncio.to_thread(pipe._run_sync, active_pr, active_changed_files))
                    else:
                        stage2_result = asyncio.run(get_pipeline().run(active_pr_number))
                    st.session_state[stage2_state_key] = stage2_result

                stage3 = Stage3Pipeline(
                    mirror=active_mirror,
                    pr_number=active_pr_number,
                    worktree_root=f"{settings.REPO_CACHE_DIR}/worktree",
                )
                result = asyncio.run(
                    stage3.run(
                        stage2_result,
                        requirements_text,
                        project_areas=selected_project_areas,
                    )
                )
                st.session_state[stage3_state_key] = {
                    "checks": {
                        name: {
                            "status": check["status"],
                            "findings": [finding.to_dict() for finding in check["findings"]],
                        }
                        for name, check in stage3.check_results.items()
                    },
                    "dependencies": stage3.dependency_status,
                    "head_sha": stage2_result["head_sha"],
                    "worktree_dir": str(stage3.worktree_dir) if stage3.worktree_dir else None,
                    "result": result.to_dict(),
                }
        except Exception as exc:
            st.error(f"Stage 3 analysis failed: {exc}")

    stage3_result = st.session_state.get(stage3_state_key)
    if stage3_result:
        result_data = stage3_result["result"]
        st.caption("Each card is one check. Skipped checks need their listed tool installed.")
        checks = list(stage3_result["checks"].items())
        for start in range(0, len(checks), 3):
            columns = st.columns(3)
            for column, (check_name, check_result) in zip(columns, checks[start : start + 3]):
                with column:
                    with st.container(border=True):
                        check_findings = check_result["findings"]
                        if check_result["status"].startswith("skipped"):
                            st.warning(check_result["status"])
                        elif check_findings:
                            st.error(f"{len(check_findings)} issue(s)")
                        else:
                            st.success("Completed · no findings")
                        st.markdown(f"**{check_name}**")
                        st.caption(check_result["status"])
                        for finding in check_findings:
                            st.markdown(
                                f"**{finding['severity'].upper()}** · "
                                f"`{finding['file']}:{finding['line']}`  \n"
                                f"{finding['title']}"
                            )
                            st.caption(finding["message"])
                            if finding.get("requirement"):
                                st.markdown(f"**Requirement:** {finding['requirement']}")
                            if finding.get("root_cause"):
                                st.markdown(f"**Why it happens:** {finding['root_cause']}")
                            if finding.get("expected_behavior"):
                                st.markdown(f"**Expected:** {finding['expected_behavior']}")
                            if finding.get("actual_behavior"):
                                st.markdown(f"**Actual:** {finding['actual_behavior']}")
                            excerpt = source_excerpt(
                                stage3_result.get("worktree_dir"),
                                finding.get("file", ""),
                                finding.get("line", 1),
                            )
                            if excerpt:
                                st.code(excerpt[0], language=excerpt[1], line_numbers=False)
                            if finding.get("file") and stage3_result.get("head_sha"):
                                if not is_offline:
                                    source_url = (
                                        f"https://{settings.GIT_HOST}/{settings.OWNER}/{settings.REPO}"
                                        f"/blob/{stage3_result['head_sha']}/{quote(finding['file'], safe='/')}"
                                        f"#L{finding['line']}"
                                    )
                                    st.markdown(f"[Open {finding['file']}:{finding['line']} on GitHub]({source_url})")
                                else:
                                    st.caption(f"📁 Local file: `{finding['file']}:{finding['line']}`")
                            if finding.get("suggestion"):
                                st.info(f"Suggested fix: {finding['suggestion']}")

        with st.expander("Dependency setup details"):
            st.json(stage3_result["dependencies"])

        st.markdown("### User Story Alignment")
        alignment = result_data.get("alignment")
        if alignment:
            try:
                score = max(0, min(100, int(alignment.get("alignment_score", 0))))
            except (TypeError, ValueError):
                score = 0
            status = alignment.get("alignment_status", "PARTIALLY_ALIGNED")
            score_col, status_col = st.columns([1, 2])
            score_col.metric("Alignment score", f"{score}%")
            status_col.metric("Status", status.replace("_", " ").title())
            st.progress(score)
            st.write(alignment.get("summary", ""))
            met_col, missing_col = st.columns(2)
            with met_col:
                st.markdown("**Met requirements**")
                for item in alignment.get("met_requirements", []):
                    st.markdown(f"- {item}")
                if not alignment.get("met_requirements"):
                    st.caption("No requirements marked as met.")
            with missing_col:
                st.markdown("**Missing or unaddressed**")
                for item in alignment.get("missing_or_unaddressed", []):
                    st.markdown(f"- {item}")
                if not alignment.get("missing_or_unaddressed"):
                    st.caption("No missing requirements reported.")
            if alignment.get("scope_creep"):
                st.markdown("**Potential scope creep**")
                for item in alignment["scope_creep"]:
                    st.markdown(f"- {item}")
        else:
            st.info("Alignment score is unavailable. Add story/bug requirements and check the alignment reviewer status below.")

        requirement_cases = (alignment or {}).get("requirement_test_cases", [])
        st.markdown("### Requirement-driven test scenarios")
        st.caption("These scenarios are assessed against the PR code by the alignment reviewer.")
        if requirement_cases:
            for case in requirement_cases:
                verdict = str(case.get("result", "UNCLEAR")).upper()
                if verdict == "FAILED":
                    st.error(f"BUG · {case.get('requirement', case.get('id', 'Requirement'))}")
                elif verdict == "PASSED":
                    st.success(f"PASSED · {case.get('requirement', case.get('id', 'Requirement'))}")
                else:
                    st.warning(f"UNCLEAR · {case.get('requirement', case.get('id', 'Requirement'))}")
                with st.expander(case.get("scenario", "Requirement scenario")):
                    st.markdown(f"**Expected:** {case.get('expected_behavior', 'Not provided')}")
                    st.markdown(f"**Observed in PR:** {case.get('observed_behavior', 'Not assessed')}")
                    if case.get("evidence_file"):
                        st.caption(f"Evidence: {case['evidence_file']}:{case.get('evidence_line', 1)}")
                        excerpt = source_excerpt(
                            stage3_result.get("worktree_dir"),
                            case["evidence_file"],
                            case.get("evidence_line", 1),
                        )
                        if excerpt:
                            st.code(excerpt[0], language=excerpt[1], line_numbers=False)
                    if case.get("explanation"):
                        st.write(case["explanation"])
        else:
            st.info("No requirement scenarios were returned. Add clear acceptance criteria or bug reproduction steps to generate them.")

        st.markdown("### Parallel reviewer runs")
        agent_runs = result_data.get("agent_runs", [])
        if agent_runs:
            all_review_findings = result_data.get("findings", [])
            security_findings = [
                finding for finding in all_review_findings
                if finding.get("agent") == "security"
                or finding.get("category") in {"security", "sast", "secret_exposure"}
            ]
            logic_findings = [
                finding for finding in all_review_findings
                if finding.get("agent") == "logic_and_bugs"
            ]
            high_critical = sum(
                finding.get("severity") in {"critical", "high"}
                for finding in all_review_findings
            )
            summary_cols = st.columns(4)
            summary_cols[0].metric("All reported issues", len(all_review_findings))
            summary_cols[1].metric("Bug / logic issues", len(logic_findings))
            summary_cols[2].metric("Security issues", len(security_findings))
            summary_cols[3].metric("High / critical", high_critical)

            metric_cols = st.columns(len(agent_runs))
            for column, run in zip(metric_cols, agent_runs):
                column.metric(run["agent"].replace("_", " ").title(), f"{run['latency_ms']} ms")
                if run["status"] != "completed":
                    column.caption(run.get("error") or run["status"])

            st.markdown("#### Findings")
            filter_cols = st.columns(3)
            severity_options = ["critical", "high", "medium", "low", "info"]
            agent_options = sorted({finding.get("agent", "static") for finding in all_review_findings})
            category_options = sorted({finding.get("category", "other") for finding in all_review_findings})
            file_options = sorted({finding.get("file", "") for finding in all_review_findings if finding.get("file")})
            severity_filter = filter_cols[0].multiselect(
                "Severity", severity_options, default=severity_options,
                key=f"stage3_severity_{stage3_state_key}",
            )
            agent_filter = filter_cols[1].multiselect(
                "Agent", agent_options, default=agent_options,
                key=f"stage3_agent_{stage3_state_key}",
            )
            category_filter = filter_cols[2].multiselect(
                "Category", category_options, default=category_options,
                key=f"stage3_category_{stage3_state_key}",
            )
            file_filter = st.selectbox(
                "File", ["All files", *file_options], key=f"stage3_file_{stage3_state_key}"
            )
            search_filter = st.text_input(
                "Search findings", key=f"stage3_search_{stage3_state_key}",
                placeholder="Search title, message, or rule ID",
            ).strip().lower()

            all_findings = [
                finding for finding in all_review_findings
                if finding.get("severity") in severity_filter
                and finding.get("agent", "static") in agent_filter
                and finding.get("category", "other") in category_filter
                and (file_filter == "All files" or finding.get("file") == file_filter)
                and (
                    not search_filter
                    or search_filter in finding.get("title", "").lower()
                    or search_filter in finding.get("message", "").lower()
                    or search_filter in finding.get("rule_id", "").lower()
                )
            ]
            if not all_findings:
                st.success("No findings match these filters.")
            for finding in all_findings:
                with st.expander(
                    f"{finding['severity'].upper()} · {finding['agent']} · "
                    f"`{finding['file']}:{finding['line']}` · {finding['title']}"
                ):
                    excerpt = source_excerpt(
                        stage3_result.get("worktree_dir"),
                        finding.get("file", ""),
                        finding.get("line", 1),
                    )
                    if excerpt:
                        st.code(excerpt[0], language=excerpt[1], line_numbers=False)
                    if not is_offline:
                        st.markdown(
                            f"[{finding['file']}:{finding['line']} on GitHub]"
                            f"(https://{settings.GIT_HOST}/{settings.OWNER}/{settings.REPO}"
                            f"/blob/{stage3_result['head_sha']}/{quote(finding['file'], safe='/')}"
                            f"#L{finding['line']})"
                        )
                    else:
                        st.caption(f"📁 Local file: `{finding['file']}:{finding['line']}`")
                    if finding.get("requirement"):
                        st.markdown(f"**Requirement / bug:** {finding['requirement']}")
                    st.markdown(f"**What is wrong:** {finding['message']}")
                    if finding.get("root_cause"):
                        st.markdown(f"**Why it happens:** {finding['root_cause']}")
                    if finding.get("expected_behavior") or finding.get("actual_behavior"):
                        expected_col, actual_col = st.columns(2)
                        expected_col.markdown(
                            f"**Expected**\n\n{finding.get('expected_behavior') or 'Not specified'}"
                        )
                        actual_col.markdown(
                            f"**Actual**\n\n{finding.get('actual_behavior') or finding['message']}"
                        )
                    if finding.get("suggestion"):
                        st.markdown(f"**Suggested fix:** {finding['suggestion']}")

            for run in agent_runs:
                if run.get("summary"):
                    with st.expander(f"{run['agent'].replace('_', ' ').title()} summary"):
                        st.write(run["summary"])

        with st.expander("Prepared review payload", expanded=False):
            st.download_button(
                "Download payload JSON",
                data=result_data["payload_json"],
                file_name=f"pr-{active_pr_number}-review-payload.json",
                mime="application/json",
                key=f"payload_download_{pr_state_key}",
            )
            st.code(result_data["payload_json"], language="json")

        with st.expander("Exact prompts sent to Groq", expanded=False):
            st.caption("Each reviewer gets its system instruction below and the same user payload shown here.")
            for agent_name, system_prompt in result_data.get("agent_prompts", {}).items():
                st.markdown(f"**{agent_name.replace('_', ' ').title()} system prompt**")
                st.code(system_prompt, language="text")
            st.markdown("**Shared user payload**")
            st.code(result_data["payload_json"], language="json")
