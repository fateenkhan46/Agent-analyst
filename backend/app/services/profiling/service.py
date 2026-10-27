"""Ingestion and profiling, end to end.

``ingest_upload`` validates and stores the bytes immutably; ``profile_project``
reads every data source back out, profiles it, measures cross-table key
overlap and persists a ``SourceDataProfile``.

The original bytes are never rewritten, and a source whose rows were capped
says so in its issues rather than quietly reporting a smaller row count as if
it were the whole file.
"""
from __future__ import annotations

import re
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ...config import get_settings
from ...schemas.common import Origin, Provenance, Severity, utcnow
from ...schemas.agent import AuditAction
from ...schemas.source import (
    IngestStatus, SourceDataProfile, SourceFile, SourceKind, SourceTable,
)
from .. import repository as repo
from ..storage import get_store, project_key, safe_segment, sha256_bytes
from .keys import cross_table_issues, find_key_overlaps
from .profiler import profile_table
from .readers import DATA_KINDS, ReadResult, UnsupportedSourceError, detect_kind, read_source

#: Extensions we accept at all. Anything else is rejected before a byte is
#: parsed -- we do not try to sniff arbitrary binaries.
ALLOWED_SUFFIXES = {
    ".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".json",
    ".md", ".vtt", ".srt",
}
#: Magic-byte prefixes that must never be accepted, whatever the extension
#: claims. Guards against an executable renamed to .csv.
FORBIDDEN_MAGIC = (
    b"MZ",            # Windows PE
    b"\x7fELF",       # ELF
    b"\xca\xfe\xba\xbe",  # Mach-O fat / Java class
    b"%PDF",
)
ZIP_MAGIC = b"PK\x03\x04"


class UploadRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_upload(filename: str, data: bytes) -> SourceKind:
    """Strict pre-parse validation. Raises ``UploadRejected`` with a reason
    safe to show a user (no paths, no internals)."""
    settings = get_settings()
    if not data:
        raise UploadRejected("The file is empty.")
    if len(data) > settings.max_upload_bytes:
        mb = settings.max_upload_bytes / (1024 * 1024)
        raise UploadRejected(f"The file is larger than the {mb:.0f} MB upload limit.")

    name = filename.strip()
    if not name:
        raise UploadRejected("The upload has no filename.")
    if "\x00" in name:
        raise UploadRejected("The filename contains invalid characters.")
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix not in ALLOWED_SUFFIXES:
        raise UploadRejected(
            f"'{suffix or name}' is not an accepted file type. Accepted: "
            + ", ".join(sorted(ALLOWED_SUFFIXES))
        )

    head = data[:8]
    for magic in FORBIDDEN_MAGIC:
        if head.startswith(magic):
            raise UploadRejected(
                "The file content does not match its extension (it looks like an "
                "executable or PDF). Upload was refused."
            )
    kind = detect_kind(name)
    # .xlsx is a zip; a non-zip .xlsx is a mislabelled file.
    if kind is SourceKind.XLSX and not data.startswith(ZIP_MAGIC):
        raise UploadRejected(
            "The file is named .xlsx but is not a valid Excel workbook. If it is really a "
            "CSV, rename it to .csv and upload it again."
        )
    if kind is SourceKind.CSV and data.startswith(ZIP_MAGIC):
        raise UploadRejected(
            "The file is named .csv but contains zip/Excel data. Rename it to .xlsx."
        )
    # Text formats must actually be text. A NUL byte means binary content
    # wearing a text extension, which the CSV reader would turn into garbage
    # columns rather than fail on.
    if kind in {SourceKind.CSV, SourceKind.JSON, SourceKind.TRANSCRIPT, SourceKind.BRIEF} \
            and b"\x00" in data[:65_536]:
        raise UploadRejected(
            "The file has a text extension but contains binary data. Upload was refused."
        )
    return kind


