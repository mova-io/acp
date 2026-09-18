"""A WCAG-exception resolution is a JUDGEMENT, and 'decorative' is a MARKING — never a value.

A reviewer may close a finding with the standard's own exception instead of authoring a fix:
'decorative' (WCAG 1.1.1 — the image conveys nothing, so no text alternative is required) or
'essential_exception' (1.4.5/1.4.9 — a logotype is exempt from images-of-text). EvidenceCard
already suppressed the values for both, and routes/hitl.py recorded the exception in a
decision-log detail.

It recorded it NOWHERE ELSE, and the certify gate reads rows. So store._row_approved_values fell
through to its `proposed_value` fallback — which exists on the sound principle that approving an
unedited card means accepting the drafts it was showing — and handed back the card's own UI
label. Two consequences, both verified before this was changed:

  * count_unapplied_approved_values counted the row forever. No applier could ever mark it
    applied, so mark_file_compliant_if_reviewed returned False permanently and a correctly
    resolved file could never certify or reach Publish. This is the dead end
    tests/test_pdf_explain_only_maps.py closed for the three PDF map cards.
  * Worse on Office, which really is appliable: approved_alt_values handed apply_alt the string
    "Mark as decorative" to write into the image's /Alt. A decorative image wearing nonsense alt
    text is a worse accessibility outcome than the missing alt it replaced.

The fix is not to drop the write. A decorative image is not undescribed, it is DELIBERATELY
undescribed, and WCAG asks for it to be marked so. Left blank, the reviewer's decision lives only
in our audit log: every later scan re-raises the same finding for the next reviewer to decide
again. So `resolution` is persisted on the row (the counters and _row_approved_values read it
there), and 'decorative' on an Office file reaches the applier as a MARKING — empty descr plus
the OOXML `adec:decorative` marker the .NET analysers honour, the same markup
tests/test_alt_text_decorative_marker.py pins against the real engines.

'essential_exception', and 'decorative' on a PDF, write nothing: marking a PDF figure decorative
means re-tagging it as an /Artifact, which no writer here does. Those stay recorded judgements —
which is exactly what the resolution vocabulary means, and is enough to resolve the finding.
"""
from __future__ import annotations
import io
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from hitl_viewed import approve_bound, viewed_fields

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

SLIDE = "ppt/slides/slide1.xml"
FILE = "deck.pptx"
SID = "s-exc"
LOGO = f"{SLIDE}#Logo 1"
CHART = f"{SLIDE}#Chart 2"

# The card's own button label. It is a UI string; the day it reaches a document is the bug.
DECO_LABEL = "Mark as decorative — no alt text needed"


@pytest.fixture()
def store(monkeypatch):
    import store as store_mod
    monkeypatch.setattr(store_mod, "_SQLITE_PATH", Path(tempfile.mkdtemp()) / "exc.db")
    return store_mod.Store()


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


def _remediated(store, file=FILE, engine="office"):
    store.init_scan_run(SID, "drive", 1, "2026-07-10T00:00:00Z", "rubric", "hash")
    store.save_file_result(SID, {
        "file": file, "engine": engine, "status": "fail", "score": 40, "compliant": 0,
        "skipped_rules": 0, "drive_file_id": "drv1",
        "issues": [{"ruleId": "PPTX-ALT-001", "wcag": "1.1.1", "severity": "CRITICAL"}],
    }, "2026-07-10T00:00:00Z")
    store.record_remediation(SID, file, drive_write_url="http://d/1", blob_url="http://b/1")


def _deco_row(store, file=FILE, locator=LOGO):
    """The decorative-inference card remediate_office enqueues, verbatim in shape."""
    return store.enqueue_proposals(SID, file, "1.1.1", [
        {"locator": locator, "before": "(no alt text)", "proposed_value": DECO_LABEL,
         "rationale": "filename 'logo' looks decorative", "kind": "decorative",
         "source": "decorative-image inference (heuristic)"},
    ], rule_name="Non-text Content")


def _resolve(store, item_id, resolution="decorative", note=None, value=None):
    """What the client sends for an exception: a note, no values (EvidenceCard suppresses them)."""
    approve_bound(store, item_id, None, resolution=resolution,
                  note=note or "Marked decorative — no description needed", value=value)


class _Blob:
    def __init__(self, data): self.data, self.uploads = data, []
    def download_remediated(self, owner, sid, f): return self.data
    def upload_remediated(self, owner, sid, f, data, mime):
        self.data = data; self.uploads.append((f, mime)); return "http://b/2"
    # The approved writer publishes digest-scoped and moves the pointer at commit.
    def upload_immutable_retry(self, owner, sid, f, data, mime):
        return self.upload_remediated(owner, sid, f, data, mime)


