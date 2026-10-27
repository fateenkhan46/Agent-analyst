#!/usr/bin/env python3
"""Create the sample project over the HTTP API and run the whole pipeline.

Driving this through HTTP rather than the service layer means the endpoints the
frontend uses are the ones exercised here. ``--auto-approve`` answers the
approval gates so the script can run unattended; without it the run stops at
the first gate, which is the normal behaviour.

Usage:
    python scripts/seed_sample_project.py [--base http://127.0.0.1:8000] [--auto-approve]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "samples" / "sample_project"
DATA_FILES = ["project_register.csv", "project_costs.xlsx", "labour_hours.csv"]
TRANSCRIPT = "requirements_workshop_transcript.txt"
#: The KPIs documented in samples/EXPECTED.md, with the expected values QA
#: cross-checks against a sum computed directly from the prepared data.
EXPECTED_KPIS = [
    "Total Cost", "Total Budget", "Budget Variance", "Budget Variance %",
    "Total Labour Hours", "Average Cost per Project", "Project Completion %",
    "Year-to-Date Cost",
]
BRIEF = "reporting_brief.md"


def call(base: str, method: str, path: str, body: object = None) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}/api/v1{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as res:
            raw = res.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"{method} {path} -> {exc.code}: {detail[:600]}") from None


def upload(base: str, project_id: str, paths: list[pathlib.Path]) -> dict:
    """multipart/form-data, hand-built so the script needs no dependencies."""
    boundary = "----agentanalystseed"
    parts: list[bytes] = []
    for p in paths:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{p.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + p.read_bytes()
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(parts)
    req = urllib.request.Request(
        f"{base}/api/v1/projects/{project_id}/files",
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as res:
        return json.loads(res.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--auto-approve", action="store_true")
    args = ap.parse_args()

    missing = [f for f in [*DATA_FILES, TRANSCRIPT] if not (SAMPLES / f).exists()]
    if missing:
        raise SystemExit(
            f"Missing sample files: {', '.join(missing)}. Run samples/generate.py first."
        )

    brief_text = (SAMPLES / BRIEF).read_text() if (SAMPLES / BRIEF).exists() else ""
    transcript = (SAMPLES / TRANSCRIPT).read_text()

    created = call(
        args.base, "POST", "/projects",
        {
            "name": "FY25 Delivery Cost Review (sample)",
            "description": brief_text[:2000],
            "business_objective": (
                "Report delivery cost against budget by month and project so the "
                "steering group can see overruns early."
            ),
            "reporting_period": "FY25",
            "currency": "USD",
            "units": "hours",
            # Stated explicitly rather than left to be inferred from prose.
            # Without a live model the requirements agent will not invent a KPI
            # it cannot ground, so an unattended seed has to name them -- these
            # are the eight documented in samples/EXPECTED.md.
            "desired_kpis": EXPECTED_KPIS,
            "reporting_questions": [
                "Which projects are over budget?",
                "What is the monthly cost trend?",
                "How much labour has each project consumed?",
            ],
            "meeting_transcript": transcript,
        },
    )
    project_id = created["project"]["id"]
    print(f"project: {project_id}")

    result = upload(args.base, project_id, [SAMPLES / f for f in DATA_FILES])
    print(f"uploaded: {len(result['accepted'])} accepted, {len(result['rejected'])} rejected")
    for r in result["rejected"]:
        print(f"  rejected {r['filename']}: {r['reason']}")

    run = call(
        args.base, "POST", f"/projects/{project_id}/run",
        {"auto_approve": bool(args.auto_approve)},
    )
    for step in run["steps"]:
        mark = "ok  " if step.get("succeeded") else "FAIL"
        extra = step.get("reason") or step.get("error") or ""
        print(f"  {mark} {step['kind']}{(' - ' + str(extra)) if extra else ''}")
    print(f"progress: {run['progress']:.0%}")
    if run["pending_approvals"]:
        print(f"pending approvals: {len(run['pending_approvals'])}")
        for a in run["pending_approvals"]:
            print(f"  [{a['severity']}] {a['question']}")

    failed = [s for s in run["steps"] if not s.get("succeeded")]
    print(f"\nOpen the project at: /projects/{project_id}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
