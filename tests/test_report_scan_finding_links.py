"""Contract 8: a SCAN report links every finding to its exact record — generated from the REAL store.

`GET /scans/{sid}/report-facts?include=findings` gives each index row the per-file facts' own
finding records (same server ids, same structured location, every occurrence). This file:

  * builds a small estate in a real SQLite store and reads it through the real routes (TestClient),
    with and without `include=findings`, and writes what the server answered to
    `frontend/src/__fixtures__/reportScanFindings.real.json` — the ONLY input of
    `frontend/src/scanReportFindingLinks.test.jsx`, which replays those pages through the real
    loadScanReportFacts → buildScanReportModel → reportHtmlFromModel and the evidence viewer;
  * proves contract 7 on the same data: the scan digest, every row digest and every other row key
    are identical with and without the include;
  * keeps the paging semantics (memo, 409, bounded pages) with the include;
  * renders the scan MODEL the browser built from those pages
    (`frontend/src/__fixtures__/reportScanFindings.model.real.json`, written by the JS test) with the
    SERVER renderer (report_render.render_html, base_url https://acp.example.com) and checks every
    finding link names a finding id that exists in that file's per-file facts.

CHECK MODE is the default for both fixtures. Regenerate, in this order:

    ACP_REGEN_REPORT_FIXTURES=1 python -m pytest tests/test_report_scan_finding_links.py
    cd frontend && ACP_REGEN_REPORT_FIXTURES=1 npx vitest run src/scanReportFindingLinks.test.jsx
    python -m pytest tests/test_report_scan_finding_links.py        # check mode, both fixtures
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

FIXTURE = ACP / "frontend/src/__fixtures__/reportScanFindings.real.json"
MODEL_FIXTURE = ACP / "frontend/src/__fixtures__/reportScanFindings.model.real.json"
OWNER = "reviewer@hosp.org"
SID = "s-2026-09-links"
FIXED_NOW = "2026-09-18T12:00:00+00:00"
BASE = "https://acp.example.com"

BOARD = "Board/Q1 #2 – Überblick.docx"          # nested, '#', non-ASCII, en dash
DECK = "decks/Q3 review & plan.pptx"
PDF = "reports/annual 100%.pdf"
SHEET = "sheets/budget.xlsx"
FIXED = "remediated/fixed.docx"


def _issue(rule, wcag, detail, location, page=None, severity="SERIOUS"):
    return {"ruleId": rule, "wcag": wcag, "severity": severity, "detail": detail,
            "page": page, "location": location}


ALT = "1.1.1 Non-text Content"


def _doc(name, issues, n):
    return {"file": name, "engine": ".net/office", "status": "uncertain", "score": 70,
            "compliant": 0, "skipped_rules": 0, "issues": issues, "checksum": f"md5-{n}",
            "drive_file_id": f"d-{n}"}


def build_estate(store):
    store.save_scan({
        "_scan_id": SID, "started_at": "2026-09-01T09:00:00+00:00",
        "completed_at": "2026-09-01T09:00:00+00:00", "source": "drive", "owner": OWNER,
        "rubric": {"name": "wcag-aa", "hash": "rubric-1"},
        "summary": {"files": 5, "certifiable": 0, "uncertain": 5, "error": 0, "avg_score": 70},
        "files": [
            # Two findings of ONE criterion with the SAME detail on two different objects, and a
            # finding with no recorded location: the three shapes a criterion-matched link gets wrong.
            _doc(BOARD, [_issue("DOCX-ALT-001", ALT, "Image has no description", "docx:image:1"),
                         _issue("DOCX-ALT-001", ALT, "Image has no description", "docx:image:2"),
                         _issue("DOCX-LANG-001", "3.1.1 Language of Page",
                                "Document language is not set", None)], 1),
            _doc(DECK, [_issue("PPTX-ALT-001", ALT, "Shape 5 has no alt text",
                               "pptx:slide:1:element:5", page=2)], 2),
            _doc(PDF, [_issue("PDF-ALT-001", ALT, "Figure has no alternate text", "Figure 2",
                              page=3)], 3),
            _doc(SHEET, [_issue("XLSX-LINK-001", "2.4.4 Link Purpose", "Link text is 'click here'",
                                "xlsx:sheet:Budget 2026:cell:B3", severity="MODERATE"),
                         _issue("XLSX-TABLE-001", "1.3.1 Info and Relationships",
                                "Check the header row is marked", "xlsx:sheet:Budget 2026",
                                severity="REVIEW")], 4),
            _doc(FIXED, [_issue("DOCX-ALT-001", ALT, "Chart has no description", "docx:image:4")], 5),
        ],
    })
    store.record_remediation(SID, FIXED, corrected_sha256="c" * 64)
    with store._db.cursor() as cur:        # a fixed instant, so the fixture is deterministic
        store._db.execute(cur, "UPDATE file_records SET remediated_at=%s WHERE scan_id=%s AND file=%s",
                          ("2026-09-02T10:00:00+00:00", SID, FIXED))


@pytest.fixture()
def client(monkeypatch, isolated_store):
    import core
    import report_facts as rf
    from fastapi.testclient import TestClient
    from app import app
    rf.clear_scan_index_cache()
    monkeypatch.setattr(rf, "_now", lambda: FIXED_NOW)
    monkeypatch.delenv("ACP_BUILD_VERSION", raising=False)
    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "ACCESS_CODE", "", raising=False)
    monkeypatch.setattr(core, "GOOGLE_CLIENT_ID", "test-client-id", raising=False)
    monkeypatch.setattr(core, "E2E_KEY", None, raising=False)
    monkeypatch.setattr(core, "OWNER_EMAIL", OWNER, raising=False)
    monkeypatch.setattr(core, "verify_gis_token", lambda tok: tok or None)
    monkeypatch.setattr(core, "email_allowed", lambda e: e == OWNER)
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {OWNER}"})
    yield c
    rf.clear_scan_index_cache()


def _pages(client, *, include: bool, limit: int = 2) -> list[dict]:
    q = "&include=findings" if include else ""
    first = client.get(f"/scans/{SID}/report-facts?limit={limit}{q}")
    assert first.status_code == 200, first.text
    pages = [first.json()]
    while not pages[-1]["complete"] and sum(len(p["files"]) for p in pages) < pages[0]["filesTotal"]:
        offset = sum(len(p["files"]) for p in pages)
        r = client.get(f"/scans/{SID}/report-facts?offset={offset}&limit={limit}"
                       f"&digest={pages[0]['factsDigest']}{q}")
        assert r.status_code == 200, r.text
        pages.append(r.json())
    return pages


def generate(client, store) -> dict:
    build_estate(store)
    with_findings = _pages(client, include=True)
    files = {}
    for page in with_findings:
        for row in page["files"]:
            r = client.get(f"/scans/{SID}/files/{quote(row['file'], safe='')}/report-facts")
            assert r.status_code == 200, r.text
            files[row["file"]] = r.json()
    return {"_generatedBy": "tests/test_report_scan_finding_links.py (real store, real routes)",
            "scanId": SID, "pageLimit": 2, "scanPages": with_findings, "files": files}


def test_the_committed_fixture_is_what_the_real_store_produces(client, isolated_store):
    got = generate(client, isolated_store)
    text = json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("ACP_REGEN_REPORT_FIXTURES") == "1":
        FIXTURE.write_text(text, encoding="utf-8")
    assert FIXTURE.exists(), "run with ACP_REGEN_REPORT_FIXTURES=1 to create the fixture"
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == json.loads(text), (
        "the server's scan finding records changed; regenerate (see this module's docstring)")


def test_every_row_carries_exactly_its_per_file_findings(client, isolated_store):
    got = generate(client, isolated_store)
    rows = [row for page in got["scanPages"] for row in page["files"]]
    assert [r["file"] for r in rows] == sorted(got["files"])
    for row in rows:
        facts = got["files"][row["file"]]
        assert row["factsDigest"] == facts["factsDigest"]                  # contract 3a
        assert [f["id"] for f in row["findings"]] == [f["id"] for f in facts["findings"]]
        for rec, full in zip(row["findings"], facts["findings"]):
            assert rec == {k: full[k] for k in rec}                         # same record, verbatim
            assert set(rec) >= {"id", "ruleId", "sc", "severity", "detail", "location", "comparable"}
        assert row["remediatedAt"] == facts["identity"]["remediatedAt"]
    board = {f["location"]["label"] if f["location"] else None: f
             for f in next(r for r in rows if r["file"] == BOARD)["findings"]}
    # The same criterion and detail on two objects: two records, two ids, two locations.
    assert board["Image 1"]["id"] != board["Image 2"]["id"]
    assert board["Image 1"]["detail"] == board["Image 2"]["detail"]
    assert board[None]["sc"] == "3.1.1" and board[None]["comparable"] is False


def test_contract_7_the_digest_and_the_rows_do_not_depend_on_the_include(client, isolated_store):
    build_estate(isolated_store)
    plain = _pages(client, include=False)
    import report_facts as rf
    rf.clear_scan_index_cache()
    rich = _pages(client, include=True)
    assert [p["factsDigest"] for p in plain] == [p["factsDigest"] for p in rich]
    assert len({p["factsDigest"] for p in plain}) == 1
    strip = lambda row: {k: v for k, v in row.items() if k not in ("findings", "remediatedAt")}
    assert [strip(r) for p in rich for r in p["files"]] == [r for p in plain for r in p["files"]]
    assert all("findings" not in r for p in plain for r in p["files"])
    # And the digest a fresh read (the render route / final check) computes is the same one.
    assert rf.current_digest(isolated_store, SID, None, owner=OWNER) == plain[0]["factsDigest"]


def test_paging_with_the_include_keeps_memo_409_and_bounds(client, isolated_store):
    import report_facts as rf
    build_estate(isolated_store)
    # A memo built WITHOUT findings cannot serve a page that asks for them: rebuilt, same digest.
    first = client.get(f"/scans/{SID}/report-facts?limit=2").json()
    again = client.get(f"/scans/{SID}/report-facts?offset=2&limit=2&digest={first['factsDigest']}"
                       "&include=findings").json()
    assert again["snapshot"]["servedFrom"] == "fresh" and again["factsDigest"] == first["factsDigest"]
    assert all(isinstance(r["findings"], list) for r in again["files"])
    third = client.get(f"/scans/{SID}/report-facts?offset=4&limit=2&digest={first['factsDigest']}"
                       "&include=findings").json()
    assert third["snapshot"]["servedFrom"] == "memo" and third["files"][0]["findings"]
    # Evidence moves between pages → 409, with or without the include.
    isolated_store.record_remediation_diffs(SID, BOARD, [{"rule_id": "1.1.1", "before": "",
                                                          "after": "A logo"}])
    rf.clear_scan_index_cache()
    moved = client.get(f"/scans/{SID}/report-facts?offset=2&limit=2&digest={first['factsDigest']}"
                       "&include=findings")
    assert moved.status_code == 409 and moved.json()["detail"] == rf.SNAPSHOT_CHANGED_DETAIL
    # Pages with findings are bounded by rows; the response states the limit it applied.
    big = client.get(f"/scans/{SID}/report-facts?limit={rf.FILE_PAGE_MAX}&include=findings").json()
    assert big["limit"] == rf.FINDINGS_PAGE_MAX
    assert client.get(f"/scans/{SID}/report-facts?include=everything").status_code == 422
    # Owner-scoped exactly as before.
    assert rf.build_scan_facts(isolated_store, SID, owner="someone@else.org",
                               include_findings=True) is None


# ── the browser's model, rendered by the server renderer ──────────────────────────────────────

_ANCHOR = re.compile(r'<a href="([^"]+)">([^<]*)</a>')


def test_the_server_renderer_links_each_finding_card_to_its_exact_record():
    import report_render as rr
    assert MODEL_FIXTURE.exists(), (
        "run the JS test with ACP_REGEN_REPORT_FIXTURES=1 to write the model fixture")
    facts = json.loads(FIXTURE.read_text(encoding="utf-8"))
    model = json.loads(MODEL_FIXTURE.read_text(encoding="utf-8"))
    # The model must have been built from THIS facts fixture, not an older one.
    assert model["factsDigest"] == facts["scanPages"][0]["factsDigest"]
    body = rr.validate_request({"kind": "scan", "file": None, "mode": "full",
                                "factsDigest": model["factsDigest"], "model": model})["model"]
    out = rr.render_html(body, {}, "full", BASE)
    cards = [b for b in model["blocks"] if b.get("k") == "findingCard"]
    assert cards, "the model has no finding cards"
    links = []
    for href, text in _ANCHOR.findall(out):
        url = html.unescape(href)
        if "finding=" not in url:
            continue
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == BASE and parts.path == "/"
        q = parse_qs(parts.query, keep_blank_values=True)
        assert all(len(v) == 1 for v in q.values())
        links.append((q["scan"][0], q["file"][0], q["finding"][0], html.unescape(text)))
    # One exact link per card, each naming a finding that exists in that file's per-file facts.
    assert sorted((f, i) for _s, f, i, _t in links) == sorted((c["file"], c["id"]) for c in cards)
    for scan, name, finding, text in links:
        assert scan == SID
        record = next(f for f in facts["files"][name]["findings"] if f["id"] == finding)
        label = (record["location"] or {}).get("label")
        assert text == (label or "open this record in ACP")
    board = [(t, i) for _s, f, i, t in links if f == BOARD]
    assert len(board) == 3 and len({i for _t, i in board}) == 3
    assert {t for t, _i in board} == {"Image 1", "Image 2", "open this record in ACP"}
    assert "Location not recorded · <a href=" in out
    # The corrected document's finding is not a card and not a link (it awaits re-assessment).
    assert all(f != FIXED for _s, f, _i, _t in links)