def slug_table_name(filename: str, sheet: str, taken: set[str]) -> str:
    """A stable, unique, SQL- and TMDL-safe logical name."""
    base = re.sub(r"\.[^.]+$", "", filename)
    parts = [base] + ([sheet] if sheet else [])
    name = "_".join(parts)
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "table"
    if name[0].isdigit():
        name = f"t_{name}"
    candidate, n = name, 2
    while candidate in taken:
        candidate = f"{name}_{n}"
        n += 1
    taken.add(candidate)
    return candidate


def ingest_upload(
    s: Session, *, project_id: str, filename: str, data: bytes,
    content_type: str = "", actor: str = "system",
) -> SourceFile:
    """Validate, store immutably, register. Does not parse the contents."""
    settings = get_settings()
    if repo.count_source_files(s, project_id) >= settings.max_files_per_project:
        raise UploadRejected(
            f"This project already has the maximum of {settings.max_files_per_project} files."
        )

    digest = sha256_bytes(data)
    existing = repo.find_duplicate_upload(s, project_id, digest)
    if existing:
        repo.record_audit(
            s, project_id=project_id, actor=actor, action=AuditAction.FILE_UPLOADED,
            summary=f"Re-upload of identical file '{filename}' ignored; kept {existing.original_filename}.",
            target=existing.id, detail={"sha256": digest},
        )
        return existing

    try:
        kind = validate_upload(filename, data)
    except UploadRejected as exc:
        rejected = SourceFile(
            project_id=project_id, original_filename=filename, stored_path="",
            kind=detect_kind(filename), content_type=content_type,
            size_bytes=len(data), sha256=digest,
            status=IngestStatus.REJECTED, rejection_reason=exc.reason, uploaded_by=actor,
        )
        repo.save_source_file(s, rejected)
        repo.record_audit(
            s, project_id=project_id, actor=actor, action=AuditAction.FILE_REJECTED,
            summary=f"Rejected '{filename}': {exc.reason}", target=rejected.id,
            severity=Severity.MEDIUM, detail={"reason": exc.reason},
        )
        return rejected

    key = project_key(project_id, "uploads", f"{digest[:8]}_{safe_segment(filename)}")
    get_store().put_bytes(key, data)
    sf = SourceFile(
        project_id=project_id, original_filename=filename, stored_path=key, kind=kind,
        content_type=content_type, size_bytes=len(data), sha256=digest,
        status=IngestStatus.UPLOADED, uploaded_by=actor,
    )
    repo.save_source_file(s, sf)
    repo.record_audit(
        s, project_id=project_id, actor=actor, action=AuditAction.FILE_UPLOADED,
        summary=f"Uploaded '{filename}' ({len(data):,} bytes, {kind.value}).",
        target=sf.id, detail={"sha256": digest, "kind": kind.value, "stored_path": key},
    )
    return sf


def load_frames(
    s: Session, project_id: str, *, actor: str = "system"
) -> tuple[dict[str, pd.DataFrame], list[tuple[SourceFile, str, ReadResult]], list[SourceFile]]:
    """Read every usable data source back into DataFrames.

    Returns (frames_by_table_name, [(file, table_name, read_result)], failures).
    """
    settings = get_settings()
    store = get_store()
    frames: dict[str, pd.DataFrame] = {}
    reads: list[tuple[SourceFile, str, ReadResult]] = []
    failures: list[SourceFile] = []
    taken: set[str] = set()

    for sf in repo.list_source_files(s, project_id):
        if sf.kind not in DATA_KINDS or sf.status is IngestStatus.REJECTED:
            continue
        try:
            data = store.get_bytes(sf.stored_path)
            results = read_source(data, sf.original_filename, sf.kind, settings.max_rows_profiled)
        except (UnsupportedSourceError, FileNotFoundError, ValueError) as exc:
            sf.status = IngestStatus.FAILED
            sf.rejection_reason = str(exc)
            repo.save_source_file(s, sf)
            repo.record_audit(
                s, project_id=project_id, actor=actor, action=AuditAction.ERROR,
                summary=f"Could not read '{sf.original_filename}': {exc}",
                target=sf.id, severity=Severity.HIGH,
            )
            failures.append(sf)
            continue
        repo.record_audit(
            s, project_id=project_id, actor=actor, action=AuditAction.FILE_READ,
            summary=f"Read '{sf.original_filename}' ({len(results)} table(s)).", target=sf.id,
        )
        for res in results:
            name = slug_table_name(sf.original_filename, res.sheet_name, taken)
            frames[name] = res.frame
            reads.append((sf, name, res))
    return frames, reads, failures


