"""The honesty layer: what this deployment can actually do, probed at runtime.

Every claim the UI makes about Power BI integration reads from here. A
capability is only ``IMPLEMENTED`` if the code exists *and* its dependency is
importable/configured in this process -- not because it is in the repo.

``tested=True`` marks capabilities covered by the automated test suite; the
test ``test_capability_claims_match_tests`` keeps those flags honest.
"""
from __future__ import annotations

import importlib.util
import shutil
from functools import lru_cache

from ..config import get_settings
from ..schemas.common import Capability, CapabilityStatus


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _binary_available(name: str) -> bool:
    return shutil.which(name) is not None


@lru_cache(maxsize=1)
def _probe() -> dict[str, bool]:
    return {
        "pandas": _module_available("pandas"),
        "openpyxl": _module_available("openpyxl"),
        "xlrd": _module_available("xlrd"),
        "duckdb": _module_available("duckdb"),
        "anthropic": _module_available("anthropic"),
        "dotnet": _binary_available("dotnet"),
        "pbi_tools": _binary_available("pbi-tools"),
    }


def reset_probe_cache() -> None:
    _probe.cache_clear()


def capabilities() -> list[Capability]:
    p = _probe()
    s = get_settings()
    caps: list[Capability] = [
        Capability(
            key="profile.csv", title="CSV profiling",
            status=CapabilityStatus.IMPLEMENTED if p["pandas"] else CapabilityStatus.UNAVAILABLE,
            detail="Encoding and delimiter detection, header-row discovery, full column "
                   "profiling.", requires=["pandas"], tested=True,
        ),
        Capability(
            key="profile.xlsx", title="Excel (.xlsx/.xlsm) profiling",
            status=CapabilityStatus.IMPLEMENTED if p["openpyxl"] else CapabilityStatus.UNAVAILABLE,
            detail="Multi-sheet inspection with cached formula results; hidden sheets skipped.",
            requires=["openpyxl"], tested=True,
        ),
        Capability(
            key="profile.xls", title="Legacy Excel (.xls) profiling",
            status=CapabilityStatus.IMPLEMENTED if p["xlrd"] else CapabilityStatus.NOT_CONFIGURED,
            detail="Requires the 'xlrd' package. Not installed here, so .xls uploads are "
                   "rejected with instructions to re-save as .xlsx rather than failing "
                   "obscurely.",
            requires=["xlrd"], tested=True,
        ),
        Capability(
            key="prep.deterministic_transforms", title="Deterministic transformations",
            status=CapabilityStatus.IMPLEMENTED,
            detail="Executed by a closed allowlist of typed operations. No generated code is "
                   "evaluated.", requires=["pandas"], tested=True,
        ),
        Capability(
            key="prep.power_query_m", title="Power Query M generation",
            status=CapabilityStatus.IMPLEMENTED,
            detail="Each executed step emits the equivalent M so the PBIP project reproduces "
                   "the same transformation inside Power BI.", tested=True,
        ),
        Capability(
            key="model.tmdl", title="TMDL semantic model generation",
            status=CapabilityStatus.IMPLEMENTED,
            detail="Tables, columns, measures, relationships and partitions written as TMDL, "
                   "the documented text format Power BI Desktop reads.", tested=True,
        ),
        Capability(
            key="model.tom", title="Model editing via the Tabular Object Model",
            status=CapabilityStatus.UNAVAILABLE,
            detail="TOM/AMO needs the .NET Analysis Services client libraries, which are not "
                   "present (no dotnet runtime on this host) and are Windows-oriented. Model "
                   "authoring therefore goes through TMDL text, not TOM.",
            requires=["dotnet", "Microsoft.AnalysisServices.Tabular"],
        ),
        Capability(
            key="dax.parse", title="DAX parsing and syntax validation",
            status=CapabilityStatus.IMPLEMENTED,
            detail="Hand-written lexer and recursive-descent parser; rejects malformed "
                   "expressions and bad argument counts.", tested=True,
        ),
        Capability(
            key="dax.reference_check", title="Model-aware DAX reference validation",
            status=CapabilityStatus.IMPLEMENTED,
            detail="Resolves every table, column, measure and function against the approved "
                   "model and a function catalogue, so fabricated names are caught.",
            tested=True,
        ),
        Capability(
            key="dax.evaluate", title="DAX evaluation over real data (supported subset)",
            status=CapabilityStatus.IMPLEMENTED if p["duckdb"] else CapabilityStatus.NOT_CONFIGURED,
            detail="Measures within the documented subset are transpiled to SQL and executed "
                   "against the prepared data in DuckDB, producing real numbers QA can check. "
                   "Anything outside the subset is reported as 'not evaluated', never as "
                   "passing. See docs/DAX_SUBSET.md.",
            requires=["duckdb"], tested=True,
        ),
        Capability(
            key="dax.analysis_services", title="DAX execution on the Power BI engine",
            status=CapabilityStatus.UNAVAILABLE,
            detail="There is no Analysis Services / VertiPaq engine on Linux. No result in this "
                   "system is ever labelled engine-validated.",
            requires=["Power BI Desktop or an XMLA endpoint"],
        ),
        Capability(
            key="report.pbir", title="PBIR report definition generation",
            status=CapabilityStatus.IMPLEMENTED,
            detail="Pages, visual containers, field bindings and theme written as the PBIP "
                   "report definition.", tested=True,
        ),
        Capability(
            key="export.pbip", title="Editable PBIP project export",
            status=CapabilityStatus.IMPLEMENTED,
            detail="A complete .pbip project (semantic model + report) is written and zipped "
                   "for download. It opens and is fully editable in Power BI Desktop.",
            tested=True,
        ),
        Capability(
            key="export.pbix", title="PBIX file generation",
            status=CapabilityStatus.UNAVAILABLE,
            detail="A .pbix is a proprietary container that in practice only Power BI Desktop "
                   "writes, and Desktop is Windows-only. No .pbix is produced here and none is "
                   "claimed. The PBIP project is the editable deliverable; saving it from "
                   "Desktop produces a .pbix if one is needed.",
            requires=["Power BI Desktop (Windows)"],
        ),
        Capability(
            key="powerbi.rest", title="Power BI service operations (publish, refresh)",
            status=CapabilityStatus.IMPLEMENTED if s.powerbi_configured
            else CapabilityStatus.NOT_CONFIGURED,
            detail="Needs POWERBI_TENANT_ID, POWERBI_CLIENT_ID and POWERBI_CLIENT_SECRET. "
                   "Not configured here, so nothing is published and no dataset is refreshed.",
            requires=["POWERBI_TENANT_ID", "POWERBI_CLIENT_ID", "POWERBI_CLIENT_SECRET"],
        ),
        Capability(
            key="llm.anthropic", title="LLM reasoning via Anthropic",
            status=CapabilityStatus.IMPLEMENTED if (p["anthropic"] and s.llm_configured)
            else CapabilityStatus.NOT_CONFIGURED,
            detail="Set ANTHROPIC_API_KEY to enable. Until then the heuristic planner runs and "
                   "every artifact it produces is stamped origin='heuristic'.",
            requires=["ANTHROPIC_API_KEY"], tested=True,
        ),
        Capability(
            key="llm.heuristic", title="Deterministic heuristic planner (development mode)",
            status=CapabilityStatus.IMPLEMENTED,
            detail="A rule-based stand-in so the full pipeline runs with no API key. It is a "
                   "development mode, not a language model, and is labelled as such wherever "
                   "its output appears.", tested=True,
        ),
        Capability(
            key="qa.deterministic", title="Deterministic validation suite",
            status=CapabilityStatus.IMPLEMENTED,
            detail="Row-count reconciliation, referential integrity, cardinality, double-count "
                   "detection, totals reconciliation and visual binding checks, all computed "
                   "from the real data.", tested=True,
        ),
    ]
    return caps


