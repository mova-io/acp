"""Per-file packet export, fed by the REAL store and the REAL routes (parent review item 5).

The packet exporter (frontend/src/reportPacketExport.js) compares each scan-index row's
`factsDigest` with a FRESH per-file facts read, and the render route re-verifies that digest.
That only works if the two routes project the facts identically (contract 3a). A hand-built
fixture would agree with whatever the exporter expects; so this test builds a real multi-file scan
in the isolated store — nested, Unicode, case-colliding, traversal and reserved names, an
unanalysable file, an unassessed one, saved changes, and an earlier scan for comparison — serves it
through the production app with TestClient, and asserts over HTTP:

  * the paged scan index (several pages) carries every document exactly once, digest-coherent;
  * every index row's factsDigest EQUALS the per-file route's factsDigest for the same evidence;
  * the render route accepts exactly that digest (200, a PDF) and refuses it once the evidence
    moves (409) — the exporter's "changed during export" is the server's answer, not a guess;
  * the exporter's ONE final fresh read (contract 7: offset 0, limit 1, no digest) returns the
    snapshot digest for unchanged evidence, and a different one once a reviewer decision is
    recorded through the real PUT after every per-file read (the `finalCheck` section);

and it records the actual responses to tests/fixtures/report_packet_real_store.json, which
frontend/src/reportPacketRealStore.test.js replays through the real exporter. Regenerate with

    ACP_REGEN_PACKET_FIXTURE=1 python -m pytest tests/test_report_packet_real_store.py

(the default run checks the checked-in recording has the same shape instead of rewriting it,
because digests include store timestamps and so differ between runs).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

FIXTURE = ACP / "tests" / "fixtures" / "report_packet_real_store.json"
OWNER = "owner@example.com"
SID = "s-packets"
PREV = "s-packets-prev"
PAGE = 3

FILES = [
    # (name, status, score, issues, checksum)
    ("Policies/2026/Überblick – Richtlinie.docx", "uncertain", 72, [
        {"ruleId": "DOCX-ALT-001", "wcag": "1.1.1 Non-text Content", "severity": "SERIOUS",
         "detail": "Image 1 has no description", "location": "docx:image:1"},
        {"ruleId": "DOCX-HEAD-002", "wcag": "SC_1_3_1", "severity": "MODERATE",
         "detail": "Paragraph styled as a heading is not a heading", "location": "docx:paragraph:14"},
    ], "md5-uber-1"),
    ("Reports/Q1 summary.pdf", "uncertain", 55, [
        {"ruleId": "PDF-CONTRAST-001", "wcag": "1.4.3", "severity": "SERIOUS",
         "detail": "Grey text on white is 2.9:1", "page": 3},
        {"ruleId": "PDF-LANG-001", "wcag": "3.1.1", "severity": "SERIOUS",
         "detail": "Document language is not set"},
    ], "sha256:" + "ab" * 32),
    ("reports/q1 SUMMARY.pdf", "uncertain", 90, [
        {"ruleId": "PDF-TITLE-001", "wcag": "2.4.2", "severity": "MODERATE",
         "detail": "Document title is not set"},
    ], "md5-q1-lower"),
    ("../../outside/escape.xlsx", "uncertain", 80, [
        {"ruleId": "XLSX-HEADER-001", "wcag": "1.3.1", "severity": "MODERATE",
         "detail": "Table has no header row", "location": "xlsx:sheet:Budget:cell:B3"},
    ], "md5-escape"),
    ("CON.pptx", "uncertain", 64, [
        {"ruleId": "PPTX-ALT-001", "wcag": "1.1.1", "severity": "SERIOUS",
         "detail": "Picture has no alternative text", "page": 2, "location": "pptx:slide:1"},
    ], "md5-con"),
    ("Budget & notes (draft).xlsx", "uncertain", 100, [], "md5-budget"),
    ("broken.docx", "error", None, [], None),
    ("not-yet.pdf", "discovered", None, [], None),
]

# A separate scan, names disjoint from FILES (so neither scan is a baseline for the other), in
# which every document is analysable — the only way a real recording can reach COMPLETE.
CLEAN = "s-packets-clean"
CLEAN_FILES = [
    ("Clean/Minutes – March.docx", "uncertain", 88, [
        {"ruleId": "DOCX-HEAD-002", "wcag": "SC_1_3_1", "severity": "MODERATE",
         "detail": "Paragraph styled as a heading is not a heading", "location": "docx:paragraph:3"},
    ], "md5-clean-minutes"),
    ("Clean/Plan.pdf", "uncertain", 100, [], "md5-clean-plan"),
]


def _save(store, sid, *, completed, files):
    store.save_scan({
        "_scan_id": sid,
        "started_at": "2026-09-01T05:00:00+00:00",
        "completed_at": completed,
        "source": "local", "owner": OWNER,
        "rubric": {"name": "wcag-aa", "hash": "h1"},
        "summary": {"files": len(files), "certifiable": 0, "uncertain": len(files), "error": 0,
                    "avg_score": 70},
        "files": [{"file": name, "engine": ".net/office", "status": status, "score": score,
                   "compliant": 0, "skipped_rules": 0, "issues": list(issues), "checksum": checksum,
                   "drive_file_id": None} for name, status, score, issues, checksum in files],
    })


@pytest.fixture()
def client(monkeypatch, isolated_store):
    import core
    from fastapi.testclient import TestClient
    from app import app

    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "ACCESS_CODE", "", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "test-client-id", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    monkeypatch.setattr(core, "verify_gis_token", lambda tok: tok or None)
    monkeypatch.setattr(core, "email_allowed", lambda e: e == OWNER)
    monkeypatch.setattr(core, "PUBLIC_URL", "https://acp.example.com", raising=False)
    monkeypatch.setenv("ACP_BUILD_VERSION", "2026.9.17.9")
    # an earlier scan of the same documents, so index rows carry a comparison
    _save(isolated_store, PREV, completed="2026-08-01T05:05:00+00:00", files=[
        (n, s, sc, iss[:1], ck) for n, s, sc, iss, ck in FILES if s != "discovered"])
    _save(isolated_store, SID, completed="2026-09-01T05:05:00+00:00", files=FILES)
    uber = FILES[0][0]
    q1 = FILES[1][0]
    isolated_store.record_remediation(SID, uber, corrected_sha256="c" * 64)
    isolated_store.record_remediation_diffs(SID, uber, [
        {"rule_id": "1.1.1", "before": "", "after": "Organisation chart: three directors report to the board.",
         "note": "vision"}])
    isolated_store.record_remediation(SID, q1, corrected_sha256="d" * 64)
    _save(isolated_store, CLEAN, completed="2026-09-02T05:05:00+00:00", files=CLEAN_FILES)
    made = TestClient(app)
    made.acp_store = isolated_store
    return made


AUTH = {"Authorization": f"Bearer {OWNER}"}


def _get(client, path):
    r = client.get(path, headers=AUTH)
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    return r.json()


def _file_path(name, suffix, sid=SID):
    return f"/scans/{sid}/files/{quote(name, safe='')}/{suffix}"


def final_read(sid=SID):
    """Contract 7's one fresh read: offset 0, limit 1, NO digest (the server always rebuilds it)."""
    return f"/scans/{sid}/report-facts?offset=0&limit=1"


