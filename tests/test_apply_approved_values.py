"""The write-back that closes remediate → review → publish.

Approving an alt-text value used to store the text as evidence and stop. Nothing wrote it into
the document, so store.mark_file_compliant_if_reviewed correctly refused to certify the file —
and, having no applier, it refused forever. The file was approved and permanently unpublishable.

These tests pin the loop end to end: per-image approvals reach the right images, the values are
credited only when a re-scan of the WRITTEN bytes shows the criterion cleared, and the file
certifies off that re-scan rather than off the approval.
"""
from __future__ import annotations
import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from hitl_viewed import approve_bound, viewed_fields

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

SLIDE = "ppt/slides/slide1.xml"
FILE = "deck.pptx"
SID = "s1"


def _deck(*names: str) -> bytes:
    pics = "".join(f'<p:pic><p:cNvPr id="{i+2}" name="{n}"/></p:pic>' for i, n in enumerate(names))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(SLIDE, f"<p:sld>{pics}</p:sld>")
        z.writestr("docProps/core.xml", "<cp:coreProperties xmlns:cp='http://schemas.openxmlformats.org/package/2006/metadata/core-properties'/>")
    return buf.getvalue()


def _slide_xml(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(SLIDE).decode("utf-8")


@pytest.fixture()
def store(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "apply.db")
    return store_mod.Store()


def _seed(store, *, names=("Picture 1", "Chart 2")):
    """A remediated deck with one 1.1.1 HITL row carrying one proposal per image."""
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": FILE, "engine": "office", "status": "pass", "score": 40, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv1",
        "issues": [{"ruleId": "PPTX-ALT-001", "wcag": "1.1.1", "severity": "CRITICAL"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, FILE, drive_write_url="http://d/1", blob_url="http://b/1")
    item_id = store.enqueue_proposals(SID, FILE, "1.1.1", [
        {"locator": f"{SLIDE}#{n}", "before": "(no alt text)",
         "proposed_value": f"AI draft for {n}", "rationale": "r", "source": "llava"}
        for n in names
    ], rule_name="Non-text Content")
    return item_id


class _Blob:
    """Stands in for Azure Blob: the remediated copy lives here and the applier rewrites it."""
    def __init__(self, data): self.data, self.uploads = data, []
    def download_remediated(self, owner, sid, f): return self.data
    def upload_remediated(self, owner, sid, f, data, mime):
        self.data = data; self.uploads.append((f, mime)); return "http://b/2"

    # The approved writer publishes to a digest-scoped immutable object and moves the pointer
    # at commit (handlers._apply_approved_values); this fake serves whatever was stored last.
    def upload_immutable_retry(self, owner, sid, f, data, mime):
        return self.upload_remediated(owner, sid, f, data, mime)


def _run_handler(monkeypatch, store, blob, *, residual, file=FILE):
    """Drive the handler with Blob and the residual re-scan stubbed."""
    import core, handlers
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    # The seam is `_verify_residual`, which returns a three-state Verification. `ok=True`
    # here says "the re-scan RAN"; `residual` is what it saw. A stub returning a bare set
    # could not express "could not verify", which is the outcome the fail-closed tests need
    # (see tests/test_verification_fail_closed.py).
    from proposals import Verification
    monkeypatch.setattr(handlers, "_verify_residual",
                        lambda b, f, **kwargs: Verification(True, residual or ()))
    handlers._apply_approved_values({"scan_id": SID, "file": file}, {})


# ── the gate, before anything is written ──────────────────────────────────────

def test_approval_alone_leaves_the_file_uncertified(store):
    item_id = _seed(store)
    approve_bound(store, item_id, [])          # accept both drafts unedited

    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False


# ── the write ─────────────────────────────────────────────────────────────────

def test_each_image_receives_its_own_approved_description(store, monkeypatch):
    item_id = _seed(store)
    approve_bound(store, item_id, ["A clinician at a desk.", None])   # edit 1st, accept 2nd

    blob = _Blob(_deck("Picture 1", "Chart 2"))
    _run_handler(monkeypatch, store, blob, residual=set())

    xml = _slide_xml(blob.data)
    assert 'name="Picture 1" descr="A clinician at a desk."' in xml
    assert 'name="Chart 2" descr="AI draft for Chart 2"' in xml     # unedited draft accepted
    assert blob.uploads == [(FILE, "application/vnd.openxmlformats-officedocument.presentationml.presentation")]
    # Approved edits carry the same embedded provenance as deterministic outputs, and
    # evidence binds the final stamped bytes, not an earlier unstamped version.
    from xml.etree import ElementTree as ET
    import hashlib
    with zipfile.ZipFile(io.BytesIO(blob.data)) as archive:
        props = ET.fromstring(archive.read("docProps/custom.xml"))
        by_name = {p.get("name"): list(p)[0].text for p in props}
    assert by_name["Remediated By"] == "Mova.io ACP"
    assert by_name["Remediation Date"].endswith("Z")
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT corrected_sha256 FROM file_records WHERE scan_id=%s AND file=%s", (SID, FILE))
        assert store._db.fetchone(cur)["corrected_sha256"] == hashlib.sha256(blob.data).hexdigest()



def test_written_values_are_credited_and_the_file_certifies_off_the_rescan(store, monkeypatch):
    item_id = _seed(store)
    approve_bound(store, item_id, [])

    _run_handler(monkeypatch, store, _Blob(_deck("Picture 1", "Chart 2")), residual=set())

    assert store.count_unapplied_approved_values(SID, FILE) == 0     # promise kept
    rec = store.get_file_record(SID, FILE)
    assert rec["compliant"] == 1                                     # certified by the apply job
    diffs = store.get_remediation_diffs(SID, FILE)
    assert [d["after"] for d in diffs] == ["AI draft for Picture 1", "AI draft for Chart 2"]
    assert all("approved by a reviewer" in d["note"] for d in diffs)


# ── the honesty gates ─────────────────────────────────────────────────────────

def test_a_write_that_does_not_clear_the_criterion_credits_nothing(store, monkeypatch):
    """The text went in but 1.1.1 still fails — an image nobody reviewed, say. Crediting the
    approval here would certify a document that still fails, which is the original bug."""
    item_id = _seed(store)
    approve_bound(store, item_id, [])

    blob = _Blob(_deck("Picture 1", "Chart 2"))
    _run_handler(monkeypatch, store, blob, residual={"1.1.1"})       # still failing

    assert store.count_unapplied_approved_values(SID, FILE) == 1     # row stays unapplied
    assert store.get_file_record(SID, FILE)["compliant"] == 0
    assert blob.uploads == []                                        # nothing stored
    assert store.get_remediation_diffs(SID, FILE) == []


def test_an_unresolvable_locator_is_never_written_to_another_image(store, monkeypatch):
    """The reviewer approved text for an image this document no longer has."""
    item_id = _seed(store, names=("Picture 1", "Ghost 9"))
    approve_bound(store, item_id, [])

    blob = _Blob(_deck("Picture 1"))                                 # Ghost 9 is gone
    _run_handler(monkeypatch, store, blob, residual=set())

    xml = _slide_xml(blob.data)
    assert 'name="Picture 1" descr="AI draft for Picture 1"' in xml
    assert "Ghost 9" not in xml
    notes = [d["detail"] for d in store.list_decisions(scan_id=SID) if d["action"] == "apply.unresolved"]
    assert notes and "Ghost 9" in notes[0]


def test_no_remediated_copy_means_nothing_is_written(store, monkeypatch):
    item_id = _seed(store)
    approve_bound(store, item_id, [])

    blob = _Blob(None)                                               # never remediated
    from worker import FatalJobError
    with pytest.raises(FatalJobError, match='No corrected copy'):
        _run_handler(monkeypatch, store, blob, residual=set())

    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert [d["action"] for d in store.list_decisions(scan_id=SID)].count("apply.no_remediated_copy") == 1


def test_a_format_with_no_applier_says_so_rather_than_succeeding(store, monkeypatch):
    """HTML, not PDF: PDF gained an applier (remediate_pdf.apply_pdf_approved — see
    tests/test_pdf_approved_value_writeback.py), so it is no longer an example of a format
    that has none. HTML is remediated in place and has no approved-value write-back."""
    import core, handlers
    store.init_scan_run(SID, "drive", 1, "t", "r", "h")
    monkeypatch.setattr(core, "store", store)
    handlers._apply_approved_values({"scan_id": SID, "file": "page.html"}, {})
    actions = [d["action"] for d in store.list_decisions(scan_id=SID)]
    assert "apply.unsupported" in actions


def test_applying_twice_is_idempotent(store, monkeypatch):
    item_id = _seed(store)
    approve_bound(store, item_id, [])

    blob = _Blob(_deck("Picture 1", "Chart 2"))
    _run_handler(monkeypatch, store, blob, residual=set())
    first = blob.data
    _run_handler(monkeypatch, store, blob, residual=set())           # row is applied now

    assert blob.data == first                                        # no second write
    assert len(blob.uploads) == 1
    assert len(store.get_remediation_diffs(SID, FILE)) == 2          # diffs not duplicated


# ── link text (2.4.4 / 2.4.9) ──────────────────────────────────────────────────
# The write-back apply_link_text.py adds — same loop, different locator scheme (href, not
# part#name), so a separate seed helper and its own pass through the same gates.

DOC_FILE = "report.docx"
_DOC_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" '
             'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
             'Target="https://example.com/pricing" TargetMode="External"/>'
             '</Relationships>')


def _report(text: str = "click here") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml",
                   '<w:document><w:body>'
                   f'<w:hyperlink r:id="rId1"><w:r><w:t>{text}</w:t></w:r></w:hyperlink>'
                   '</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
    return buf.getvalue()


def _doc_xml(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("word/document.xml").decode("utf-8")


def _seed_link(store):
    """A remediated docx with one 2.4.4 HITL row carrying one link-text proposal."""
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": DOC_FILE, "engine": "office", "status": "pass", "score": 80, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv2",
        "issues": [{"ruleId": "DOCX-LINK-001", "wcag": "2.4.4", "severity": "SERIOUS"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, DOC_FILE, drive_write_url="http://d/2", blob_url="http://b/2")
    return store.enqueue_proposals(SID, DOC_FILE, "2.4.4", [
        {"locator": "https://example.com/pricing", "before": "click here",
         "proposed_value": "Pricing details", "rationale": "r", "source": "derived"},
    ], rule_name="Link Purpose")


def test_link_text_lands_on_the_hyperlink_and_certifies(store, monkeypatch):
    item_id = _seed_link(store)
    approve_bound(store, item_id, [])                       # accept the draft unedited

    blob = _Blob(_report())
    _run_handler(monkeypatch, store, blob, residual=set(), file=DOC_FILE)

    xml = _doc_xml(blob.data)
    assert "Pricing details" in xml and "click here" not in xml
    assert store.count_unapplied_approved_values(SID, DOC_FILE) == 0
    assert store.get_file_record(SID, DOC_FILE)["compliant"] == 1
    diffs = store.get_remediation_diffs(SID, DOC_FILE)
    assert len(diffs) == 1
    d = diffs[0]
    assert (d["rule_id"], d["before"], d["after"]) == ("2.4.4", "click here", "Pricing details")
    assert d["note"] == "approved by a reviewer · https://example.com/pricing"


def test_link_text_not_cleared_credits_nothing(store, monkeypatch):
    item_id = _seed_link(store)
    approve_bound(store, item_id, [])

    blob = _Blob(_report())
    _run_handler(monkeypatch, store, blob, residual={"2.4.4"}, file=DOC_FILE)       # still failing

    assert store.count_unapplied_approved_values(SID, DOC_FILE) == 1
    assert store.get_file_record(SID, DOC_FILE)["compliant"] == 0
    assert blob.uploads == []


def test_alt_and_link_approvals_on_the_same_file_both_apply(store, monkeypatch):
    """A file can carry both an approved alt-text row and an approved link-text row; both
    writes must land in the one uploaded copy, not just whichever ran first."""
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": DOC_FILE, "engine": "office", "status": "pass", "score": 60, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv3",
        "issues": [{"ruleId": "DOCX-LINK-001", "wcag": "2.4.4", "severity": "SERIOUS"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, DOC_FILE, drive_write_url="http://d/3", blob_url="http://b/3")
    link_item = store.enqueue_proposals(SID, DOC_FILE, "2.4.4", [
        {"locator": "https://example.com/pricing", "before": "click here",
         "proposed_value": "Pricing details", "rationale": "r", "source": "derived"},
    ], rule_name="Link Purpose")
    alt_item = store.enqueue_proposals(SID, DOC_FILE, "1.1.1", [
        {"locator": "word/document.xml#Figure 1", "before": "(no alt text)",
         "proposed_value": "A pricing chart.", "rationale": "r", "source": "llava"},
    ], rule_name="Non-text Content")
    approve_bound(store, link_item, [])
    approve_bound(store, alt_item, [])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml",
                   '<w:document><w:body>'
                   '<w:hyperlink r:id="rId1"><w:r><w:t>click here</w:t></w:r></w:hyperlink>'
                   '<wp:docPr id="1" name="Figure 1"/>'
                   '</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)

    blob = _Blob(buf.getvalue())
    _run_handler(monkeypatch, store, blob, residual=set(), file=DOC_FILE)

    xml = _doc_xml(blob.data)
    assert "Pricing details" in xml
    assert 'descr="A pricing chart."' in xml
    assert len(blob.uploads) == 1                                    # one upload, both writes in it
    assert store.count_unapplied_approved_values(SID, DOC_FILE) == 0


# ── unedited approval means "the drafts I was shown are correct" ──────────────

def test_approve_proposal_values_falls_back_to_each_draft(store):
    item_id = _seed(store)
    assert store.approve_proposal_values(item_id, [None, ""]) == 2
    vals = store.approved_alt_values(SID, FILE)
    assert vals == {}                                                # not approved yet
    store.update_hitl_item(item_id, "approved", None, None)
    assert store.approved_alt_values(SID, FILE) == {
        f"{SLIDE}#Picture 1": "AI draft for Picture 1",
        f"{SLIDE}#Chart 2": "AI draft for Chart 2",
    }


# ── the route seam: approving must actually schedule the write ────────────────

def _req(email="ada@movate.com"):
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(user_email=email))


def test_the_approve_route_enqueues_the_write_and_records_the_values(store, monkeypatch):
    """Without this enqueue the values sit in the database forever — the original dead end."""
    import core
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None)
    from routes.hitl import hitl_update, HitlUpdate

    item_id = _seed(store)
    res = hitl_update(item_id, HitlUpdate(status="approved", **viewed_fields(item_id),
                                          approved_values=["A clinician at a desk.", None]), _req())
    assert res["status"] == "approved"

    # the reviewer's per-image text was recorded, unedited entries falling back to the draft
    assert store.approved_alt_values(SID, FILE) == {
        f"{SLIDE}#Picture 1": "A clinician at a desk.",
        f"{SLIDE}#Chart 2": "AI draft for Chart 2",
    }
    # …and a write was scheduled
    job = store.claim_job("w1")
    assert job["type"] == "apply_approved_values"
    assert job["payload"] == {"scan_id": SID, "file": FILE}
    # the file does NOT certify on the approval itself — the document has no descriptions yet
    assert store.get_file_record(SID, FILE)["compliant"] == 0


def test_a_judgement_approval_schedules_no_write(store, monkeypatch):
    """A contrast sign-off resolves by approval alone; there is no content to write."""
    import core
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None)
    from routes.hitl import hitl_update, HitlUpdate

    store.init_scan_run(SID, "drive", 1, "t", "r", "h")
    item = store.queue_hitl_deferral(SID, FILE, "contrast needs sign-off", 1, rule_id="1.4.3")
    hitl_update(item, HitlUpdate(status="approved", **viewed_fields(item)), _req())
    assert store.claim_job("w1") is None


def test_approving_without_sending_values_still_counts_the_drafts_as_content(store, monkeypatch):
    """Approving a row means accepting the drafts it was showing.

    A client that approves without sending approved_values (a bulk approve, an older build)
    left every proposal valueless. The row then held no "content", the gate counted nothing,
    and the file certified with the drafts never written in — the original bug, reintroduced
    through a different door.
    """
    item_id = _seed(store)
    approve_bound(store, item_id, None)   # note: no approved_values sent

    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False
    assert store.approved_alt_values(SID, FILE) == {
        f"{SLIDE}#Picture 1": "AI draft for Picture 1",
        f"{SLIDE}#Chart 2": "AI draft for Chart 2",
    }
    # …and the drafts are what actually get written
    blob = _Blob(_deck("Picture 1", "Chart 2"))
    _run_handler(monkeypatch, store, blob, residual=set())
    assert 'descr="AI draft for Picture 1"' in _slide_xml(blob.data)
    assert store.get_file_record(SID, FILE)["compliant"] == 1


def test_link_purpose_approvals_have_no_applier_and_keep_the_file_out_of_publish(store):
    """2.4.4 carries no alt-text applier. It must not be silently dropped: the file stays
    uncertified rather than certifying with the link text still unwritten."""
    _seed(store)
    item = store.enqueue_proposals(SID, FILE, "2.4.4", [
        {"locator": "slide1#rId3", "before": "click here",
         "proposed_value": "Download the intake form", "rationale": "r", "source": "llm"}],
        rule_name="Link Purpose")
    approve_bound(store, item, [])

    assert store.approved_alt_values(SID, FILE) == {}                # not an alt-text value
    assert store.count_unapplied_approved_values(SID, FILE) >= 1     # still gates Publish


# ── the gate must name every value kind, or a kind is stranded ────────────────

def test_a_link_text_only_approval_still_schedules_the_write(store, monkeypatch):
    """Each value kind lives in its own hitl row, and the route decides whether the apply job
    runs at all. A gate that asks only about SOME kinds strands the rest exactly as the missing
    applier did: the text is approved, never written, and the file can never certify."""
    import core
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None)
    from routes.hitl import hitl_update, HitlUpdate

    item = _seed_link(store)                                  # a docx whose ONLY row is 2.4.4
    hitl_update(item, HitlUpdate(status="approved", **viewed_fields(item), approved_values=[None]), _req())

    assert store.approved_alt_values(SID, DOC_FILE) == {}     # nothing on the alt lane
    job = store.claim_job("w1")
    assert job and job["type"] == "apply_approved_values"
    assert job["payload"] == {"scan_id": SID, "file": DOC_FILE}