def capability(key: str) -> Capability | None:
    return next((c for c in capabilities() if c.key == key), None)


def is_usable(key: str) -> bool:
    c = capability(key)
    return bool(c and c.status.usable)


def require(key: str) -> Capability:
    """Raise with the capability's own explanation when something unavailable
    is attempted. Used to keep failure messages truthful and specific."""
    c = capability(key)
    if c is None:
        raise RuntimeError(f"unknown capability: {key}")
    if not c.status.usable:
        raise RuntimeError(f"{c.title} is {c.status.value}: {c.detail}")
    return c


def environment_summary() -> dict[str, object]:
    p = _probe()
    s = get_settings()
    caps = capabilities()
    return {
        "probes": p,
        "database": "postgresql" if s.is_postgres else "sqlite",
        "storage_backend": s.storage_backend,
        "llm_provider_active": "anthropic" if (p["anthropic"] and s.llm_configured) else "heuristic",
        "llm_is_development_mode": not s.llm_configured,
        "powerbi_desktop_available": False,
        "powerbi_desktop_note": "Power BI Desktop is Windows-only and is not present; see "
                                "docs/INTEGRATION_LIMITATIONS.md.",
        "counts": {
            st.value: sum(1 for c in caps if c.status is st) for st in CapabilityStatus
        },
    }