FINAL_READ = final_read()


def record_scan(client, sid) -> dict:
    """Every per-scan response the packet exporter reads, in the order it reads them."""
    scan = _get(client, f"/scans/{sid}")
    pages = []
    first = _get(client, f"/scans/{sid}/report-facts?offset=0&limit={PAGE}")
    pages.append({"offset": 0, "limit": PAGE, "digest": None, "response": first})
    digest = first["factsDigest"]
    offset = len(first["files"])
    while not first.get("complete") and offset < first["filesTotal"]:
        page = _get(client, f"/scans/{sid}/report-facts?offset={offset}&limit={PAGE}&digest={digest}")
        pages.append({"offset": offset, "limit": PAGE, "digest": digest, "response": page})
        if not page["files"]:
            break
        offset += len(page["files"])
    names = [row["file"] for p in pages for row in p["response"]["files"]]
    per_file = {name: _get(client, _file_path(name, "report-facts", sid)) for name in names}
    remediation_state = {name: _get(client, _file_path(name, "remediation-state", sid)) for name in names}
    dispositions = {name: _get(client, _file_path(name, "dispositions", sid)) for name in names}
    # Contract 7: after every per-document read, the exporter reads the index ONCE more — offset 0,
    # limit 1, NO digest, which the server always rebuilds from the store — and compares digests.
    unchanged = _get(client, final_read(sid))
    return {
        "scanId": sid,
        "scan": {"files": scan.get("files") or []},
        "scanFactsPages": pages,
        "fileFacts": per_file,
        "remediationState": remediation_state,
        "dispositions": dispositions,
        "decisions": _get(client, f"/scans/{sid}/decisions"),
        # `mutated` is added by record_mutation() — only where the test means to move the evidence.
        "finalCheck": {"unchanged": unchanged},
    }


