"""The learning tools, reading a tenant's cases instead of a local folder.

`CaseLoaderAgent` walks `data/agent_output/` on disk. That is right for the
eval harness and for a laptop, and useless in a deployed system: the API and
the worker are containers with nothing in that directory, and the cases belong
to tenants who must not see each other's. A rule drafted against an empty
folder is drafted against nothing, and its impact report would truthfully say
"no collateral" because it looked at no cases at all.

Same two methods the orchestrator and the impact assessor already call, so
they take this instead of that and nothing else changes.
"""

from __future__ import annotations

from .context import build_case_context
from .core.case_loader import CaseData
from .ports import CaseRepository, RunContext


class TenantCaseLoader:
    """`CaseLoaderAgent`'s surface, over the tenant case store."""

    def __init__(self, cases: CaseRepository, tenant_id: str, documents=None):
        self.cases = cases
        self.tenant_id = tenant_id
        self.documents = documents

    def list_cases(self) -> list[str]:
        """The tenant's invoice cases. A scanned PO or receipt's case holds
        no invoice, so a rule measured over it counts what never runs."""
        skip = (
            self.documents.non_invoice_ids(self.tenant_id)
            if self.documents is not None
            else set()
        )
        return sorted(
            c for c in self.cases.iter_case_ids(self.tenant_id) if c not in skip
        )

    def load_many(self, case_ids: list[str]) -> dict[str, CaseData]:
        """Every case at once.

        One query rather than one per case: the impact assessor reads the
        whole corpus to measure a rule's blast radius, and against a database
        a round trip per case is the difference between a report someone waits
        for and one they abandon. This is the batch read `CaseRepository`'s own
        docstring says exists for exactly this.
        """
        artifacts_by_case = self.cases.get_many_artifacts(
            self.tenant_id, case_ids
        )
        return {
            case_id: self._to_case_data(case_id, artifacts)
            for case_id, artifacts in artifacts_by_case.items()
        }

    def run(self, case_id: str) -> CaseData:
        artifacts = self.cases.get_artifacts(
            RunContext(tenant_id=self.tenant_id, case_id=case_id)
        )
        if not artifacts:
            raise FileNotFoundError(f"No case {case_id} for this tenant")
        return self._to_case_data(case_id, artifacts)

    @staticmethod
    def _to_case_data(case_id: str, artifacts: dict) -> CaseData:
        data = CaseData(case_id=case_id)
        data.artifacts = dict(artifacts)
        data.postprocessing = artifacts.get("postprocessing") or {}
        # The canonical builder, the same one the live engine evaluates
        # against. A rule measured in one vocabulary and run in another is how
        # an impact report comes to promise something the engine does not do.
        data.context = build_case_context(artifacts)
        data.alf_audit = artifacts.get("alf") or {}
        data.alf_postprocessing = artifacts.get("postprocessing_pre_alf") or {}
        return data
