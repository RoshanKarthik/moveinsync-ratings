"""The issue taxonomy (taxonomy.yaml) as typed objects."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

RootCause = Literal["defect", "design", "policy", "service", "gap"]

ROOT_CAUSE_LABELS: dict[str, str] = {
    "defect": "Broken (bug or wrong data)",
    "design": "Works, but badly designed",
    "policy": "Rule set by MoveInSync or the employer",
    "service": "Cab, driver or helpdesk failed",
    "gap": "Missing feature",
}

# Codes that carry no diagnosable reason; excluded from "which issues drive bad ratings".
NON_SPECIFIC = frozenset({"generic_negative", "generic_positive", "praise_specific", "other"})


class Issue(BaseModel):
    code: str
    stage: str
    owner: str
    root: RootCause
    label: str
    definition: str


class Taxonomy(BaseModel):
    version: int
    stages: dict[str, str]
    owners: dict[str, str]
    issues: dict[str, Issue]

    @property
    def codes(self) -> list[str]:
        return list(self.issues)

    @property
    def specific_codes(self) -> list[str]:
        return [c for c in self.issues if c not in NON_SPECIFIC]

    def stage_label(self, code: str) -> str:
        return self.stages[self.issues[code].stage]

    def owner_label(self, code: str) -> str:
        return self.owners[self.issues[code].owner]

    def prompt_block(self) -> str:
        """Codes and definitions grouped by journey stage, for the tagging prompt."""
        lines: list[str] = []
        for stage, stage_label in self.stages.items():
            members = [i for i in self.issues.values() if i.stage == stage]
            if not members:
                continue
            lines.append(f"## {stage_label}")
            lines.extend(f"- {i.code}: {i.definition}" for i in members)
            lines.append("")
        return "\n".join(lines).strip()


def load_taxonomy(path: Path) -> Taxonomy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    issues = {code: Issue(code=code, **body) for code, body in raw["issues"].items()}
    tax = Taxonomy(version=raw["version"], stages=raw["stages"], owners=raw["owners"], issues=issues)
    for issue in tax.issues.values():
        if issue.stage not in tax.stages:
            raise ValueError(f"{issue.code}: unknown stage {issue.stage!r}")
        if issue.owner not in tax.owners:
            raise ValueError(f"{issue.code}: unknown owner {issue.owner!r}")
    return tax
