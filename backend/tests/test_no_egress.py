"""The data must not leave the machine.

Without an LLM key configured, this system is supposed to be entirely local.
That is easy to claim and easy to break later -- one library that phones home,
one telemetry call added by a dependency, one agent that starts calling an API.

So the guarantee is enforced rather than documented: the whole pipeline runs
with every outbound socket and DNS lookup intercepted, on real sample data. If
a single byte would leave the machine, this test fails and names the host.
"""
from __future__ import annotations

import pathlib
import socket

import pytest

SAMPLES = pathlib.Path(__file__).resolve().parents[2] / "samples" / "sample_project"

#: A unix socket path, loopback, or an unresolved bind are all local.
_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


class EgressAttempted(AssertionError):
    pass


@pytest.fixture
def no_egress(monkeypatch):
    """Block and record every non-loopback connection and DNS lookup."""
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _is_external(addr) -> bool:
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        return str(host) not in _LOCAL and not str(host).startswith("/")

    def connect(self, addr):
        if _is_external(addr):
            attempts.append(str(addr))
            raise EgressAttempted(f"outbound connection to {addr}")
        return real_connect(self, addr)

    def connect_ex(self, addr):
        if _is_external(addr):
            attempts.append(str(addr))
            raise EgressAttempted(f"outbound connection to {addr}")
        return real_connect_ex(self, addr)

    def getaddrinfo(host, *a, **k):
        if host is not None and str(host) not in _LOCAL:
            attempts.append(f"DNS {host}")
            raise EgressAttempted(f"DNS lookup for {host}")
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts


def test_the_whole_pipeline_runs_without_touching_the_network(
    client, no_egress
) -> None:
    missing = [f for f in ("project_register.csv", "project_costs.xlsx",
                           "labour_hours.csv", "requirements_workshop_transcript.txt")
               if not (SAMPLES / f).exists()]
    if missing:
        pytest.skip(f"sample data not generated: {', '.join(missing)}")

    r = client.post("/api/v1/projects", json={
        "name": "Egress guard", "currency": "USD", "units": "hours",
        "reporting_period": "FY25",
        "business_objective": "Confirm the pipeline is entirely local.",
        "desired_kpis": ["Total Cost", "Total Budget", "Budget Variance",
                         "Budget Variance %", "Total Labour Hours",
                         "Average Cost per Project", "Project Completion %",
                         "Year-to-Date Cost"],
        "meeting_transcript": (SAMPLES / "requirements_workshop_transcript.txt").read_text(),
    })
    assert r.status_code == 201, r.text
    pid = r.json()["project"]["id"]

    uploads = [
        ("files", (f, (SAMPLES / f).read_bytes(), "application/octet-stream"))
        for f in ("project_register.csv", "project_costs.xlsx", "labour_hours.csv")
    ]
    assert client.post(f"/api/v1/projects/{pid}/files", files=uploads).status_code == 201

    run = client.post(f"/api/v1/projects/{pid}/run", json={"auto_approve": True})
    assert run.status_code == 200, run.text
    body = run.json()

    assert not no_egress, (
        "the pipeline tried to reach the network: " + ", ".join(sorted(set(no_egress)))
    )
    failed = [s for s in body["steps"] if not s.get("succeeded")]
    assert not failed, f"pipeline did not complete offline: {failed}"
    assert body["progress"] == 1.0


def test_the_guard_itself_catches_an_outbound_call(no_egress) -> None:
    """A guard that never fires proves nothing."""
    with pytest.raises(EgressAttempted):
        socket.create_connection(("example.com", 443), timeout=1)
    assert no_egress


def test_no_llm_key_means_the_heuristic_provider_is_active(client) -> None:
    env = client.get("/api/v1/system/capabilities").json()["environment"]
    assert env["llm_is_development_mode"] is True
    assert env["llm_provider_active"] != "anthropic"