def test_the_gate_covers_every_kind_the_applier_writes(store):
    """The union in one place. Each kind alone must open the gate — a new kind added to the
    handler and forgotten here would never reach a job, and nothing else would notice."""
    store.init_scan_run(SID, "drive", 1, "t", "r", "h")
    for sc, locator in (("1.1.1", f"{SLIDE}#Picture 1"), ("2.4.4", "https://example.com/x"),
                        ("4.1.2", "pdf:field:1:0")):
        f = f"only-{sc}.pdf"
        assert store.has_approved_values_to_write(SID, f) is False
        item = store.enqueue_proposals(SID, f, sc, [
            {"locator": locator, "before": "(before)", "proposed_value": "approved text",
             "rationale": "r", "source": "s"}], rule_name=sc)
        approve_bound(store, item, [])
        assert store.has_approved_values_to_write(SID, f) is True, f"{sc} alone must open the gate"


def test_every_applier_returns_the_row_shape_the_write_loop_reads():
    """_apply_one_value_kind records the remediation diff from a['before']/a['after'], inside a
    best-effort try. An applier returning a different shape raises there and the whole diff
    batch is dropped: the value lands in the document with no record of what it replaced, and
    nothing fails. Pin the contract at the source instead."""
    import re
    api = ACP / "api"
    for mod in ("apply_alt.py", "apply_link_text.py", "remediate_pdf.py"):
        for row in re.findall(r"applied\.append\(\{[^}]*\}", (api / mod).read_text()):
            if '"locator"' not in row:
                continue                              # prose progress messages, not applied rows
            assert '"before"' in row and '"after"' in row, f"{mod}: {row}"


