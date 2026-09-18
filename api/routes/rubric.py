"""Rubric & rule-catalog endpoints."""
from __future__ import annotations
import json

from fastapi import APIRouter, Request
from pydantic import BaseModel

import core
from routes.system import _require_admin

router = APIRouter()


@router.get("/rubric")
def rubric():
    rb = core.active_rubric()
    return {"name": rb.name, "version": rb.version, "hash": rb.hash,
            "target": rb.cfg.get("conformance_target"), "threshold": rb.threshold,
            "criteria": rb.criteria}


@router.get("/rules")
def rules():
    catalog = json.loads((core.ACP / "config/rule-catalog.json").read_text())
    disabled = set(core.active_rubric().disabled)
    findings = core.store.rule_findings()
    # Exclude the _meta key; enrich each rule with runtime state.
    return {
        fmt: [
            {
                **r,
                "enabled": r["id"] not in disabled,
                "findings": findings.get(r["id"], 0),
                # wcag_level already present in the enriched catalog; fall back for
                # older catalog rows that only have the legacy wcag key.
                "level": r.get("wcag_level") or ("AA" if r.get("wcag") == "SC_1_4_3" else "A"),
            }
            for r in items
        ]
        for fmt, items in catalog.items()
        if fmt != "_meta"
    }


@router.get("/rules/explanations")
def rule_explanations(request: Request):
    """What ACP actually checks for each WCAG criterion, per format (Settings → Rule explanations).

    Read-only product metadata, gated exactly like GET /rules: behind the access gate, open to any
    signed-in user who can read the rules list. The explanations are static per process (cached in
    rule_explanations.build); the rubric state and settings are read per request because they are
    the parts an administrator can change. No `/rules/{x}` route exists anywhere, so this literal
    path cannot be shadowed."""
    import rule_explanations
    user = getattr(getattr(request, "state", None), "user_email", None)
    return rule_explanations.payload(user=user)


class RubricUpdate(BaseModel):
    disabled_rules: list[str] | None = None
    compliant_threshold: int | None = None


@router.put("/rubric")
def update_rubric(body: RubricUpdate, request: Request):
    # Owner-only, same gate as PUT /settings: the rubric is the GLOBAL scoring policy (disabled
    # rules + compliant threshold), so any allow-listed user could otherwise rewrite how every
    # tenant is scored with a direct call. No-op when no owner is configured (local dev).
    _require_admin(request)
    # WRITES TO THE DATABASE, NOT TO THIS CONTAINER. It used to write
    # `config/rubric.active.json` into the replica that served the request, which no deployment
    # mounts a volume for — so the other API replicas and every worker container kept the old
    # policy, and it was lost on the next restart. Workers are where scoring happens, so the
    # change was invisible to the tier that applies it. See `core.active_rubric`.
    #
    # Starts from what is IN FORCE rather than from a file, so an edit composes with whatever the
    # last one left, wherever it came from. `fresh=True` because a writer must not build on a
    # cached copy that is up to five seconds old.
    cfg = dict(core.active_rubric(fresh=True).cfg)
    if body.disabled_rules is not None:
        cfg["disabled_rules"] = sorted(set(body.disabled_rules))
    if body.compliant_threshold is not None:
        cfg["compliant_threshold"] = int(body.compliant_threshold)
    core.store.set_setting(core._RUBRIC_SETTING, json.dumps(cfg, indent=2, sort_keys=True))
    core.invalidate_rubric_cache()
    rb = core.active_rubric(fresh=True)
    return {"hash": rb.hash, "disabled_rules": sorted(rb.disabled), "threshold": rb.threshold}
