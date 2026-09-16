"""Reset is a TRUE clean slate — it purges blob storage, not just the DB.

store.reset_analytics() drops the remediation_state / applied_fixes / hitl_queue ROWS, but a
remediated file's fixed bytes, the cached source original and the page preview live in blob
storage (the `remediated` / `sources` / `thumbnails` containers). Before this, those survived a
reset, so "legacy remediations" lingered after the demo was supposedly wiped. /admin/reset now
also calls blob.purge_all() on the grafana/all scope. These pin: the wiring fires, its result is
reported + audited, it's a no-op when blob isn't configured, and the langfuse-only scope leaves
blobs untouched.
"""
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

import core  # noqa: E402
import blob  # noqa: E402
import store as store_mod  # noqa: E402

# The ONLY tables allowed to survive a reset: genuine configuration + user-authored programs.
# Everything else the schema defines is scan/review DATA and MUST be wiped so the app is a true
# clean slate for a new customer. If you add a config/program table, add it here WITH a reason;
# if you add a data table, add it to store._ANALYTICS_TABLES instead. One of the two — the test
# below fails closed otherwise.
_CONFIG_SURVIVORS = {
    "app_settings",        # worker count, AI mode, feature flags
    "schedule_config",     # scheduled-scan cadence
    "disposition_policy",  # retention/disposition RULES (the audit trail IS wiped)
    "ai_provider_config",  # provider endpoints + secret-REFs (never secret values)
    "campaign",            # admin-authored remediation programs
    "campaign_batch",
    "scope_rule",          # per-file WCAG scope RULES (config, like disposition_policy)
    # The administrator-authored live remediation routing rule survives just like the other
    # policy tables. Its action receipts and per-run immutable snapshots are DATA and are wiped.
    "remediation_automation_policy",
    # The archive auto-fire POLICY (R9) — administrator-authored configuration, the same class as
    # disposition_policy beside it, and split from its records the same way: archive_execution and
    # archive_policy_snapshot are in _ANALYTICS_TABLES and go.
    #
    # SURVIVING IS THE SAFE DIRECTION HERE, which is not obvious and is why it is written down.
    # This row is where the KILL SWITCH lives. A reset that wiped it would silently return the
    # tenant to the shipped defaults — and those are `enabled: False`, so the feature would be off
    # rather than running unsupervised; the switch is not load-bearing after a wipe. What a wipe
    # would actually destroy is the administrator's confirmed document-family mappings, which are
    # evidence somebody signed off on, and their archive destination. Both are configuration a
    # customer authored, not a record of their scans.
    "archive_autofire_policy",
    # Schema metadata, not customer data: one row holding a checksum of the DDL this build
    # applies, so a booting replica can verify the schema instead of replaying it (see
    # _PgAdapter.init_schema — replaying it on every boot deadlocked production). Nothing here
    # describes a scan, a document or a decision. Wiping it would be harmless but pointless: the
    # next boot would simply re-run the migration, taking the exclusive locks this exists to
    # avoid.
    "acp_schema_version",
    # Workspace roles and their permissions (PRD §12) — administrator-authored authorization
    # config, the same class as scope_rule and disposition_policy. Nothing here describes a scan,
    # a document or a decision.
    #
    # SURVIVING IS NOT MERELY HARMLESS HERE, IT IS REQUIRED, and for a reason the other survivors
    # do not have. Role ASSIGNMENTS live on the managed-person record in app_settings
    # (`people_records`), which already survives. Wiping the role DEFINITIONS while the
    # assignments survived would leave every person pointing at a role id that resolves to
    # nothing — and "the role could not be loaded" is the state PRD §14 requires the gate to treat
    # as a refusal. A reset would therefore lock every user, including the Owner, out of a
    # feature that had not gone wrong. Wiping both halves would be no better: it deletes the
    # protected Owner role, which exists precisely so administrative lockout is impossible.
    "workspace_roles",
    "workspace_role_permissions",
    # A user's chosen scan days, local time and timezone are durable preferences, not scan
    # output. Resetting assessment data must not unexpectedly turn their recurring scan off.
    "user_scan_schedules",
    "schedule_guardrails",  # deployment-wide administrator-authored scheduling limits
}


def _client(monkeypatch, isolated_store):
    monkeypatch.setattr(core, "store", isolated_store)
    monkeypatch.setattr(core, "OWNER_EMAIL", "", raising=False)  # admin gate no-op (local dev)
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


def test_reset_grafana_scope_purges_blobs(monkeypatch, isolated_store):
    calls = []
    monkeypatch.setattr(blob, "purge_all",
                        lambda owner=None: (calls.append(owner) or {"remediated": 3, "sources": 1, "thumbnails": 2}))
    c = _client(monkeypatch, isolated_store)
    r = c.post("/admin/reset?scope=grafana&confirm=true")
    assert r.status_code == 200, r.text
    body = r.json()
    # blob purge fired (whole-account: owner=None, matching the global reset_analytics)
    assert calls == [None]
    assert body["blobs_purged"] == {"remediated": 3, "sources": 1, "thumbnails": 2}
    assert body["cleared_tables"]  # DB tables still wiped