def record(client) -> dict:
    """Every response the packet exporter reads, exactly as the routes answer them."""
    main = record_scan(client, SID)
    return {
        "_comment": "Recorded by tests/test_report_packet_real_store.py from the real store and routes; "
                    "replayed by frontend/src/reportPacketRealStore.test.js. Do not edit by hand.",
        "pageLimit": PAGE,
        **main,
        # the scan-wide inputs FileDrawer reads before it builds a report
        "rules": _get(client, "/rules"),
        "rubric": _get(client, "/rubric"),
        "capability": _get(client, "/capability"),
        # A second scan in the same store whose every document CAN have a packet: the main scan's
        # unanalysable broken.docx (correctly) keeps it from ever reading COMPLETE, and COMPLETE is
        # the verdict the final check has to be shown to allow.
        "clean": record_scan(client, CLEAN),
    }


def record_mutation(client, rec) -> dict:
    """Move the evidence AFTER the snapshot and every per-file read, the way a reviewer does: a
    decision on the Unicode document's saved change, recorded through the real PUT. Then make the
    exporter's final read again. Returns that response."""
    uber = FILES[0][0]
    facts = rec["fileFacts"][uber]
    change = facts["savedChanges"][0]
    path = (f"/scans/{SID}/files/{quote(uber, safe='')}/change-reviews/"
            f"{quote(change['id'], safe='')}")
    r = client.put(path, headers=AUTH, json={
        "verdict": "accepted", "note": "Checked against the chart.",
        "change_digest": change["changeDigest"],
        "expected_sha256": facts["identity"]["correctedSha256"]})
    assert r.status_code == 200, r.text[:300]
    return _get(client, FINAL_READ)


def test_the_paged_index_lists_every_document_once_across_several_pages(client):
    rec = record(client)
    assert len(rec["scanFactsPages"]) >= 3, "the fixture must exercise more than one page"
    names = [row["file"] for p in rec["scanFactsPages"] for row in p["response"]["files"]]
    assert sorted(names) == sorted(n for n, *_ in FILES)
    assert len(names) == len(set(names))
    digests = {p["response"]["factsDigest"] for p in rec["scanFactsPages"]}
    assert len(digests) == 1, "every page carries the same full-index digest"


def test_every_index_row_digest_equals_the_per_file_route_digest(client):
    """Contract 3a. If this fails, every packet would read 'changed during export'."""
    rec = record(client)
    rows = [row for p in rec["scanFactsPages"] for row in p["response"]["files"]]
    mismatched = [(r["file"], r["factsDigest"], rec["fileFacts"][r["file"]]["factsDigest"])
                  for r in rows if r["factsDigest"] != rec["fileFacts"][r["file"]]["factsDigest"]]
    assert mismatched == []


def _render(client, name, digest):
    body = {"kind": "file", "file": name, "mode": "summary", "factsDigest": digest,
            "model": {"docTitle": f"Report — {name}", "lang": "en-US",
                      "blocks": [{"k": "heading", "text": "Decision summary"}]}}
    return client.post(f"/scans/{SID}/report-render", content=json.dumps(body),
                       headers={**AUTH, "Content-Type": "application/json"})


def test_the_render_route_accepts_the_index_digest_and_refuses_it_after_the_evidence_moves(client):
    rec = record(client)
    rows = {row["file"]: row for p in rec["scanFactsPages"] for row in p["response"]["files"]}
    for name, row in rows.items():
        r = _render(client, name, row["factsDigest"])
        assert r.status_code == 200, (name, r.status_code, r.text[:200])
        assert r.content.startswith(b"%PDF")
    # a reviewer note, a new corrected copy — anything the facts cover — moves the digest
    uber = FILES[0][0]
    client.acp_store.record_remediation(SID, uber, corrected_sha256="e" * 64)
    assert _render(client, uber, rows[uber]["factsDigest"]).status_code == 409
    fresh = _get(client, _file_path(uber, "report-facts"))["factsDigest"]
    assert fresh != rows[uber]["factsDigest"]


