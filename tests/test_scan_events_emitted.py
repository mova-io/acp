"""The pipeline actually writes the ADR 0042 lifecycle log (PR 2 of 4).

PR 1 landed the table with no caller and a test asserting exactly that; this is the PR that
invalidates it, so `test_nothing_emits_scan_events_yet` is deleted here rather than worked around.

What these pin, in rough order of how much they would cost to get wrong:

  1. THE ORDERING RULE. An event is appended AFTER the durable write it describes, never before.
     This is `test_discover_completion_race.py`'s lesson applied to a second reader: #934 shipped
     a status flip that raced `add_inventory`, and a "Discovery complete" event read before the
     rows exist is the identical bug wearing a different hat. Asserted by spying on the store, not
     by checking the end state — which would pass either way.
  2. THE NEVER-RAISES CONTRACT. A store that is down, or a bug in the log itself, must not fail
     the scan it is merely narrating.
  3. THE VOCABULARY. `handlers.scan_event` swallows the ValueError that `append_scan_event`
     raises for an unknown kind (deliberately — see its docstring), so the guard is re-established
     statically here: every kind literal in the emit sites must be a declared kind. This catches a
     typo on branches no test executes, which is most of them.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ── 3. the vocabulary, checked statically across every emit site ──────────────

# Files that emit lifecycle events, and the call names they emit through.
_EMIT_SITES = ["handlers.py", "worker.py", "routes/scans.py", "review_target_reconciliation.py"]
# `_rem_event` is handlers' remediation wrapper. It MUST be here: it takes (scan_id, kind, ...)
# like the other two, so args[1] finds its kind unchanged — but a wrapper this walk does not know
# about is a set of emit sites the vocabulary guard silently skips. Adding the remediation kinds
# without adding this name made the whole suite pass while checking none of them, which is the
# failure mode this file exists to prevent, one level up.
_EMIT_CALLS = {"scan_event", "append_scan_event", "_rem_event"}


def _emitted_kind_literals() -> dict[str, list[str]]:
    """Every string literal passed as the `kind` argument at an emit site, by file.

    Parsed with `ast`, not grepped: a regex over source would miss a call split across lines
    (most of them are) and would happily match the word inside a comment.
    """
    found: dict[str, list[str]] = {}
    for rel in _EMIT_SITES:
        tree = ast.parse((ACP / "api" / rel).read_text(encoding="utf-8"))
        kinds: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in _EMIT_CALLS or len(node.args) < 2:
                continue
            kind = node.args[1]          # (scan_id, kind, **kw)
            if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                kinds.append(kind.value)
        if kinds:
            found[rel] = kinds
    return found


def test_every_emitted_kind_is_a_declared_kind():
    """The static half of the guard handlers.scan_event gives up at runtime.

    A typo like "scan.discoverd" would otherwise be swallowed at every call site and simply never
    appear in any log, with nothing failing anywhere.
    """
    import store as store_mod

    emitted = _emitted_kind_literals()
    assert emitted, "no emit sites found — the AST walk is broken, not the code"
    unknown = {rel: sorted(set(ks) - store_mod.Store.SCAN_EVENT_KINDS)
               for rel, ks in emitted.items()}
    unknown = {rel: ks for rel, ks in unknown.items() if ks}
    assert not unknown, (
        f"emit sites use kinds not in Store.SCAN_EVENT_KINDS: {unknown}. Add them to the "
        f"vocabulary (and ADR 0042) or fix the typo — scan_event swallows this at runtime.")


def test_the_pipeline_emits_something(monkeypatch):
    """Guards the AST walk itself: if the emit sites were renamed or removed, the test above
    would pass vacuously by finding nothing to object to."""
    emitted = _emitted_kind_literals()
    assert "handlers.py" in emitted, "handlers.py no longer emits any lifecycle event"
    assert len(set(emitted["handlers.py"])) >= 5, (
        f"handlers.py emits only {sorted(set(emitted['handlers.py']))} — the discover lifecycle "
        f"lost emit sites")


# ── 1 + the happy path: a real discover run writes a readable story ───────────

@pytest.fixture()
def discover_run(isolated_store, monkeypatch):
    """Drive the real _scan_discover over a one-file fake listing, as the race test does."""
    import core
    import handlers
    import scanner
    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setenv("ACP_DEFER_ANALYSIS_TO_ASSESS", "1")
    monkeypatch.setattr(scanner, "_list", lambda *a, **k: [
        {"name": "a.docx", "id": "d1", "mime": _DOCX}])
    return handlers, isolated_store


def test_a_discover_run_writes_its_lifecycle_in_order(discover_run):
    handlers, st = discover_run
    handlers._scan_discover(
        {"scan_id": "s-emit", "source": "local", "user": "a@x"},
        {"scan_id": "s-emit", "id": "j-emit", "attempts": 1})

    events = st.list_scan_events("s-emit")
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "scan.claimed", f"the claim must come first: {kinds}"
    assert kinds[-1] == "scan.discovered", f"the run must end discovered: {kinds}"
    for expected in ("scan.listing_started", "scan.listing_complete",
                     "scan.inventory_saved", "scan.lifecycle_applied"):
        assert expected in kinds, f"{expected} missing from {kinds}"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)), "seq must be gap-free"
    assert kinds.index("scan.listing_started") < kinds.index("scan.listing_complete")
    assert kinds.index("scan.listing_complete") < kinds.index("scan.inventory_saved")
    assert kinds.index("scan.inventory_saved") < kinds.index("scan.lifecycle_applied")


def test_events_carry_the_job_and_owner_that_produced_them(discover_run):
    handlers, st = discover_run
    handlers._scan_discover(
        {"scan_id": "s-attr", "source": "local", "user": "a@x"},
        {"scan_id": "s-attr", "id": "j-attr", "attempts": 3})

    claimed = [e for e in st.list_scan_events("s-attr") if e["kind"] == "scan.claimed"]
    assert len(claimed) == 1
    assert claimed[0]["job_id"] == "j-attr"
    assert claimed[0]["attempt"] == 3, "attempt must be the job's real claim counter"
    assert claimed[0]["owner_email"] == "a@x"


def test_listing_complete_records_the_count_and_completeness(discover_run):
    handlers, st = discover_run
    handlers._scan_discover(
        {"scan_id": "s-count", "source": "local", "user": "a@x"},
        {"scan_id": "s-count", "id": "j-count", "attempts": 1})

    (done,) = [e for e in st.list_scan_events("s-count") if e["kind"] == "scan.listing_complete"]
    assert done["detail"]["files_found"] == 1
    assert done["detail"]["truncated"] is False
    assert done["detail"]["complete"] is True, (
        "a non-truncated listing must record itself as complete — 'listed 1 file' and 'listed the "
        "first 1 of an unknown number' are different claims about the estate")


def test_inventory_saved_is_appended_only_after_the_rows_are_written(discover_run):
    """THE ordering rule, and the one worth spying for rather than asserting on the end state.

    Direct descendant of test_discover_completion_race.py: #934's bug was a status flip that ran
    before add_inventory, so a reader saw "complete, 0 files" against an empty table. An event is
    read exactly like a status. This spies on add_inventory and asserts no inventory_saved event
    exists at the moment it is entered — proving the ORDER, not merely the end state, which would
    pass under either ordering.
    """
    handlers, st = discover_run
    seen_at_save = []
    real_add = st.add_inventory

    def _spy(sid, inv):
        seen_at_save.append([e["kind"] for e in st.list_scan_events(sid)])
        return real_add(sid, inv)

    st.add_inventory = _spy
    handlers._scan_discover(
        {"scan_id": "s-order", "source": "local", "user": "a@x"},
        {"scan_id": "s-order", "id": "j-order", "attempts": 1})

    assert seen_at_save, "add_inventory was never called — the test setup is wrong"
    assert "scan.inventory_saved" not in seen_at_save[0], (
        "scan.inventory_saved was already appended while the inventory was still being written — "
        f"a reader in that window sees a saved inventory that does not exist: {seen_at_save[0]}")
    assert "scan.inventory_saved" in [e["kind"] for e in st.list_scan_events("s-order")]


def test_a_discovery_conflict_is_recorded_as_failed(discover_run):
    """The conflict branch returns cleanly rather than raising, so without an event the run's
    durable log would simply stop mid-story with no reason recorded."""
    handlers, st = discover_run
    # The holder has to be GENUINELY live: the guard treats a holder whose scan reached a
    # terminal status as stale and lets the newcomer take over, so seeding a finished run here
    # would quietly test the happy path instead of the conflict (it did, on the first draft).
    st.init_scan_run("s-one", "local", 0, "2026-08-29T00:00:00", "r", "h",
                     owner="a@x", status="running")
    assert st.acquire_discovery_guard("a@x", "local", "s-one") is None, "holder must take the slot"

    handlers._scan_discover(
        {"scan_id": "s-two", "source": "local", "user": "a@x"},
        {"scan_id": "s-two", "id": "j-two", "attempts": 1})

    kinds = [e["kind"] for e in st.list_scan_events("s-two")]
    assert kinds[-1] == "scan.failed", f"a rejected run must say so: {kinds}"
    (failed,) = [e for e in st.list_scan_events("s-two") if e["kind"] == "scan.failed"]
    assert failed["detail"]["reason"] == "discovery_conflict"


# ── 2. the never-raises contract, at the pipeline level ───────────────────────

def test_a_broken_event_log_does_not_fail_the_scan(discover_run):
    """The whole contract in one test: the log is narration, and narration may not break the work.

    Exercised at the PIPELINE level, not on the store method — the store's own guarantee was
    proven in PR 1; what matters here is that every call site actually goes through the wrapper
    that honours it.
    """
    handlers, st = discover_run

    def _boom(*a, **kw):
        raise RuntimeError("the event log is on fire")

    st.append_scan_event = _boom
    handlers._scan_discover(
        {"scan_id": "s-boom", "source": "local", "user": "a@x"},
        {"scan_id": "s-boom", "id": "j-boom", "attempts": 1})

    run = st.get_scan("s-boom", owner="a@x")
    assert run is not None, "the scan itself must survive a failing event log"
    assert run["run"]["status"] == "discovered", (
        "the run must still reach 'discovered' with the log broken — a telemetry failure that "
        "changes the outcome is not best-effort")
    assert st.count_inventory("s-boom") == 1, "and its inventory must still be persisted"


def test_scan_event_swallows_an_unknown_kind_rather_than_raising():
    """The deliberate hole the static test above exists to cover. A typo'd kind on a rare branch
    must not crash a production scan; test_every_emitted_kind_is_a_declared_kind is what stops
    the typo reaching production in the first place."""
    import handlers
    handlers.scan_event("s1", "scan.not_a_real_kind")      # must not raise


def test_scan_event_is_a_noop_without_a_scan_id():
    """The thread path calls this before a scan_id exists. The log is scan-anchored; inventing an
    anchor would be worse than the gap."""
    import handlers
    handlers.scan_event(None, "scan.failed")               # must not raise