def _run_handler(monkeypatch, store, blob, *, residual=frozenset(), file=FILE):
    import core, handlers
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setitem(sys.modules, "blob", blob)
    from proposals import Verification
    monkeypatch.setattr(handlers, "_verify_residual",
                        lambda b, f, **kwargs: Verification(True, set(residual)))
    handlers._apply_approved_values({"scan_id": SID, "file": file}, {})


def _req(user_email="reviewer@example.com"):
    return SimpleNamespace(state=SimpleNamespace(user_email=user_email))


# ── the certify gate ──────────────────────────────────────────────────────────

def test_a_decorative_resolution_does_not_block_certification(store):
    """THE regression. Before this, the row counted 1 forever and the file never certified."""
    _remediated(store)
    _resolve(store, _deco_row(store))

    assert store.count_unapplied_approved_values(SID, FILE) == 0, (
        "the resolved row counts as content the document does not carry — but the reviewer "
        "authored no content, so nothing will ever be written and the file can never certify")
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is True


def test_an_essential_exception_does_not_block_certification(store):
    """1.4.5 has no writer at all — its `image N` locators reach nothing — so an exception left
    unrecorded on the row was a permanent dead end with no way out even in principle."""
    _remediated(store)
    item = store.enqueue_proposals(SID, FILE, "1.4.5", [
        {"locator": "image 1", "before": "text baked into an image",
         "proposed_value": "ACME CORP", "rationale": "logotype", "source": "OCR (tesseract)"},
    ], rule_name="Images of Text")
    _resolve(store, item, "essential_exception", note="Marked essential logo/brand — exempt")

    assert store.count_unapplied_approved_values(SID, FILE) == 0
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is True


def test_without_the_resolution_the_same_row_blocks_forever(store):
    """The bug, still reproducible on demand: drop the resolution and the block comes back. This
    is what keeps the test above honest — it would pass just as well if the gate had been
    disabled wholesale, and this shows it was not."""
    _remediated(store)
    item = _deco_row(store)
    store.update_hitl_item(item, "approved", "note", None)      # no resolution recorded

    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False


def test_the_resolution_survives_a_client_that_sends_a_headline_value(store):
    """A client that posts a final value alongside the exception writes the legacy
    `approved_value` column, which normally counts forever by design ('a human agreed to some
    text but we don't know where it goes'). For a resolved row there is nowhere for it to go BY
    DESIGN — the text is a note about an exception — so it must not resurrect the dead end."""
    _remediated(store)
    _resolve(store, _deco_row(store), value="it's just the letterhead")

    assert store.count_unapplied_approved_values(SID, FILE) == 0
    assert store.approved_alt_values(SID, FILE) == {}


def test_a_row_still_owing_real_alt_text_keeps_blocking(store):
    """The gate is narrowed to resolved rows, not switched off. A second image on the same file
    that a reviewer actually described is still content the document does not yet carry."""
    _remediated(store)
    _resolve(store, _deco_row(store))
    described = store.enqueue_proposals(SID, FILE, "2.4.4", [
        {"locator": "https://example.com/pricing", "before": "click here",
         "proposed_value": "Pricing details", "rationale": "r", "source": "derived"},
    ], rule_name="Link Purpose")
    store.update_hitl_item(described, "approved", None, None)

    assert store.count_unapplied_approved_values(SID, FILE) == 1
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False


# ── the value that must never be written ──────────────────────────────────────

def test_the_card_label_is_never_offered_to_the_applier(store):
    """approved_alt_values feeds apply_alt directly. "Mark as decorative" going in here is
    "Mark as decorative" coming out as the picture's alt text."""
    _remediated(store)
    _resolve(store, _deco_row(store))
    assert store.approved_alt_values(SID, FILE) == {}


def test_the_card_label_never_reaches_the_document(store, monkeypatch):
    """End to end through the real applier — the accessibility harm, not just the bookkeeping."""
    _remediated(store)
    _resolve(store, _deco_row(store))

    blob = _Blob(_deck("Logo 1"))
    _run_handler(monkeypatch, store, blob)

    xml = _slide_xml(blob.data)
    assert DECO_LABEL not in xml
    assert "decorative" not in xml.split("<a:extLst")[0]     # no stray descr, marker aside


# ── the marking that must be written ──────────────────────────────────────────

