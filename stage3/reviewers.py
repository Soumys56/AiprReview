from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from stage3.llm_client import AsyncLLMClient
from stage3.schema import AgentRunSummary, ReviewFinding

AGENT_PROMPTS: dict[str, str] = {
    "alignment": """You are a User Story / Bug Alignment Assessor. Compare the PR changes against every requirement, acceptance criterion, security requirement, and bug symptom in user_story_and_bug_requirements. Return JSON with alignment_score (integer 0-100), alignment_status (ALIGNED, PARTIALLY_ALIGNED, or MISALIGNED), summary, met_requirements (array of strings), missing_or_unaddressed (array of strings), scope_creep (array of strings), and requirement_test_cases (array of objects with id, requirement, scenario, expected_behavior, observed_behavior, result [PASSED|FAILED|UNCLEAR], evidence_file, evidence_line, and explanation). Create a distinct scenario for each acceptance criterion or bug reproduction condition. Assess observed_behavior from the supplied PR code; these generated scenarios are not executed. Mark FAILED only when code evidence demonstrates the expected behavior is broken; use UNCLEAR when evidence is insufficient. Be evidence-based and do not invent requirements.""",
    "security": """Review the changed PR code against the attached User Story / Bug, including security acceptance criteria. Find concrete vulnerabilities: injection, broken authorization/IDOR, XSS, SSRF, unsafe deserialization, and exposed secrets. Return JSON {summary, findings:[{file,line,symbol,severity,confidence,category,rule_id,title,message,root_cause,requirement,expected_behavior,actual_behavior,suggestion}]}. For each issue, clearly state what is wrong, why it matters, the exact requirement or risk, the expected vs actual behavior, and the root cause at the cited line. Keep the fix concrete. Report only actionable issues supported by the supplied code/context. Do NOT re-report items present in already_flagged_by_static_tools or repo_level_static_findings.""",
    "logic_and_bugs": """Review whether the changed PR code correctly implements the attached User Story / Bug and acceptance criteria. Find defects that prevent expected behavior, fail to fix the reported root cause, violate caller contracts in impact.direct_callers, mishandle None/edge cases, or introduce async bugs. Return JSON {summary, findings:[{file,line,symbol,severity,confidence,category,rule_id,title,message,root_cause,requirement,expected_behavior,actual_behavior,suggestion}]}. For each issue, explain the broken behavior in plain terms, the requirement/bug symptom it violates, expected vs actual behavior, root cause at the cited line, and a concrete fix. Report only actionable, evidence-based defects. Do NOT re-report items present in already_flagged_by_static_tools or repo_level_static_findings.""",
    "tests_and_style": """Assess whether impact.affected_tests and changed test chunks cover every acceptance criterion and bug reproduction scenario in the attached spec, and whether changes follow repo_conventions. Return JSON {summary, findings:[{file,line,symbol,severity,confidence,category,rule_id,title,message,root_cause,requirement,expected_behavior,actual_behavior,suggestion}]}. Explain the missing test or clear convention violation, what behavior should be covered, and how to fix it. Do NOT re-report items present in already_flagged_by_static_tools or repo_level_static_findings.""",
}


def _findings_from_output(agent: str, output: dict[str, Any]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for item in output.get("findings", []):
        if not isinstance(item, dict) or not item.get("file"):
            continue
        severity = str(item.get("severity", "medium")).lower()
        if severity not in {"critical", "high", "medium", "low", "info"}:
            severity = "medium"
        try:
            line = max(1, int(item.get("line") or 1))
        except (TypeError, ValueError):
            line = 1
        findings.append(ReviewFinding(
            agent=agent,
            file=str(item["file"]),
            line=line,
            symbol=str(item["symbol"]) if item.get("symbol") else None,
            severity=severity,
            confidence=str(item.get("confidence", "medium")).lower(),
            category=str(item.get("category", agent)),
            rule_id=str(item.get("rule_id", f"llm/{agent}")),
            title=str(item.get("title", "Review finding")),
            message=str(item.get("message", "")),
            suggestion=str(item["suggestion"]) if item.get("suggestion") else None,
            requirement=str(item["requirement"]) if item.get("requirement") else None,
            expected_behavior=str(item["expected_behavior"]) if item.get("expected_behavior") else None,
            actual_behavior=str(item["actual_behavior"]) if item.get("actual_behavior") else None,
            root_cause=str(item["root_cause"]) if item.get("root_cause") else None,
        ))
    return findings


async def _run_agent(
    name: str, client: AsyncLLMClient, user_payload: str
) -> AgentRunSummary:
    started = time.perf_counter()
    try:
        output = await client.complete_json(AGENT_PROMPTS[name], user_payload)
        latency = int((time.perf_counter() - started) * 1000)
        return AgentRunSummary(
            agent=name,
            status="completed",
            latency_ms=latency,
            findings=_findings_from_output(name, output),
            summary=str(output.get("summary", "")),
            output=output,
        )
    except Exception as exc:
        return AgentRunSummary(
            agent=name,
            status="failed",
            latency_ms=int((time.perf_counter() - started) * 1000),
            findings=[],
            error=str(exc),
        )


async def run_parallel_review(
    payload: dict[str, Any] | str, client: AsyncLLMClient | None = None
) -> list[AgentRunSummary]:
    """Run four focused reviewers concurrently over the same compact JSON payload."""
    llm = client or AsyncLLMClient()
    user_payload = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return list(await asyncio.gather(*(
        _run_agent(name, llm, user_payload) for name in AGENT_PROMPTS
    )))