def test_reset_audit_log_records_blob_total(monkeypatch, isolated_store):
    monkeypatch.setattr(blob, "purge_all", lambda owner=None: {"remediated": 4, "sources": 0, "thumbnails": -1})
    c = _client(monkeypatch, isolated_store)
    c.post("/admin/reset?scope=grafana&confirm=true")
    # the reset is audited with the count of blobs actually deleted (4), ignoring the errored (-1)
    recent = isolated_store.list_decisions(limit=5)
    reset_entries = [d for d in recent if d.get("action") == "demo.reset"]
    assert reset_entries, "reset was not written to the decision log"
    assert "blobs=4" in (reset_entries[0].get("detail") or "")


def test_reset_noops_when_blob_unconfigured(monkeypatch, isolated_store):
    # Real purge_all against an unconfigured account returns {} and must not raise.
    monkeypatch.setattr(blob, "_ENABLED", False, raising=False)
    monkeypatch.setattr(blob, "_client", None, raising=False)
    c = _client(monkeypatch, isolated_store)
    r = c.post("/admin/reset?scope=grafana&confirm=true")
    assert r.status_code == 200, r.text
    assert r.json()["blobs_purged"] == {}


def test_langfuse_scope_leaves_blobs_untouched(monkeypatch, isolated_store):
    called = []
    monkeypatch.setattr(blob, "purge_all", lambda owner=None: called.append(owner) or {})
    monkeypatch.setattr(core, "reset_langfuse_traces", lambda: 7, raising=False)
    c = _client(monkeypatch, isolated_store)
    r = c.post("/admin/reset?scope=langfuse&confirm=true")
    assert r.status_code == 200, r.text
    assert called == []  # blob purge belongs to the data wipe, not the trace wipe
    assert r.json()["langfuse_traces_deleted"] == 7


def _schema_tables() -> set[str]:
    """Include composed migrations and tables created by Store outside _SCHEMA.

    Imported DDL is part of the actual migration even when its CREATE text is not
    written literally in store.py. Keep the source scan too: migration bookkeeping
    tables created outside _SCHEMA must still receive a reset classification.
    """
    src = (Path(store_mod.__file__)).read_text() + "\n" + "\n".join(store_mod._SCHEMA)
    return set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([a-zA-Z_]+)", src))


def test_schema_inventory_includes_composed_migrations(monkeypatch):
    monkeypatch.setattr(store_mod, "_SCHEMA", [*store_mod._SCHEMA,
        "CREATE TABLE IF NOT EXISTS composed_customer_records (id TEXT)"])
    assert "composed_customer_records" in _schema_tables()


def test_reset_leaves_no_customer_data(isolated_store):
    """Fail closed if ANY data table is missing from the reset set.

    The customer-facing promise (assured to Deva) is that RESET makes the app completely fresh —
    no prior scan results, decisions, inventory, disposition audit, or learned review memory
    survive. Concretely: every schema table except the explicit config/program survivors must be
    in store._ANALYTICS_TABLES. A new table that stores scan/review output will trip this until
    it's either wiped or justified as config in _CONFIG_SURVIVORS."""
    all_tables = _schema_tables()
    wiped = set(store_mod.Store._ANALYTICS_TABLES)

    # 1) The reset list must not name a table the schema doesn't define (typo guard).
    assert wiped <= all_tables, f"reset names unknown tables: {sorted(wiped - all_tables)}"

    # 2) Every table is EITHER wiped OR an explicit config/program survivor — nothing falls
    #    through the cracks and silently persists across a reset.
    unclassified = all_tables - wiped - _CONFIG_SURVIVORS
    assert not unclassified, (
        f"these tables survive RESET but aren't declared config survivors — classify each as "
        f"data (add to _ANALYTICS_TABLES) or config (add to _CONFIG_SURVIVORS): {sorted(unclassified)}")

    # 3) The data the customer specifically asked to be gone is provably wiped.
    for must_wipe in ("scan_inventory", "scan_decisions", "disposition_audit", "org_memory",
                      "ai_spending_attempts", "ai_spending_budgets", "ai_spending_run_policies"):
        assert must_wipe in wiped, f"{must_wipe} must be wiped by RESET but isn't"


def test_reset_actually_deletes_rows_from_the_new_data_tables(isolated_store):
    """Beyond the table LIST, prove reset_analytics() empties the newly-added data tables."""
    s = isolated_store
    # seed a row in each newly-added table via its real writer
    s.save_decision("scan1", "f.docx", "action", "remediate", owner="demo@x", when="2026-07-14T00:00:00Z")
    s.add_org_memory("acme", "style", "Prefer sentence case", author="admin@x")
    assert s.get_decisions("scan1"), "precondition: decision seeded"
    assert s.list_org_memory("acme"), "precondition: org memory seeded"

    cleared = s.reset_analytics()

    assert "scan_decisions" in cleared and "org_memory" in cleared
    assert s.get_decisions("scan1") == {}, "scan_decisions survived reset"
    assert s.list_org_memory("acme") == [], "org_memory survived reset"
