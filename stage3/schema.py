from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class ReviewFinding:
    agent: str
    file: str
    line: int
    symbol: str | None
    severity: str
    confidence: str
    category: str
    rule_id: str
    title: str
    message: str
    suggestion: str | None = None
    requirement: str | None = None
    expected_behavior: str | None = None
    actual_behavior: str | None = None
    root_cause: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class AgentRunSummary:
    agent: str
    status: str
    latency_ms: int
    findings: list[ReviewFinding]
    summary: str = ""
    output: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "findings": [finding.to_dict() for finding in self.findings],
            "summary": self.summary,
            "output": self.output,
            "error": self.error,
        }


@dataclass(slots=True)
class Stage3Result:
    static_findings: list[ReviewFinding]
    agent_runs: list[AgentRunSummary]
    alignment: dict[str, Any] | None = None
    spec_text: str = ""
    payload_json: str = ""
    agent_prompts: dict[str, str] | None = None

    @property
    def findings(self) -> list[ReviewFinding]:
        return self.static_findings + [finding for run in self.agent_runs for finding in run.findings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "static_findings": [finding.to_dict() for finding in self.static_findings],
            "agent_runs": [run.to_dict() for run in self.agent_runs],
            "alignment": self.alignment,
            "spec_text": self.spec_text,
            "payload_json": self.payload_json,
            "agent_prompts": self.agent_prompts or {},
            "findings": [finding.to_dict() for finding in self.findings],
        }