def profile_project(
    s: Session, project_id: str, *, actor: str = "data_profiling_agent"
) -> SourceDataProfile:
    """Profile every data source and measure cross-table keys."""
    settings = get_settings()
    frames, reads, failures = load_frames(s, project_id, actor=actor)

    tables: list[SourceTable] = []
    for sf, table_name, res in reads:
        t = profile_table(
            res.frame, project_id=project_id, source_file_id=sf.id,
            table_name=table_name,
            display_name=f"{sf.original_filename}" + (f" / {res.sheet_name}" if res.sheet_name else ""),
            sheet_name=res.sheet_name,
            preview_limit=settings.preview_row_limit,
            reader_notes=res.notes,
            header_row_index=res.header_row_index,
        )
        if res.truncated:
            from ...schemas.common import Issue
            t.issues.append(Issue(
                code="table.row_cap_reached", severity=Severity.HIGH, location=table_name,
                message=f"Only the first {len(res.frame):,} of {res.total_rows_in_source:,} rows "
                        f"were profiled (row cap).",
                evidence={"profiled_rows": len(res.frame),
                          "total_rows": res.total_rows_in_source},
                recommended_fix="Raise MAX_ROWS_PROFILED or split the file. Totals computed from "
                                "this profile are NOT comparable to the full source.",
            ))
        tables.append(t)
        sf.status = IngestStatus.PROFILED
        repo.save_source_file(s, sf)

    overlaps = find_key_overlaps(tables, frames)
    issues = cross_table_issues(tables, overlaps)

    for sf in failures:
        from ...schemas.common import Issue
        issues.append(Issue(
            code="source.unreadable", severity=Severity.HIGH, location=sf.original_filename,
            message=f"'{sf.original_filename}' could not be read: {sf.rejection_reason}",
            recommended_fix="Fix or re-export the file and upload it again.",
        ))

    profile = SourceDataProfile(
        project_id=project_id, tables=tables, key_overlaps=overlaps,
        cross_table_issues=issues, generated_at=utcnow(),
        provenance=Provenance(
            origin=Origin.DETERMINISTIC, produced_by="data_profiling_agent",
            rationale="Every statistic is computed from the stored source bytes by "
                      "app.services.profiling; no language model is involved.",
            evidence=[
                f"{len(tables)} table(s) profiled across {len(reads)} source read(s).",
                f"{sum(t.row_count for t in tables):,} total rows inspected.",
                f"{len(overlaps)} candidate key relationship(s) measured.",
            ],
        ),
    )
    repo.save_artifact(s, project_id, repo.ArtifactKind.PROFILE, profile)
    p = repo.get_project(s, project_id)
    if p:
        p.profile_id = profile.id
        repo.save_project(s, p)
    repo.record_audit(
        s, project_id=project_id, actor=actor, action=AuditAction.PROFILE_GENERATED,
        summary=f"Profiled {len(tables)} table(s), {profile.total_rows:,} rows; "
                f"{sum(profile.issue_counts().values())} finding(s).",
        target=profile.id, detail={"issue_counts": profile.issue_counts()},
    )
    return profile


def preview_table(
    s: Session, project_id: str, table_name: str, *, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """Read a fresh slice of rows straight from the stored source."""
    frames, _, _ = load_frames(s, project_id)
    frame = frames.get(table_name)
    if frame is None:
        raise KeyError(table_name)
    window = frame.iloc[offset: offset + limit]
    return {
        "table_name": table_name,
        "columns": [str(c) for c in frame.columns],
        "total_rows": int(frame.shape[0]),
        "offset": offset,
        "rows": [
            {str(k): ("" if v is None else str(v)) for k, v in rec.items()}
            for rec in window.to_dict(orient="records")
        ],
    }