# ── F4: the batched per-scan form agrees with the per-file gate, in one query ──────────────────

def test_batched_unapplied_counts_match_the_per_file_gate(store):
    item_id = _seed(store)
    approve_bound(store, item_id, [])

    by_file = store.count_unapplied_approved_values_by_file(SID)
    assert by_file == {FILE: store.count_unapplied_approved_values(SID, FILE)} == {FILE: 1}

    # a scan with nothing approved-and-unapplied → empty dict (absent, not zero-filled)
    store.init_scan_run("s2", "drive", 0, "2026-07-10T00:00:00Z", "rubric", "hash")
    assert store.count_unapplied_approved_values_by_file("s2") == {}


@pytest.mark.parametrize('cache_checksum', [None, 'inventory-content-key'])
def test_approval_before_remediation_creates_first_corrected_copy_from_assessed_source(store, monkeypatch, cache_checksum):
    import scanner
    item_id = _seed(store)
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE scan_runs SET source='local' WHERE id=%s", (SID,))
        store._db.execute(cur, 'UPDATE file_records SET remediated_at=NULL,blob_url=NULL,drive_write_url=NULL WHERE scan_id=%s', (SID,))
    approve_bound(store, item_id, [])
    cached = _deck('Picture 1','Chart 2')
    monkeypatch.setattr(store, 'get_source_checksum', lambda *a: cache_checksum)
    monkeypatch.setattr(scanner, 'read_cached_source', lambda *a, **kw: cached if kw.get('checksum') == cache_checksum else None)
    blob = _Blob(None)
    _run_handler(monkeypatch, store, blob, residual=set())
    assert blob.uploads
    assert 'AI draft for Picture 1' in _slide_xml(blob.data)
    assert store.get_file_record(SID, FILE)['remediated_at']
    assert store.count_unapplied_approved_values(SID, FILE) == 0