def test_a_decorative_resolution_marks_the_image_in_the_document(store, monkeypatch):
    """Empty description + the OOXML marker: what the analysers read as "deliberately
    undescribed" (AltTextHeuristics.IsMarkedDecorative). Left blank instead, every future scan
    would re-raise the finding and the next reviewer would decide it all over again."""
    _remediated(store)
    _resolve(store, _deco_row(store))

    blob = _Blob(_deck("Logo 1"))
    _run_handler(monkeypatch, store, blob)

    xml = _slide_xml(blob.data)
    assert 'descr=' not in xml                               # a decorative image has NO alt text
    assert 'val="1"' in xml and "adec:decorative" in xml
    assert "{C183D7F6-B498-43B3-948B-1728B52AA6E4}" in xml   # the ext uri the analysers key on
    assert blob.uploads == [(FILE, "application/vnd.openxmlformats-"
                                   "officedocument.presentationml.presentation")]


def test_the_marker_matches_what_the_analysers_actually_honour(store):
    """Cross-check against tests/test_alt_text_decorative_marker.py, which drives the real .NET
    analysers over this exact markup. If that fixture and this writer ever drift, a document we
    called compliant is one the engine still flags."""
    import apply_alt
    import test_alt_text_decorative_marker as pinned

    for token in ('<adec:decorative', 'val="1"', "{C183D7F6-B498-43B3-948B-1728B52AA6E4}",
                  "http://schemas.microsoft.com/office/drawing/2017/decorative"):
        assert token in pinned._DECORATIVE_EXT, "the analyser-pinned fixture changed shape"
        assert token in apply_alt._DECORATIVE_EXTLST


def test_an_already_decorative_image_is_not_marked_twice(store, monkeypatch):
    """OOXML allows one extLst per element. Re-running the job on a file we already marked must
    not append a second marker and invalidate the part."""
    _remediated(store)
    _resolve(store, _deco_row(store))

    blob = _Blob(_deck("Logo 1"))
    _run_handler(monkeypatch, store, blob)
    once = _slide_xml(blob.data)

    store.enqueue_job = lambda *a, **k: None
    _run_handler(monkeypatch, store, blob)                   # row is applied — nothing re-runs
    assert _slide_xml(blob.data) == once
    assert once.count("adec:decorative") == 1


def test_a_described_image_and_a_decorative_one_land_in_one_document(store, monkeypatch):
    """They share the alt lane deliberately. Split into two lanes, neither could ever clear
    1.1.1 — each would re-scan against the other's still-unresolved images and credit nothing."""
    _remediated(store)
    _resolve(store, _deco_row(store))
    described = store.enqueue_proposals(SID, FILE, "1.1.1", [
        {"locator": CHART, "before": "(no alt text)", "proposed_value": "Q3 revenue by region",
         "rationale": "r", "source": "llava"},
    ], rule_name="Non-text Content")
    # enqueue_proposals REPLACES per (scan, file, rule), so the two cards are one row here; the
    # decorative one is re-stated as its own row below to keep both alive.
    approve_bound(store, described, [])
    deco_row = store.enqueue_proposals(SID, FILE, "1.1.1/decorative", [
        {"locator": LOGO, "before": "(no alt text)", "proposed_value": DECO_LABEL,
         "rationale": "r", "kind": "decorative", "source": "heuristic"},
    ], rule_name="Non-text Content")
    _resolve(store, deco_row)

    blob = _Blob(_deck("Logo 1", "Chart 2"))
    _run_handler(monkeypatch, store, blob)

    xml = _slide_xml(blob.data)
    assert 'name="Chart 2" descr="Q3 revenue by region"' in xml
    assert len(blob.uploads) == 1                            # one document edit, not two


def test_the_decorative_write_is_recorded_as_a_marking_not_as_alt_text(store, monkeypatch):
    """The before/after report is what a customer reads. It must not say we wrote a description."""
    _remediated(store)
    _resolve(store, _deco_row(store))
    _run_handler(monkeypatch, store, _Blob(_deck("Logo 1")))

    diffs = store.get_remediation_diffs(SID, FILE)
    assert [d["after"] for d in diffs] == ["(marked decorative — no alt text needed)"]
    assert all("approved by a reviewer" in d["note"] for d in diffs)


# ── formats with no marking to write ──────────────────────────────────────────