def test_the_final_fresh_read_matches_the_snapshot_until_the_evidence_moves(client):
    """Contract 7 on the real store: the exporter's one final read (no digest) is rebuilt fresh,
    equals the snapshot digest for unchanged evidence, and moves when a reviewer decision is
    recorded after the snapshot — even though every per-file read was already made."""
    rec = record(client)
    snap = rec["scanFactsPages"][0]["response"]
    unchanged = rec["finalCheck"]["unchanged"]
    assert unchanged["factsDigest"] == snap["factsDigest"]
    assert unchanged["filesTotal"] == snap["filesTotal"]
    assert unchanged["snapshot"]["servedFrom"] == "fresh", "a request without digest must never be memo"
    mutated = record_mutation(client, rec)
    assert mutated["snapshot"]["servedFrom"] == "fresh"
    assert mutated["factsDigest"] != snap["factsDigest"], "a recorded decision must move the scan digest"
    assert mutated["filesTotal"] == snap["filesTotal"]
    # the moved digest is the store's current truth: the per-file route for that document moved too
    uber = FILES[0][0]
    assert _get(client, _file_path(uber, "report-facts"))["factsDigest"] != rec["fileFacts"][uber]["factsDigest"]


def test_the_recording_the_frontend_replays_matches_the_routes(client):
    rec = record(client)
    rec["finalCheck"]["mutated"] = record_mutation(client, rec)
    if os.environ.get("ACP_REGEN_PACKET_FIXTURE") == "1" or not FIXTURE.exists():
        FIXTURE.write_text(json.dumps(rec, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    saved = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # Same documents, same pagination, same keys on every row; and the saved recording keeps the
    # invariant the frontend test relies on.
    names = lambda r: [row["file"] for p in r["scanFactsPages"] for row in p["response"]["files"]]  # noqa: E731
    assert names(saved) == names(rec)
    assert [p["offset"] for p in saved["scanFactsPages"]] == [p["offset"] for p in rec["scanFactsPages"]]
    for got, want in zip((row for p in saved["scanFactsPages"] for row in p["response"]["files"]),
                         (row for p in rec["scanFactsPages"] for row in p["response"]["files"])):
        assert sorted(got) == sorted(want), got["file"]
        assert got["factsDigest"] == saved["fileFacts"][got["file"]]["factsDigest"], got["file"]
    assert sorted(saved["fileFacts"]) == sorted(rec["fileFacts"])
    # The final-check recording keeps the two facts the frontend replay relies on, and the keys
    # the routes answer with today.
    snap = saved["scanFactsPages"][0]["response"]["factsDigest"]
    assert saved["finalCheck"]["unchanged"]["factsDigest"] == snap
    assert saved["finalCheck"]["mutated"]["factsDigest"] != snap
    for kind in ("unchanged", "mutated"):
        assert sorted(saved["finalCheck"][kind]) == sorted(rec["finalCheck"][kind]), kind
    # The clean scan: every document analysable, row digest == per-file digest, final read equal.
    clean = saved["clean"]
    assert names(clean) == names(rec["clean"]) == [n for n, *_ in CLEAN_FILES]
    for row in (row for p in clean["scanFactsPages"] for row in p["response"]["files"]):
        assert row["factsDigest"] == clean["fileFacts"][row["file"]]["factsDigest"], row["file"]
    assert clean["finalCheck"]["unchanged"]["factsDigest"] == clean["scanFactsPages"][0]["response"]["factsDigest"]


# Found while building this fixture: api/app.py's gate judged request.url.path, which Starlette
# truncates at a DECODED '#' or '?', so a document named with either was 404 to its owner and
# answered as 'demo' without credentials. Fixed in the gate (ab3554a6, which judges the raw scope
# path); this is the packet exporter's end-to-end guard that it stays fixed.
@pytest.mark.parametrize("name", ["Budget #3.xlsx", "Why? notes.docx"])
def test_a_document_named_with_hash_or_question_mark_is_served_to_its_owner(client, name):
    _save(client.acp_store, "s-hash", completed="2026-09-01T05:05:00+00:00",
          files=[(name, "uncertain", 90, [], "md5-h")])
    r = client.get(f"/scans/s-hash/files/{quote(name, safe='')}/report-facts", headers=AUTH)
    assert r.status_code == 200, r.text
    # …and without credentials it must be refused by the gate, not answered as 'demo'
    anon = client.get(f"/scans/s-hash/files/{quote(name, safe='')}/report-facts")
    assert anon.status_code == 401
