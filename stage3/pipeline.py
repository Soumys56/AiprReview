from __future__ import annotations

import asyncio
from pathlib import Path

from stage3.prepare_payload import build_agent_payload
from stage3.reviewers import AGENT_PROMPTS, run_parallel_review
from stage3.schema import ReviewFinding, Stage3Result
from stage3.static_checks import PROJECT_AREA_EXTENSIONS, run_static_checks_by_check


class Stage3Pipeline:
    """Export the PR worktree, run static checks, then parallel LLM reviewers."""

    def __init__(self, mirror, pr_number: int, worktree_root: str | Path, llm_client=None):
        self.mirror = mirror
        self.pr_number = pr_number
        self.worktree_root = Path(worktree_root)
        self.llm_client = llm_client
        self.worktree_dir: Path | None = None
        self.dependency_status: dict[str, dict] = {}
        self.check_results: dict[str, dict] = {}

    async def run(
        self,
        context_bundle: dict,
        user_story_and_bug_requirements: str = "",
        project_areas: list[str] | None = None,
    ) -> Stage3Result:
        from components.stage3 import Stage3

        exporter = Stage3(
            self.mirror,
            context_bundle["head_sha"],
            self.pr_number,
            self.worktree_root,
        )
        self.worktree_dir = await asyncio.to_thread(exporter.export_worktree)
        self.dependency_status = exporter.dependency_status

        self.check_results = await run_static_checks_by_check(
            context_bundle,
            mirror=self.mirror,
            worktree_dir=self.worktree_dir,
            project_areas=project_areas,
        )
        static_findings = [
            finding
            for result in self.check_results.values()
            for finding in result["findings"]
        ]
        static_dicts = [finding.to_dict() for finding in static_findings]
        review_chunks = context_bundle.get("chunks", [])
        if project_areas:
            allowed_extensions = {
                extension
                for area in project_areas
                for extension in PROJECT_AREA_EXTENSIONS.get(area, ())
            }
            review_chunks = [
                chunk for chunk in review_chunks
                if Path(chunk.get("file", "")).suffix.lower() in allowed_extensions
            ]
        review_context = {
            **context_bundle,
            "project_areas": project_areas or [],
            "chunks": review_chunks,
        }
        payload_json = build_agent_payload(
            review_context,
            static_dicts,
            user_story_and_bug_requirements,
        )
        agent_runs = await run_parallel_review(payload_json, client=self.llm_client)
        alignment_run = next((run for run in agent_runs if run.agent == "alignment"), None)
        alignment = alignment_run.output if alignment_run and alignment_run.status == "completed" else None
        if alignment:
            first_chunk_file = next(
                (chunk.get("file") for chunk in context_bundle.get("chunks", []) if chunk.get("file")),
                "PR requirements",
            )
            for case in alignment.get("requirement_test_cases", []):
                if not isinstance(case, dict) or str(case.get("result", "")).upper() != "FAILED":
                    continue
                try:
                    evidence_line = max(1, int(case.get("evidence_line") or 1))
                except (TypeError, ValueError):
                    evidence_line = 1
                static_findings.append(ReviewFinding(
                    agent="requirement_tests",
                    file=str(case.get("evidence_file") or first_chunk_file),
                    line=evidence_line,
                    symbol=None,
                    severity="high",
                    confidence="high",
                    category="requirement_test_failure",
                    rule_id=f"requirement-test/{case.get('id', 'failed')}",
                    title=f"Bug: requirement test failed — {case.get('requirement', 'acceptance criterion')}",
                    message=(
                        f"Scenario: {case.get('scenario', 'Requirement scenario')}\n"
                        f"Expected: {case.get('expected_behavior', 'Expected behavior not supplied')}\n"
                        f"Observed: {case.get('observed_behavior', 'Observed behavior not supplied')}\n"
                        f"Evidence: {case.get('explanation', 'The PR does not meet this requirement.')}"
                    ),
                    suggestion="Update the PR implementation to satisfy this requirement and add a regression test.",
                    requirement=str(case.get("requirement")) if case.get("requirement") else None,
                    expected_behavior=str(case.get("expected_behavior")) if case.get("expected_behavior") else None,
                    actual_behavior=str(case.get("observed_behavior")) if case.get("observed_behavior") else None,
                    root_cause=str(case.get("explanation")) if case.get("explanation") else None,
                ))
        return Stage3Result(
            static_findings=static_findings,
            agent_runs=agent_runs,
            alignment=alignment,
            spec_text=user_story_and_bug_requirements,
            payload_json=payload_json,
            agent_prompts=AGENT_PROMPTS.copy(),
        )