def test_a_decorative_pdf_figure_resolves_without_a_write(store, monkeypatch):
    """Marking a PDF figure decorative means re-tagging it as an /Artifact, which no writer here
    does. The exception still resolves the finding — it is a human judgement, and that is what
    the resolution vocabulary means — it just leaves no trace in the bytes. The marking lane is
    gated on the Office formats in handlers, so the PDF writer is never even reached."""
    import remediate_pdf
    pdf = "report.pdf"
    _remediated(store, file=pdf, engine="pdf")
    _resolve(store, _deco_row(store, file=pdf, locator="pdf:fig:1:0"))
    calls = []
    monkeypatch.setattr(remediate_pdf, "apply_pdf_approved",
                        lambda d, v: calls.append(v) or (d, [], []))

    blob = _Blob(b"%PDF-1.7\n")
    _run_handler(monkeypatch, store, blob, file=pdf)

    assert calls == [] and blob.uploads == []
    assert store.count_unapplied_approved_values(SID, pdf) == 0
    assert store.mark_file_compliant_if_reviewed(SID, pdf) is True


def test_a_deferred_row_resolves_even_though_its_locators_are_unwritable(store):
    """A deferred 1.1.1 row addresses its images by relationship id (`part#rId2`), while apply_alt
    resolves by element NAME — so there is nothing here a writer could reach. Offering those
    locators anyway would buy an `apply.unresolved` log line and no marking; the finding is still
    resolved by the reviewer's judgement."""
    _remediated(store)
    store.queue_hitl_deferral(SID, FILE, "image lacks a faithful alt source", 1,
                              rule_id="1.1.1/deferred")
    item = next(i for i in store.list_hitl_queue(scan_id=SID))
    store.attach_hitl_evidence(SID, FILE, "1.1.1", [{"locator": f"{SLIDE}#rId2", "thumb": "x"}])
    _resolve(store, item["id"])

    assert store.approved_decorative_locators(SID, FILE) == []
    assert store.count_unapplied_approved_values(SID, FILE) == 0
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is True


# ── the route: what the reviewer's click actually does ────────────────────────

@pytest.fixture()
def route(store, monkeypatch):
    import core
    monkeypatch.setattr(core, "store", store)
    monkeypatch.setattr(core, "fire_webhook", lambda *a, **k: None, raising=False)
    jobs = []
    monkeypatch.setattr(store, "enqueue_job",
                        lambda name, payload, **k: jobs.append((name, payload)), raising=False)
    from routes.hitl import hitl_update, HitlUpdate
    return hitl_update, HitlUpdate, jobs


def test_the_route_persists_the_resolution_on_the_row(store, route):
    """It used to live only in a decision-log detail. The certify gate and the appliers read
    rows, so an exception recorded only in the log was, to them, no exception at all."""
    hitl_update, HitlUpdate, _jobs = route
    _remediated(store)
    item = _deco_row(store)
    hitl_update(item, HitlUpdate(status="approved", **viewed_fields(item), resolution="decorative"), _req())

    assert store.get_hitl_item(item)["resolution"] == "decorative"
    assert store.count_unapplied_approved_values(SID, FILE) == 0


def test_the_route_schedules_the_marking_for_a_decorative_resolution(store, route):
    """No value is approved, so the old gate (approved_alt_values or approved_field_values)
    scheduled nothing and the marking never reached the document."""
    hitl_update, HitlUpdate, jobs = route
    _remediated(store)
    hitl_update(_deco_row(store), HitlUpdate(status="approved", **viewed_fields(_deco_row(store)), resolution="decorative"), _req())

    assert [n for n, _ in jobs] == ["apply_approved_values"]


def test_reversing_a_resolution_restores_the_authored_value(store, route):
    """A reviewer who marked an image decorative and then came back to write real alt text must
    end up with the alt text WRITTEN. This is why the column is assigned, not COALESCEd — a
    sticky resolution would silently swallow the description they typed second."""
    hitl_update, HitlUpdate, _jobs = route
    _remediated(store)
    item = _deco_row(store)
    hitl_update(item, HitlUpdate(status="approved", **viewed_fields(item), resolution="decorative"), _req())
    hitl_update(item, HitlUpdate(status="approved", **viewed_fields(item), approved_values=["A nurse at a bedside."]),
                _req())

    assert store.get_hitl_item(item)["resolution"] in (None, "")
    assert store.approved_alt_values(SID, FILE) == {LOGO: "A nurse at a bedside."}
    assert store.approved_decorative_locators(SID, FILE) == []
    assert store.mark_file_compliant_if_reviewed(SID, FILE) is False    # owed a real write again


def test_a_rejection_un_resolves_the_finding(store, route):
    hitl_update, HitlUpdate, _jobs = route
    _remediated(store)
    item = _deco_row(store)
    hitl_update(item, HitlUpdate(status="approved", **viewed_fields(item), resolution="decorative"), _req())
    hitl_update(item, HitlUpdate(status="rejected", reject_reason="incorrect_object"), _req())

    assert store.get_hitl_item(item)["resolution"] in (None, "")
    assert store.approved_decorative_locators(SID, FILE) == []
