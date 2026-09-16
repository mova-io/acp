"""System & meta endpoints: liveness, SPA auth config, schedule, hub landing page."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

import core
import readiness_phase_diagnostics as _readiness
from swallowed import swallowed

router = APIRouter()

# A secret REFERENCE is an environment-variable name (e.g. AZURE_OPENAI_API_KEY), never a key
# value. This shape is what keeps a pasted key out of the DB: a real key won't match it.
_SECRET_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,64}$")


def _require_admin(request: Request) -> None:
    """Platform-admin gate for platform-mutating admin endpoints. The SPA hides these
    behind the Platform Admin role, but the API must enforce it too — any
    allow-listed user could otherwise flip platform settings (AI mode, Drive
    mirror, worker pool, data reset) with a direct call. Admin = the protected
    OWNER_EMAIL (the anti-lockout identity the allowlist can never drop) OR any
    ACP_ADMIN_EMAILS entry — the same `core.is_admin` set the SPA's is_scope_owner
    flag reads, so UI and API never disagree. No-op when no owner is configured
    (local dev without auth)."""
    if not core.OWNER_EMAIL:
        return
    email = (getattr(request.state, "user_email", None) or "").lower()
    if not core.is_admin(email):
        raise HTTPException(403, "admin access required")


def _require_owner(request: Request) -> None:
    """Owner-only gate — stricter than _require_admin. Managing WHO is an admin is the root-of-trust
    action, so only the protected OWNER_EMAIL may promote/demote admins; an admin cannot grant admin
    (nor remove the owner). No-op when no owner is configured (local dev without auth)."""
    if not core.OWNER_EMAIL:
        return
    email = (getattr(request.state, "user_email", None) or "").lower()
    if not core.is_owner(email):
        raise HTTPException(403, "owner access required")


@router.post("/admin/reset")
def admin_reset(request: Request,
                scope: str = Query("all", pattern="^(all|grafana|langfuse)$"),
                confirm: bool = Query(False)):
    _require_owner(request)   # destructive + irreversible (wipes data + blobs) — owner-only
    """Reset demo data so the charts start fresh (admin, audited).
    scope=grafana → clear the ACP Postgres analytics tables (Grafana + in-app
    charts); scope=langfuse → delete the project's Langfuse traces; all → both.
    Settings (worker count, AI mode, schedule, rubric) are preserved."""
    if not confirm:
        raise HTTPException(400, "confirmation required — pass confirm=true")
    cleared: list[str] = []
    lf_deleted = 0
    blobs_purged: dict = {}
    if scope in ("all", "grafana"):
        cleared = core.store.reset_analytics()
        # reset_analytics drops the remediation_state / applied_fixes / … ROWS but the fixed
        # file bytes, cached originals and previews live in blob storage — purge them too so
        # "reset" is a true clean slate and no legacy remediation survives. Best-effort:
        # no-op when blob isn't configured (local dev), never raises into the reset.
        try:
            from blob import purge_all
            blobs_purged = purge_all()
        except Exception as e:  # pragma: no cover - defensive; blob purge must not fail the reset
            blobs_purged = {"error": str(e)}
    if scope in ("all", "langfuse"):
        lf_deleted = core.reset_langfuse_traces()
    # Logged AFTER the wipe so the reset itself is recorded.
    _blob_total = sum(v for v in blobs_purged.values() if isinstance(v, int) and v > 0)
    core.store.log_decision("admin", "demo.reset",
                            detail=f"scope={scope} · tables={len(cleared)} · langfuse_traces={lf_deleted} · blobs={_blob_total}")
    return {"scope": scope, "cleared_tables": cleared, "langfuse_traces_deleted": lf_deleted,
            "blobs_purged": blobs_purged}


@router.post("/me/reset-data")
def reset_my_data(request: Request, confirm: bool = Query(False)):
    """Self-service reset (destructive + irreversible, DB rows only): clears the SIGNED-IN USER'S
    OWN scans and everything tied to them, so two people testing concurrently never clear each
    other's work — unlike /admin/reset, which wipes every user's data and is owner-only. No admin
    gate here on purpose: this only ever touches the caller's own rows (see
    store.reset_user_data's docstring for exactly what is and isn't cleared — notably, it does not
    purge Blob/Drive copies, and it does not delete the immutable decision_log; it appends to it)."""
    if not confirm:
        raise HTTPException(400, "confirmation required — pass confirm=true")
    owner = (getattr(request.state, "user_email", None) or "demo")
    result = core.store.reset_user_data(owner)
    core.store.log_decision(owner, "reset_user_data",
                            detail=f"tables={len(result['cleared_tables'])}")
    return result


@router.post("/alerts/webhook")
async def alert_webhook(request: Request, key: str = Query("")):
    """Receiver for Grafana alert notifications (public path, shared-secret).
    Each firing/resolved alert is recorded in the immutable decision log, so
    delivery is visible in-product (audit feed + the Grafana 'Recent decisions'
    panel) without needing external SMTP."""
    if key != core.ALERT_KEY:
        raise HTTPException(401, "bad alert key")
    try:
        body = await request.json()
    except Exception:
        body = {}
    alerts = body.get("alerts") or []
    for a in alerts:
        labels = a.get("labels", {}) or {}
        name = labels.get("alertname", "alert")
        status = a.get("status", "firing")
        summary = (a.get("annotations", {}) or {}).get("summary", "")
        core.store.log_decision("grafana", f"alert.{status}",
                                detail=f"{name}: {summary}".strip(" :"))
    # If a downstream HITL webhook is configured, forward a compact note too.
    if alerts and core.HITL_WEBHOOK:
        try:
            import httpx
            httpx.post(core.HITL_WEBHOOK, json={"event": "grafana.alert", "alerts": [
                {"name": (a.get("labels", {}) or {}).get("alertname"),
                 "status": a.get("status")} for a in alerts]}, timeout=6)
        except Exception:
            swallowed("routes.system.alert_webhook: posting the alert webhook failed")
    return {"received": len(alerts)}


@router.get("/admin/allowlist")
def get_allowlist():
    """Test users who can use the app: the editable list, the protected owner (can't be
    removed), and any always-allowed domains. `invite_enabled` tells the UI whether the opt-in
    guest-invite action is configured (ADR 0033) — it hides when the credential is unset."""
    import invites
    return {"emails": core.store.get_allowlist(),
            "owner": core.OWNER_EMAIL,
            "domains": core.ALLOWED_DOMAINS,
            "invite_enabled": invites.invite_configured()}


@router.post("/admin/invite")
def invite_tester(body: dict, request: Request):
    """Invite an external tester as an Entra B2B guest AND add them to the allowlist in one step
    (ADR 0033). Owner-only. 409 when the invite credential isn't configured — the feature ships
    dark, so this path simply doesn't exist until an operator opts in. Least-privilege: this sends
    a guest invite (Graph User.Invite.All), it does NOT create a tenant user."""
    _require_owner(request)   # manages the login perimeter — owner-only
    import invites
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "a valid email is required")
    if not invites.invite_configured():
        raise HTTPException(409, "guest invite is not configured — set ACP_INVITE_* to enable it")
    try:
        result = invites.send_guest_invite(email, body.get("redirect_url"))
    except Exception as e:
        raise HTTPException(502, f"invite failed: {e}")
    # Auto-add to the allowlist so the guest is admitted on first sign-in — keep the existing list,
    # dedupe, and never drop the owner (same anti-lockout rule as the PUT path).
    keep = list(dict.fromkeys([*core.store.get_allowlist(), email]
                              + ([core.OWNER_EMAIL] if core.OWNER_EMAIL else [])))
    saved = core.store.set_allowlist(keep)
    core.store.log_decision("admin", "settings.invite",
                            detail=f"invited {email} as a guest and added to the allowlist")
    return {"email": email, "emails": saved, "owner": core.OWNER_EMAIL,
            "redemption_url": result.get("redemption_url"), "status": result.get("status")}


@router.put("/admin/allowlist")
def set_allowlist(body: dict, request: Request):
    """Replace the editable test-user list. The owner is always kept (anti-lockout)."""
    _require_owner(request)   # manages the login perimeter — owner-only
    emails = body.get("emails", [])
    if not isinstance(emails, list):
        raise HTTPException(400, "emails must be a list of strings")
    if core.OWNER_EMAIL:
        emails = list(emails) + [core.OWNER_EMAIL]   # never drop the owner
    saved = core.store.set_allowlist(emails)
    core.store.log_decision("admin", "settings.allowlist",
                            detail=f"test-user list set to {len(saved)} email(s)")
    return {"emails": saved, "owner": core.OWNER_EMAIL}


_PEOPLE_ROLES = {"user", "admin"}
_PEOPLE_PROVIDERS = {"google", "microsoft"}


def _people_payload(can_manage: bool = True) -> dict:
    """The People screen's payload: the roster, plus what the screen needs to render it.

    The merge that builds the roster moved to `core.people_with_access` and is no longer done
    here. It was done here, and the role-assignment endpoint answered the same question from
    `store.get_people()` alone — so the screen listed people it then refused to act on. One
    function now, because two implementations of "who has access" is how they came to differ.
    """
    import invites
    admins = set(core.store.get_admins()) | set(core.ADMIN_EMAILS)
    people = [{**person, 'platform_role_admin': person.get('role') == 'admin' or person['email'] in admins,
               'platform_role_locked': person['email'] in core.ADMIN_EMAILS}
              for person in core.people_with_access()]
    return {"people": people,
            "invite_enabled": invites.invite_configured(), "domains": core.ALLOWED_DOMAINS,
            "can_manage": can_manage}


@router.get("/admin/people")
def list_people(request: Request):
    _require_admin(request)
    email = (getattr(request.state, "user_email", None) or "").lower()
    return _people_payload(can_manage=not core.OWNER_EMAIL or core.is_owner(email))


@router.post("/admin/people")
def add_person(body: dict, request: Request):
    """Authorize an existing identity and start provider-specific onboarding."""
    _require_owner(request)
    email = (body.get("email") or "").strip().lower()
    provider = (body.get("provider") or "").strip().lower()
    role = (body.get("role") or "user").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "a valid email is required")
    if provider not in _PEOPLE_PROVIDERS:
        raise HTTPException(400, "provider must be google or microsoft")
    if role not in _PEOPLE_ROLES:
        raise HTTPException(400, "role must be user or admin")
    if email == core.OWNER_EMAIL:
        raise HTTPException(409, "the owner already has access")
    if any(p["email"] == email for p in _people_payload()["people"]):
        raise HTTPException(409, "this person already has access")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status, redemption_url, failure = "access_ready", None, None
    if provider == "microsoft":
        import invites
        if invites.invite_configured():
            try:
                invited = invites.send_guest_invite(email, body.get("redirect_url"))
                redemption_url, status = invited.get("redemption_url"), "invited"
            except Exception as exc:
                status, failure = "failed", str(exc)
        else:
            status = "setup_required"
    keep = list(dict.fromkeys([*core.store.get_allowlist(), email]
                              + ([core.OWNER_EMAIL] if core.OWNER_EMAIL else [])))
    core.store.set_allowlist(keep)
    if role == "admin":
        core.store.set_admins([*core.store.get_admins(), email])
    actor = getattr(request.state, "user_email", None) or "admin"
    record = core.store.upsert_person({"email": email, "provider": provider, "role": role,
                                       "status": status, "invited_at": now, "invited_by": actor,
                                       "redemption_url": redemption_url, "failure": failure})
    core.store.log_decision(actor, "settings.person.add",
                            detail=f"{email} · {provider} · {role} · {status}")
    return {"person": record, **_people_payload()}


# `{email}`, NOT `{email:path}`, AND THAT ONE WORD IS A BUG FIX (2026-09-05).
#
# The `:path` converter matches `.*` — slashes included — so `PUT /admin/people/{email:path}`
# also matched `/admin/people/alice@hosp.org/role`, binding email to "alice@hosp.org/role".
# `routes/__init__.py` includes `system` first and `workspace_roles_admin` last, so this route
# won the match and `assign_person_role` — the endpoint the People screen's workspace-role
# dropdown calls — was UNREACHABLE. Every assignment landed here instead, failed the roster
# lookup on an address with "/role" glued to it, and answered `404 person not found`: the red
# line an administrator saw on the row they had just used, and the original bug report.
#
# It survived because every test for the shadowed endpoint calls the Python function directly
# (`adm.assign_person_role(...)` — tests/test_workspace_roles_admin.py) rather than issuing a
# request, so the routing layer was never exercised by anything. The function was correct the
# whole time. See tests/test_people_route_shadowing.py, which asks the app over HTTP.
#
# The default converter is `[^/]+`, which is the right shape for an address in one path segment:
# the SPA sends it through encodeURIComponent and no provider issues an address containing a
# literal slash. `:path` bought nothing here and cost the feature.
@router.put("/admin/people/{email}")
def update_person(email: str, body: dict, request: Request):
    _require_owner(request)
    target = email.strip().lower()
    if target == core.OWNER_EMAIL:
        raise HTTPException(409, "the owner cannot be changed")
    current = next((r for r in _people_payload()["people"] if r["email"] == target), None)
    if current is None:
        raise HTTPException(404, "person not found")
    role = (body.get("role") or current.get("role") or "user").lower()
    status = (body.get("status") or current.get("status") or "access_ready").lower()
    if role not in _PEOPLE_ROLES:
        raise HTTPException(400, "role must be user or admin")
    if status not in {"access_ready", "invited", "setup_required", "failed", "suspended"}:
        raise HTTPException(400, "unsupported status")
    allowed = [e for e in core.store.get_allowlist() if e != target]
    if status != "suspended":
        allowed.append(target)
    core.store.set_allowlist(allowed + ([core.OWNER_EMAIL] if core.OWNER_EMAIL else []))
    admins = [e for e in core.store.get_admins() if e != target]
    if role == "admin" and status != "suspended":
        admins.append(target)
    core.store.set_admins(admins)
    record = core.store.upsert_person({**current, "role": role, "status": status,
                                       "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    actor = getattr(request.state, "user_email", None) or "admin"
    core.store.log_decision(actor, "settings.person.update", detail=f"{target} · {role} · {status}")
    return {"person": record, **_people_payload()}


# `{email}` for the same reason as the PUT above. Nothing registers a DELETE under a person
# today, so this one shadows nothing yet — which is exactly why it is worth narrowing now: the
# next `/admin/people/{email}/<something>` endpoint would be swallowed silently, and the failure
# reads as "that person does not exist" rather than as a routing problem.
@router.delete("/admin/people/{email}")
def delete_person(email: str, request: Request):
    _require_owner(request)
    target = email.strip().lower()
    if target == core.OWNER_EMAIL:
        raise HTTPException(409, "the owner cannot be removed")
    core.store.set_allowlist([e for e in core.store.get_allowlist() if e != target])
    core.store.set_admins([e for e in core.store.get_admins() if e != target])
    core.store.remove_person(target)
    # Otherwise this process remembers them as rostered and never rebuilds the record, so a
    # domain-admitted user removed here would sign in again and be invisible — the exact state
    # just-in-time roster creation exists to end. Note that removal does NOT revoke a domain
    # admission: they will be re-created with the configured default role on their next sign-in,
    # which is why suspending is the action that withholds access and removing is the one that
    # forgets. `forget_rostered` is what makes the re-creation actually happen.
    core.forget_rostered(target)
    actor = getattr(request.state, "user_email", None) or "admin"
    core.store.log_decision(actor, "settings.person.remove", detail=target)
    return _people_payload()


@router.get("/admin/admins")
def get_admins():
    """Who holds Platform Admin. Three tiers, so the UI can render each correctly:
      owner       — ACP_OWNER_EMAIL, immutable (root of trust, can never be demoted).
      env_admins  — ACP_ADMIN_EMAILS, permanent (set at deploy, not removable from the UI).
      admins      — the owner-managed set (store), promotable/demotable right here.
    Whether THIS caller may EDIT the managed set is the `is_owner` flag on GET /me — the PUT is
    owner-only and enforces it regardless."""
    return {"owner": core.OWNER_EMAIL,
            "env_admins": sorted(core.ADMIN_EMAILS),
            "admins": core.store.get_admins()}


@router.put("/admin/admins")
def set_admins(body: dict, request: Request):
    """Replace the owner-managed admin set. OWNER-ONLY (managing admins is the root-of-trust action,
    stricter than the admin-gated allowlist). The owner and env admins are never stored here and
    can't be demoted through this path."""
    _require_owner(request)
    emails = body.get("emails", [])
    if not isinstance(emails, list):
        raise HTTPException(400, "emails must be a list of strings")
    # The owner and env-admins are grants from elsewhere; keep them out of the managed set so the
    # list stays exactly "who the owner promoted" and a redundant entry can't imply it's removable.
    drop = {core.OWNER_EMAIL, *core.ADMIN_EMAILS}
    emails = [e for e in emails if (e or "").strip().lower() not in drop]
    saved = core.store.set_admins(emails)
    core.store.log_decision("admin", "settings.admins",
                            detail=f"platform-admin set to {len(saved)} email(s)")
    return {"owner": core.OWNER_EMAIL, "env_admins": sorted(core.ADMIN_EMAILS), "admins": saved}


@router.get("/me/access")
def my_access(request: Request):
    """What this identity may see and do (PRD §13).

    NOT AUTHORIZATION — a description of it. The SPA reads this to decide which tabs to render and
    whether a button says "Start" or "View"; every route still enforces its own capability
    (slice 4). If this endpoint and a route ever disagree, the route wins and the UI was merely
    wrong about what it offered. That ordering is the whole reason PRD §11 exists: "Hiding a tab
    alone is not considered a security control."

    Unauthenticated callers get an empty identity's answer rather than a 401, because the SPA
    calls this during sign-in bootstrapping — and with the flag off that answer is today's access,
    which is what it must be for a signed-out shell to render exactly as it does now.
    """
    import workspace_roles as wr
    email = getattr(request.state, "user_email", None)
    return wr.access_for_email(core.store, email, owner_email=core.OWNER_EMAIL,
                               is_suspended=_is_suspended)


def _is_suspended(email: str) -> bool:
    """Is this person suspended? PRD §14: a suspended user has no effective permissions.

    Read from the managed-person record, which is where the People screen already writes it —
    rather than inferred from absence in the allowlist, which would also be true of somebody who
    was never added and of the demo path where no allowlist is configured at all.
    """
    return core.suspended_in_store(email)


@router.post("/admin/workspace-roles/bootstrap")
def bootstrap_workspace_roles(request: Request, body: dict | None = None):
    """Seed the six built-in workspace roles and map existing people onto them (PRD §15 step 1).

    OWNER-ONLY, and DRY BY DEFAULT. Pass {"apply": true} to write; anything else previews. The
    default is the safe direction because this is the migration's "Observe" step: an administrator
    is meant to read the generated assignments — who becomes Platform Admin, who becomes
    Compliance Manager — BEFORE those rows mean anything, and a preview you have to remember to
    ask for is one somebody skips.

    Safe to run repeatedly. Existing roles are not overwritten and already-assigned people are not
    reassigned, so a second call after an administrator has tightened somebody's role does not
    undo it (tests/test_workspace_roles_store.py pins both).

    WHETHER THESE ROWS ENFORCE ANYTHING DEPENDS ON THE ROLLOUT RUNG, which is reported in the
    response as `rollout` so the caller can see whether what they just wrote is inert — writing
    roles and believing they took effect is the one misreading this endpoint could invite. See
    api/workspace_rollout.py for the ladder, and GET /admin/workspace-roles/preflight for whether
    it is safe to climb it.
    """
    _require_owner(request)
    import workspace_roles as wr
    # `is True`, not truthiness. A bare `bool(...)` applies on ANY non-empty value, so a client
    # that serialises booleans as strings would migrate a live deployment by sending
    # {"apply": "false"} — the request that most clearly says do not. The only value that writes
    # is a JSON `true`.
    apply = (body or {}).get("apply") is True
    actor = getattr(request.state, "user_email", None) or "owner"
    out = wr.bootstrap(core.store, owner_email=core.OWNER_EMAIL, actor=actor, dry_run=not apply)
    if apply:
        core.store.log_decision(actor, "role.migration",
                                detail=f"seeded {len(out['roles_created'])} role(s), "
                                       f"assigned {sum(1 for a in out['assignments'] if a['applied'])}")
    return out


@router.get("/admin/workspace-roles/preflight")
def workspace_roles_preflight(request: Request):
    """Would advancing the rollout one rung break anybody? (PRD §15.)

    OWNER-ONLY, AND NOT BECAUSE IT WRITES — it writes nothing. It reports every managed person's
    email next to the capabilities they are about to lose, which is a personnel-shaped answer, and
    it is read at exactly the moment somebody is deciding whether to narrow other people's access.
    The person making that decision is the owner; `roles.manage` is the wrong gate because a role
    holding it could use this to enumerate the whole workspace's standing.

    READ IT, DO NOT POLL IT. It walks every person and resolves each one's role, so its cost is
    linear in headcount — fine once before a deployment, wasteful on a dashboard refresh.
    """
    _require_owner(request)
    import workspace_preflight as preflight
    return preflight.report(core.store, owner_email=core.OWNER_EMAIL,
                            routes=core.enumerate_api_routes(request.app),
                            is_suspended=_is_suspended)


@router.put("/workers")
def set_workers(request: Request, count: int = Query(..., ge=0, le=16)):
    """Admin: live-scale the in-process worker pool (0–16). Persisted + audited.
    Scaled-down workers finish their current job before exiting."""
    _require_admin(request)
    new = core.set_worker_count(count)
    core.store.log_decision("admin", "settings.worker_count",
                            detail=f"worker pool scaled to {new}")
    return {"workers": new}


def _build_info() -> dict:
    """Build provenance, plus whether this image was actually stamped by deploy.sh.

    deploy.sh stamps ACP_BUILD_VERSION with a CalVer string (e.g. 2026.7.10.005859) at
    deploy time. The Dockerfile's `ARG BUILD_VERSION=dev` default is what a bare
    `docker build` leaves behind, so an unstamped image is one that never went through
    deploy.sh. Such an image must not pass for a release: /healthz reports ok=false, so
    an operator sees it immediately instead of the app quietly serving "dev". ACA runs no
    health probe on this route, so this signal is advisory, not a rollout gate.

    `commit` IS THE COMMIT THIS IMAGE WAS BUILT FROM, and it is here because `version` and
    `built_at` cannot answer the question anybody actually asks after a merge: is THIS change
    live? On 2026-09-06 establishing that took a CalVer stamp, two workflow-run timestamps and a
    cancelled deploy run to disambiguate, and the answer was still an inference from when the
    build happened rather than from what it contained. One sha turns that into a read.

    It is a REPORT, never a gate. `ok` and `version_stamped` deliberately do not consider it: an
    older image predating this field, or one built by a bare `docker build`, is not unhealthy —
    it simply cannot name its commit, and says so with null rather than with a plausible string.
    deploy.sh appends `-dirty` when it builds from a working directory with uncommitted changes,
    because a sha that silently omits them names a tree that never shipped.
    """
    import os
    v = (os.environ.get("ACP_BUILD_VERSION") or "").strip()
    return {"version": v or "dev",
            "built_at": os.environ.get("ACP_BUILD_TIME") or None,
            "commit": (os.environ.get("ACP_BUILD_SHA") or "").strip() or None,
            "version_stamped": v.lower() not in ("", "dev")}


@router.get("/healthz")
def healthz():
    info = _build_info()
    return {"ok": info["version_stamped"], "service": "acp",
            "rubric_hash": core.active_rubric().hash, **info}


def pdf_engine_status() -> dict:
    """Is the PDF analyser importable in THIS process?

    worker-python is not vendored (unlike the Office analysers, which ADR 0012 brought in) —
    it is loaded at runtime from ACP_PDF_ENGINE. When that path is wrong the failure surfaces
    as a ModuleNotFoundError partway through scanning a PDF, which reads as "the scan broke"
    rather than "an engine was never installed". Probing it here turns a mid-scan crash into
    something an operator can see before anyone runs a scan.

    Import-only: the module is imported and discarded, never used to analyse anything.
    """
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path

    import scanner
    engine = _Path(str(scanner.WP))
    if not engine.exists():
        return {"available": False, "path": str(engine),
                "reason": "ACP_PDF_ENGINE path does not exist"}
    added = str(engine) not in _sys.path
    if added:
        _sys.path.insert(0, str(engine))
    try:
        found = importlib.util.find_spec("analysers.pdf_analyser") is not None
    except Exception as exc:
        return {"available": False, "path": str(engine),
                "reason": f"{exc.__class__.__name__}: {exc}"}
    return {"available": found, "path": str(engine),
            "reason": None if found else "analysers.pdf_analyser not importable from that path"}


def _langfuse_status() -> dict:
    """Secret-free, ingestion-aware status for the optional trace exporter.

    Langfuse's landing/readiness endpoint is deliberately not called here: its HTTP 200 only
    establishes that the service is up, not that trace ingestion accepts writes (the two
    disagreed during the September 2026 incident).  The exporter state is evidence from real
    writes.  Keep the shallow check explicit so clients cannot silently relabel "not checked" as
    healthy, and whitelist fields so a future SDK exception or configuration value never leaks
    through this public endpoint.
    """
    safe = {
        "configured", "state", "exporting", "pending", "attempts", "successes", "failures",
        "consecutive_failures", "last_attempt_at", "last_success_at", "last_error_at",
        "last_duration_s", "next_retry_at",
    }
    try:
        import lf as _lf
        raw = _lf.exporter_health()
        if not isinstance(raw, dict):
            raise TypeError("exporter health is not a mapping")
        ingestion = {key: raw[key] for key in safe if key in raw}
        ingestion.setdefault("configured", bool(getattr(_lf, "_ENABLED", False)))
        ingestion.setdefault("state", "unknown")
    except Exception as exc:  # telemetry diagnostics must never break deployment readiness
        ingestion = {"configured": False, "state": "unknown",
                     "error": f"{exc.__class__.__name__}: exporter status unavailable"}
    return {
        "shallow_health": {"checked": False, "state": "not_checked"},
        "ingestion_exporter": ingestion,
    }


@router.get("/readyz")
def readyz():
    """Functional readiness: can this deployment actually do work right now?

    DELIBERATELY SEPARATE FROM /healthz. That route answers "is this image what we think it
    is" — build provenance — and its docstring notes ACA runs no probe against it. Readiness
    is a different question with a different failure mode, and conflating them is a trap: if a
    platform probe were ever pointed at a combined route, a worker-tier outage would restart
    the API container, which cannot fix a worker tier and loses the API too.

    So: /healthz stays liveness + provenance, this is readiness, and an alert targets
    `ready` or a specific entry in `degraded`.

    `degraded` is a list of machine-readable reasons rather than prose, so a monitor can alert
    on one condition without pattern-matching a sentence.
    """
    workers = _readiness.run("worker_heartbeat", lambda: core.store.worker_tier_status())
    # Aggregate only: deployment automation needs to know whether replacing worker revisions
    # would interrupt customer work, but a public readiness response must never expose tenant,
    # filename, job id, or payload data. PostgreSQL is authoritative; Redis is intentionally not
    # consulted here because a Redis rollover is one of the failures this gate must detect.
    try:
        _queue_stats = _readiness.run("queue_summary", lambda: core.store.job_stats(owner=None))
        queue = {
            "queued": int(_queue_stats.get("queued") or 0),
            "running": int(_queue_stats.get("running") or 0),
            # Of those queued rows, the ones a worker could actually claim now — run_after due,
            # attempts below max_attempts, the same predicate claim_job uses. `queued` stays the
            # raw row count because it is the true state of the table and the number an operator
            # investigating a backlog wants.
            "claimable": int(_readiness.run("queue_claimable", lambda: core.store.claimable_job_count())),
        }
        # `active` is the DEPLOY GATE's number (redeploy.sh reads exactly this field), so it must
        # mean "work a worker cutover would disturb", not "rows in the table". It counted every
        # queued row, including rows no worker can ever claim — a retry past max_attempts, or one
        # deferred behind a future run_after. Such a row is permanent in job_stats, so it blocked
        # every deploy indefinitely while representing nothing: production sat at queued 1 /
        # running 0 for forty minutes on 2026-09-09 with healthy workers, failing four deploys in
        # a row. Counting claimable work keeps the protection (a claimable job is about to be
        # picked up; a running one already has been) without the wedge.
        queue["active"] = queue["running"] + queue["claimable"]
        queue["available"] = True
    except Exception as exc:  # fail closed in deploy automation; keep readiness diagnostic alive
        queue = {"queued": None, "running": None, "active": None, "available": False,
                 "error": f"{exc.__class__.__name__}: queue status unavailable"}
    # Defended: a per-role read must never be able to 500 the readiness endpoint, the same posture
    # the source and vision probes below take. An empty dict reads as "no role ever beaten", which
    # is the honest answer when this cannot be established.
    try:
        role_status = _readiness.run("role_heartbeats", lambda: core.store.worker_roles_status())
    except Exception as exc:  # pragma: no cover - defensive: a role probe must not break /readyz
        role_status = {"error": f"{exc.__class__.__name__}: {exc}"}
    local_pool = int(getattr(core, "WORKERS", 0) or 0)
    # Either tier can man the queue: the split topology (#113) runs the pool in a standalone
    # worker container, the single-tier setup runs it in-process. Readiness is the OR — the
    # scan-start guard in routes/scans.py makes exactly the same call.
    can_run_scans = bool(local_pool) or workers["alive"]

    # Capacity state mirrors discovery preflight — "starting" means the queue is durable and
    # scans can be submitted; "unavailable" means the worker tier was never started at all.
    if can_run_scans:
        capacity_state = "ready"
    elif workers["ever_seen"]:
        capacity_state = "starting"
    else:
        capacity_state = "unavailable"

    degraded: list[str] = []
    if core.dedicated_release_enabled() and not (role_status.get("release") or {}).get("alive"):
        degraded.append("release_worker_unavailable")
    elif core.dedicated_release_enabled() and int((role_status.get("release") or {}).get("pool_size") or 0) < 3:
        degraded.append("release_worker_capacity_insufficient")
    if not can_run_scans:
        degraded.append("no_workers" if workers["ever_seen"] else "worker_tier_never_started")
    pdf = _readiness.run("pdf_engine", pdf_engine_status)
    if not pdf["available"]:
        degraded.append("pdf_engine_missing")
    try:
        redis_status = _readiness.run("redis", core.redis_dependency_status)
    except Exception as exc:  # pragma: no cover - diagnostics must not 500 readiness
        redis_status = {"configured": bool(getattr(core, "REDIS_URL", "")),
                        "reachable": False, "tls": None, "topology": "unknown",
                        "reason": f"{exc.__class__.__name__}: Redis status unavailable"}
    if redis_status["configured"] and not redis_status["reachable"]:
        degraded.append("redis_unavailable")

    # Source-adapter readiness, reported INFORMATIONALLY — deliberately NOT folded into `degraded`.
    # A deployment that scans only Drive/SharePoint legitimately has no SMB config, so an
    # unconfigured SMB source is not a deployment fault and must not flip `ready`. Surfacing it here
    # gives the monitor and the Content Sources UI one place to read WHY an SMB scan would return an
    # empty estate — before one is started — which is the whole point of describe_smb_readiness
    # (config-only: it reads env and touches no network). Imported lazily, exactly as the scanner
    # does, and defended so a source probe can never 500 the readiness endpoint itself.
    try:
        import smb_source
        smb_ready = _readiness.run("sources_config", smb_source.describe_smb_readiness)
    except Exception as exc:  # pragma: no cover - defensive: a source probe must not break /readyz
        smb_ready = {"ready": False, "error": f"{exc.__class__.__name__}: {exc}"}

    # Vision/GPU readiness — reported INFORMATIONALLY (like sources.smb), NOT folded into `degraded`.
    # A text-only or AI-off deployment is not "degraded" for lacking a vision model, and image findings
    # degrade safely to human review when vision is down (ADR 0039) — so a missing vision model is not a
    # scan-blocking fault the way a missing PDF engine is. But it is otherwise INVISIBLE until a scan
    # silently produces no alt drafts. This surfaces `vision_unavailable_reason` — which names the exact
    # cause (endpoint unreachable, or the configured model not present: a typo, a failed pull, or a stale
    # admin override pinning a torn-down pod's model) AND the fix — so a monitor / the AI-settings UI
    # catches it up front. Probed via ai's memoised tags cache (no per-request network hit); defended so
    # a vision probe can never 500 /readyz.
    try:
        import ai as _ai
        import providers as _providers
        vision = {"ready": _readiness.run("vision_available", _ai.vision_is_available),
                  "reason": _readiness.run("vision_reason", _ai.vision_unavailable_reason),
                  "model": _ai.OLLAMA_VISION_MODEL,
                  "zone": _providers.zone_for_url(_ai.OLLAMA_BASE_URL)}
    except Exception as exc:  # pragma: no cover - defensive: a vision probe must not break /readyz
        vision = {"ready": False, "reason": f"{exc.__class__.__name__}: {exc}"}

    # Tagged-PDF renderer readiness — INFORMATIONAL, like vision and sources.smb, and for the same
    # reason: a deployment that cannot render a PDF/UA-1 ACR export is not thereby unable to scan,
    # assess or remediate anything. Folding it into `degraded` would flip `ready` false for the
    # whole deployment over one export route, which is precisely the mistake the container-probe
    # note below warns about.
    #
    # WHY IT IS HERE AT ALL. It is the one capability in this app whose absence is invisible until
    # somebody needs it and gets a 503 — and it depends on a SYSTEM library (Pango, via WeasyPrint)
    # that pip cannot supply, so "the requirements pinned it" is not the same claim as "this
    # container can produce a tagged PDF". On 2026-09-02 that gap could only be closed by
    # reproducing the base image's dependency hash by hand and reasoning about what the layer must
    # contain; there was no surface that simply answered the question. Now there is.
    #
    # `variant` states WHAT would be produced rather than only whether something would be — an
    # untagged PDF is indistinguishable from this one to everybody except the reader it is for, so
    # "ready: true" alone would be the same shape of half-answer as ACP's own `pdf.tagged` rule
    # passing on an empty structure tree.
    try:
        import acr_export_pdf as _acr_pdf
        _renderer_ok = _readiness.run("pdf_renderer", _acr_pdf.is_available)
        report_pdf = {"ready": _renderer_ok,
                      "reason": None if _renderer_ok else _acr_pdf.MISSING_RENDERER,
                      "variant": _acr_pdf.PDF_VARIANT}
    except Exception as exc:  # pragma: no cover - defensive: a renderer probe must not break /readyz
        report_pdf = {"ready": False, "reason": f"{exc.__class__.__name__}: {exc}", "variant": None}

    return {
        "ready": not degraded,
        "capacity_state": capacity_state,
        "degraded": degraded,
        "workers": {**workers, "local_pool": local_pool, "can_run_scans": can_run_scans,
                    # PER-ROLE, because the fields above cannot answer "is each worker service
                    # running the build we shipped". They come from one shared heartbeat key that
                    # every worker overwrites, so with more than one service running (acp-worker
                    # and acp-discovery, since #1169) `version` and `pool_size` report whichever
                    # beat last — measured flapping between two services' answers in production on
                    # 2026-09-01. See store.worker_roles_status.
                    "roles": role_status},
        "queue": queue,
        # Langfuse is optional, so failed export is visible but never flips pipeline readiness.
        # `shallow_health` and `ingestion_exporter` stay separate: service-up was a false green
        # during the incident while real trace writes returned HTTP 500.
        "dependencies": {"redis": redis_status, "langfuse": _readiness.run("langfuse", _langfuse_status)},
        # `pdf` is the ANALYSER (can this deployment read a PDF); `pdf_renderer` is the tagged-PDF
        # WRITER (can it produce one). Deliberately not both under "pdf": they fail independently,
        # for unrelated reasons, and a single key would make one of them unanswerable.
        "engines": {"pdf": pdf, "vision": vision, "pdf_renderer": report_pdf},
        "sources": {"smb": smb_ready},
        "service": "acp",
    }


# ── Container-local readiness: the ONE route a platform probe may point at ────────────────
#
# WHY THIS EXISTS AS A THIRD HEALTH ROUTE. Neither of the two above can be a probe target.
#
#   /healthz  answers "is this the image deploy.sh stamped". It touches no dependency at all,
#             so it returns 200 from a replica that cannot reach the database — which is
#             exactly the replica a readiness gate has to hold traffic away from.
#   /readyz   answers "can this DEPLOYMENT do work" — worker tier, PDF engine. Its own
#             docstring says why pointing a probe at it would be a mistake: a worker-tier
#             outage would evict the API container, which cannot fix a worker tier and loses
#             the API too. `ready` there goes false for faults this container did not cause
#             and cannot cure by restarting.
#
# THE GAP THAT LEFT. With no probe configured, Azure Container Apps decides a new replica is
# ready as soon as its port accepts a TCP connection. uvicorn binds that port after the app's
# startup handlers finish, but binding is not the same as being able to serve, and on this
# deployment the difference is measurable. Sampled live during the deploy of #1151, against a
# single-replica app being swapped revision-for-revision:
#
#     t+20s   /healthz 200 in 0.39s      /readyz 000 after 25s     /config 000 after 25s
#     t+40s   /healthz 200 in 0.43s      /readyz 200 in 0.46s      /config 200 in 0.75s
#
# The non-database route was fast throughout; every database-backed route hung for the whole
# sampling window and then recovered on its own. Traffic was being sent to a replica that could
# not yet answer a database read. That window is what stranded a browser mid-submit on
# 2026-09-01 (the Discovery request that produced no preflight, no POST /scans and no job) —
# the client-side half of which is fixed in #1151; this is the server-side half, and it is the
# half that stops the window existing rather than making the browser survive it.
#
# WHAT THIS ROUTE CHECKS IS DELIBERATELY NARROW: this process, this container, its database.
# Nothing about the worker tier, the PDF engine, the vision model or any source adapter — all
# of which are legitimately absent or broken on a replica that is nonetheless perfectly able
# to serve, and none of which a restart of THIS container can repair. Adding a dependency here
# is not a small change: it hands the platform a new reason to take the API down.
#
# IT ANSWERS WITH A STATUS CODE, not a field. An httpGet probe reads the code and nothing else,
# so a 200 carrying {"ready": false} would be read as ready. Failure is 503.

# One in-flight database check at a time, process-wide.
#
# A sync FastAPI route runs on anyio's bounded worker threadpool (40 by default). A probe fires
# every few seconds forever, so if the database stops answering — the precise case this route
# exists to detect — an unguarded implementation parks one pooled thread per probe until the
# threadpool is gone, and takes down the replica's ability to serve anything at all. The gate
# turns that into at most one parked thread: while a check is outstanding, further probes are
# answered immediately and negatively, which is both the truthful answer and the cheap one.
_PROBE_DB_LOCK = threading.Lock()


def _probe_database() -> tuple[bool, str]:
    """(reachable, reason) for one database round-trip, never blocking on another probe."""
    if not _PROBE_DB_LOCK.acquire(blocking=False):
        # An earlier probe is still waiting on the database. Not "unknown" — a replica whose
        # last database read has not come back is not ready, and saying so is the point.
        return False, "db_check_in_flight"
    try:
        _readiness.run("db_ping", lambda: core.store.ping())
        return True, ""
    except Exception as exc:  # noqa: BLE001 — any failure to reach the DB means not ready
        # Class name only. This body is served to an unauthenticated caller, and a psycopg2
        # OperationalError's message carries the host, port and user from the DSN.
        return False, f"db_unreachable: {exc.__class__.__name__}"
    finally:
        _PROBE_DB_LOCK.release()


def probe_readyz(response: Response):
    """Is THIS container able to serve a database-backed request right now?

    The rollout gate. See the block comment above for why /healthz and /readyz cannot be it.
    """
    ok, reason = _probe_database()
    if not ok:
        response.status_code = 503
    return {"ready": ok, "checks": {"db": "ok" if ok else reason}, "service": "acp"}


_PROBE_HTTP_TIMEOUT_S = 2.0


@router.get("/probe/readyz")
async def bounded_probe_readyz(response: Response):
    # Ordinary sync routes consume AnyIO request slots. Readiness must still
    # answer when those slots are busy, without running a database call on the
    # event loop. The shared gate keeps at most one underlying ping outstanding
    # even after its HTTP deadline; a timeout never cancels or unlocks that ping.
    try:
        ok, reason = await asyncio.wait_for(
            asyncio.to_thread(_probe_database), timeout=_PROBE_HTTP_TIMEOUT_S)
    except asyncio.TimeoutError:
        ok, reason = False, 'db_check_timeout'
    if not ok:
        response.status_code = 503
    return {"ready": ok, "checks": {"db": "ok" if ok else reason}, "service": "acp"}


# How many recent scans the estate summary reports. The monitor decides what counts as a
# collapse (scripts/monitor.py:COLLAPSE_RATIO/COLLAPSE_WINDOW); this only has to return enough
# history for that policy to be evaluated, and to stay a bounded response on a busy estate.
MONITOR_SCAN_WINDOW = 20


@router.get("/monitor/estate")
def monitor_estate(request: Request):
    """Aggregate counts for the production monitor. COUNTS ONLY — never records.

    WHY THIS EXISTS AS A SEPARATE ROUTE. The monitor's deep checks previously read /scans and
    /hitl/queue through the X-E2E-Key gate bypass, and that could never work in production:
    core.E2E_KEY is None whenever IS_PROD, so the header was rejected on the only deployment
    anyone needs monitored. The two ways to "fix" that were to set ACP_ENABLE_TEST_BYPASS in
    production — reopening a whole-gate backdoor on a public deployment to power a health
    check — or to give monitoring its own door. This is the door.

    It is deliberately the NARROWEST thing that answers the two questions the monitor asks:

      - did the newest scan collapse?  → recent per-scan file COUNTS, newest first
      - how big is the review backlog? → one pending COUNT

    No filenames, no owner emails, no findings, no document content. If ACP_MONITOR_KEY ever
    leaks, what leaks with it is a handful of integers, which is the entire point of not
    reaching for the bypass that would have handed over the estate.

    Owner-agnostic ON PURPOSE (owner=None → every tenant). The old check was subtly broken in a
    second way: /scans is scoped to _owner(request), and on the keyed path no user_email is ever
    set, so it read the 'demo' user's scans — near-certainly empty — and would have reported
    "no completed scans at all" against a perfectly healthy estate. A monitor asks about the
    deployment, not about a user.
    """
    # Fail CLOSED and LOUD. 503 (not 404) so an unconfigured deployment is distinguishable from
    # a route that moved — the monitor reports the two differently and neither reads as healthy.
    if not core.MONITOR_KEY:
        raise HTTPException(503, "monitoring is not configured on this deployment (ACP_MONITOR_KEY unset)")
    presented = request.headers.get("x-monitor-key", "")
    # compare_digest, not ==, so a wrong key cannot be recovered a byte at a time.
    if not hmac.compare_digest(presented, core.MONITOR_KEY):
        raise HTTPException(401, "bad monitor key")

    # list_scans() filters to completed_at IS NOT NULL, which an ADR 0020 Discover-only run
    # never sets — the exact 2026-08-21 blind spot (see list_finished_scans' own docstring).
    # Found live 2026-08-28: this route hit that identical gap, so "did the newest scan
    # collapse" could never see a Discover-only run at all — its "newest scan" was stale by
    # definition on any deployment where Discover-only is now the default scan type.
    # list_finished_scans() is the narrower fix: completed_at OR discovered_at, excluding
    # anything still in-flight (so a scan that started seconds ago and has not listed a single
    # file yet cannot masquerade as "the newest," the same dishonest-zero shape this checks for
    # in the first place).
    scans = core.store.list_finished_scans() or []
    pending = core.store.list_hitl_queue(status="pending") or []

    # The background scheduler (core._do_scheduled_scan) runs under the service-account ADC
    # identity, not a user's own OAuth token — it can legitimately see far fewer files than a
    # user's manual scan of the same source, since ADC's Drive access is whatever the service
    # account was explicitly granted, not the signed-in user's full permission set. Found live
    # 2026-08-28: "newest scan is full-size" (below) has no way to tell that shape apart from a
    # genuine collapse — both look identical, a small scan sitting where a large one was. This
    # says whether the newest scan run WAS a scheduled sweep and how many files it saw, so the
    # two causes stop being indistinguishable from the same three numbers.
    #
    # Config (enabled/interval) and a file COUNT only — no owner email, no scan id, matching
    # this route's own counts-only contract.
    cfg = core.store.get_schedule()
    last_sweep = core.store.get_last_sweep()
    return {
        "service": "acp",
        "scans": {
            "total": len(scans),
            # Newest first, exactly as list_finished_scans orders them
            # (COALESCE(completed_at, discovered_at) DESC).
            "recent_files": [int(s.get("files") or 0) for s in scans[:MONITOR_SCAN_WINDOW]],
        },
        "inbox": {"pending": len(pending)},
        "sweep": {
            "enabled": bool(cfg.get("enabled")),
            "interval_minutes": cfg.get("interval_minutes"),
            "last_ok": last_sweep.get("ok") if last_sweep else None,
            "last_at": last_sweep.get("at") if last_sweep else None,
            "last_files": last_sweep.get("files") if last_sweep else None,
            # PRD Phase 3: True when the last sweep found (via Drive's sync cursor) that
            # nothing had changed and skipped the full re-scan entirely — last_files is then
            # None, not 0, deliberately: 0 already means "a scan ran and legitimately saw no
            # files under ADC" elsewhere in this same block, and this must not read the same.
            "last_skipped": bool(last_sweep.get("skipped")) if last_sweep else None,
        },
    }


@router.get("/config")
def config(request: Request = None):
    """Tells the SPA how to authenticate: GIS per-user (client id present) vs demo."""
    import os
    # Public Langfuse trace base, so the SPA can deep-link "📊 View trace" chips straight
    # to the relevant trace (deterministic ids: {scan}, {scan}-assess, {scan}-remediate).
    # Null when Langfuse isn't configured → the frontend simply omits the chips.
    lf_host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    import lf as _lf
    lf_project = _lf._project_id()
    import ai as _ai   # AI provenance (ADR 0019 Phase 0): active model + local/cloud zone
    import scanner
    from realtime_feature import gateway_enabled
    return {"google_client_id": core.GOOGLE_CLIENT_ID,
            "drive_scope": core.DRIVE_SCOPES[0],
            # Entra app for the SharePoint/OneDrive connect — runtime so the tenant can be set per
            # deployment without rebuilding the SPA (the frontend falls back to VITE_AZURE_* only
            # when these are absent). Null when SharePoint isn't configured; the SPA hides the button.
            "azure_client_id": core.AZURE_CLIENT_ID,
            "azure_tenant_id": core.AZURE_TENANT_ID,
            # Every SharePoint/OneDrive DATA-SOURCE connection this deployment can reach — one entry
            # per Entra app registration (ACP_AZURE_CLIENT_ID[_N]/ACP_AZURE_TENANT_ID[_N]). Distinct
            # from azure_client_id/azure_tenant_id above, which stays the single identity provider
            # that gates SIGN-IN to ACP itself; connecting a second tenant's SharePoint as a scan
            # source does not change who is allowed to use the app. [] when unconfigured — same
            # "hide the button" contract as the singular fields.
            "microsoft_tenants": core.MICROSOFT_TENANTS,
            # How many SharePoint sites one scan may span (ACP_SP_MAX_SITES, default 30). Served
            # rather than hardcoded in the SPA because the enforcement is the SERVER's — the scan
            # route refuses a larger selection and the walk caps itself — and a picker holding its
            # own copy of the number would disagree with the deployment the moment an operator
            # raised it: the UI would either block a selection the server would accept, or wave
            # through one it will refuse after the operator has finished choosing.
            "sharepoint_max_sites": scanner._sp_max_sites(),
            "auth": "gis" if core.GOOGLE_CLIENT_ID else "demo",
            # Runtime rather than Vite build-time state: one generic image can remain inert in
            # production while staging explicitly exposes the diagnostics-only shadow panel.
            "realtime_shadow_enabled": gateway_enabled(),
            **_build_info(),
            "ai": _ai.provenance(),
            "scope": _active_scope_info(),
            # Ownership signal for the scope editor. /config is fetched PRE-auth, so identity is
            # usually absent here (None) — the authoritative per-user value is on `me` (GET /me).
            # When the request DOES carry a verified identity (the access gate ran), report it;
            # otherwise None. When no owner is configured at all, everyone is an owner.
            "is_scope_owner": (core.is_scope_owner(_ident)
                               if (_ident := getattr(getattr(request, "state", None), "user_email", None))
                               or not core.OWNER_EMAIL
                               else None),
            # Strict owner flag (root of trust) — gates the owner-only "who is an admin" controls in
            # Settings, above the admin-level is_scope_owner. Same PRE-auth None caveat as above.
            "is_owner": (core.is_owner(_ident)
                         if (_ident := getattr(getattr(request, "state", None), "user_email", None))
                         or not core.OWNER_EMAIL
                         else None),
            "langfuse_trace_base": (f"{lf_host}/project/{lf_project}/traces" if lf_host else None)}


def _active_scope_info() -> dict:
    """The operator scope the SERVER is actually gating on, for the SPA to render.

    Until this existed the SPA hard-coded `ACTIVE_SCOPE_PRESET = 'engagement-14'` in
    activeScope.js, so changing the `scan_scope` setting moved the server's gate while every
    denominator, "N of 20 in scope" line and out-of-scope note in the UI kept describing the
    preset compiled into the bundle. Two sources of truth for one question, and the wrong one
    was the one the customer could see.

    Shipped on /config rather than /settings because /config is ALWAYS_PUBLIC and the SPA
    already fetches it at boot — the scope is not a secret (it is a list of WCAG criteria the
    customer agreed to) and gating it behind sign-in would leave the pre-auth shell describing
    a scope nobody had confirmed.

    Returns the NAME and the resolved criteria map, deliberately both. The name is what an
    operator recognises; the map is what the UI must arithmetic over, and deriving it here
    means the SPA never has to keep its own copy of a preset's contents in step with ours.
    `{"name": "", "criteria": null}` means no restriction — every criterion in scope.
    """
    try:
        from store import active_scope, scope_problem, SCOPE_SETTING
        raw = core.store.get_setting(SCOPE_SETTING, "") or ""
        scope = active_scope(core.store)
        problem = scope_problem(core.store)
        # A scope written as DATA has no preset name to show. Reporting the raw JSON here would
        # put a wall of text where the UI expects a label, so it is named for what it is and the
        # criteria map — which the SPA already renders — carries the detail.
        name = "" if not raw else ("custom" if raw.strip().startswith("{") else raw)
        # `error` appears ONLY when there is something to say. The no-restriction response stays
        # byte-identical to what #138 pinned, so every existing consumer is untouched, and the
        # key's mere presence is the signal — it is the difference between "no scope is set" and
        # "a scope IS set and the server is ignoring it", which otherwise both read criteria:null.
        if not scope:
            out = {"name": "", "criteria": None}
            if problem:
                out["error"] = problem
            return out
        return {"name": name, "criteria": {sc: sorted(f) for sc, f in scope.items()}}
    except Exception:
        # A scope we cannot read must not be reported as a scope that excludes everything.
        return {"name": "", "criteria": None}


class ScheduleUpdate(BaseModel):
    enabled: bool
    # Deprecated, retained so the API may roll out before an older SPA refreshes.
    interval_minutes: int | None = None
    timezone: str | None = None
    local_time: str | None = None
    days: list[int] | None = None
    source: str | None = None
    scope: dict | None = None
    notifications: str | None = None
    execution: dict | None = None


class ScheduleGuardrailsUpdate(BaseModel):
    allowed_sources: list[str]
    min_frequency_minutes: int = 60
    max_concurrent_per_owner: int = 1
    catch_up_ceiling: int = 1
    blackout_timezone: str = "UTC"
    blackout_start: str | None = None
    blackout_end: str | None = None


def _schedule_owner(request: Request) -> str:
    return (getattr(request.state, "user_email", None) or "demo").strip().lower()


def _schedule_response(request: Request) -> dict:
    """Return only the caller's schedule; configuration is owner-scoped at the query."""
    owner = _schedule_owner(request)
    cfg = core.store.get_user_scan_schedule(owner)
    legacy = core.store.get_schedule()
    legacy_owner = (legacy.get("owner_email") or "demo").strip().lower()
    # An existing process-wide interval remains visible only to its original owner until that
    # owner saves a wall-clock schedule. It is never exposed to another signed-in user.
    if cfg.get("updated_at") is None and legacy_owner == owner and legacy.get("enabled"):
        cfg = {**cfg, **legacy, "schedule_type": "interval"}
        job = core.scheduler.get_job("scheduled_local_scan")
        cfg["next_at"] = job.next_run_time.isoformat() if job and job.next_run_time else None
    else:
        cfg["schedule_type"] = "wall_clock"
        cfg["interval_minutes"] = None
        try:
            import scan_schedule
            next_at = scan_schedule.next_occurrence(cfg, datetime.now(timezone.utc))
            cfg["next_at"] = next_at.isoformat() if next_at else None
        except ValueError:
            cfg["next_at"] = None
    history = core.store.list_schedule_occurrences(owner, limit=20)
    metrics = cfg.get("metrics", {})
    total = int(metrics.get("scheduled", 0))
    cfg["source_config"] = {"kind": cfg.get("source", "drive"),
                            "scope": cfg.get("source_scope", {})}
    cfg["notifications"] = cfg.get("notification_policy", "failures")
    cfg["execution"] = cfg.get("queue_policy", {})
    cfg["guardrails"] = core.store.get_schedule_guardrails()
    cfg["history"] = history
    cfg["reliability"] = {
        "scheduled": total, "delayed": int(metrics.get("delayed", 0)),
        "skipped": int(metrics.get("skipped", 0)), "failed": int(metrics.get("failed", 0)),
        "on_time_rate": (None if total == 0 else round(max(0, total - int(metrics.get("delayed", 0))) / total, 4)),
    }
    cfg["options"] = {"notification_policies": ["off", "failures", "changes_and_failures", "all"],
                      "sources": cfg["guardrails"]["allowed_sources"]}
    return cfg


@router.get("/schedule")
def schedule(request: Request):
    cfg = _schedule_response(request)
    # list_scans() filters to completed_at IS NOT NULL, which an ADR 0020 Discover-only run
    # never sets (see list_finished_scans' own docstring) — a discover-only sweep landed here
    # and last_at kept showing the last scan that was ever ASSESSED, which can be arbitrarily
    # older than the estate's true last refresh. list_finished_scans() plus the same
    # COALESCE(completed_at, discovered_at) its own ordering uses is the fix: whichever
    # timestamp the newest row actually has.
    owner = _schedule_owner(request)
    # Local/demo mode deliberately remains one shared estate. An authenticated deployment must
    # not let Alice's schedule status advance because Bob completed a scan.
    scans = core.store.list_finished_scans(None if owner == "demo" else owner)
    cfg["last_at"] = (scans[0].get("completed_at") or scans[0].get("discovered_at")) if scans else None
    # The last sweep's OUTCOME, not just when a scan last completed. A failing sweep saves
    # nothing by design, so `last_at` keeps pointing at the last SUCCESSFUL scan and reads as
    # healthy while the estate quietly goes stale. None until a sweep has run.
    owner = _schedule_owner(request)
    cfg["last_sweep"] = core.store.get_last_sweep(owner)
    # Demo/no-auth deployments predate owner-scoped outcomes. Preserve that single-user history;
    # authenticated users never fall back to another user's process-wide result.
    if cfg["last_sweep"] is None and owner == "demo":
        cfg["last_sweep"] = core.store.get_last_sweep()
    return cfg


@router.put("/schedule")
def update_schedule(body: ScheduleUpdate, request: Request):
    # Attribute scheduled sweeps to whoever set the schedule, so the resulting scans
    # show up in their (owner-scoped) scan list.
    owner = _schedule_owner(request)
    if (body.timezone is None and body.local_time is None and body.days is None and
            body.source is None and body.scope is None and body.notifications is None and
            body.execution is None):
        if body.interval_minutes is None:
            raise HTTPException(422, "interval_minutes or a wall-clock schedule is required")
        if body.interval_minutes < 1:
            raise HTTPException(422, "interval_minutes must be at least 1")
        core.store.save_schedule(body.enabled, body.interval_minutes, owner=owner, source="drive")
    else:
        current = core.store.get_user_scan_schedule(owner)
        try:
            core.store.save_user_scan_schedule(
                owner, body.enabled,
                body.timezone if body.timezone is not None else current["timezone"],
                body.local_time if body.local_time is not None else current["local_time"],
                body.days if body.days is not None else current["days"],
                source=body.source or current.get("source") or "drive",
                source_scope=body.scope if body.scope is not None else current.get("source_scope"),
                notification_policy=body.notifications or current.get("notification_policy", "failures"),
                queue_policy=body.execution if body.execution is not None else current.get("queue_policy"))
            legacy = core.store.get_schedule()
            legacy_owner = (legacy.get("owner_email") or "demo").strip().lower()
            if legacy_owner == owner and legacy.get("enabled"):
                core.store.save_schedule(False, legacy["interval_minutes"], owner=owner,
                                         source=legacy.get("source") or "drive")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    core.reload_scheduler()
    return schedule(request)


@router.get("/schedule/history")
def schedule_history(request: Request, limit: int = Query(20, ge=1, le=200)):
    return {"items": core.store.list_schedule_occurrences(_schedule_owner(request), limit=limit)}


@router.get("/schedule/notifications")
def schedule_notifications(request: Request, limit: int = Query(50, ge=1, le=200)):
    return {"items": core.store.list_schedule_notifications(_schedule_owner(request), limit=limit)}


@router.post("/schedule/notifications/{notification_id}/read")
def read_schedule_notification(notification_id: str, request: Request):
    if not core.store.mark_schedule_notification_read(_schedule_owner(request), notification_id):
        raise HTTPException(404, "notification not found")
    return {"ok": True}


@router.get("/admin/schedule-guardrails")
def schedule_guardrails(request: Request):
    _require_admin(request)
    return core.store.get_schedule_guardrails()


@router.put("/admin/schedule-guardrails")
def update_schedule_guardrails(body: ScheduleGuardrailsUpdate, request: Request):
    _require_admin(request)
    try:
        return core.store.save_schedule_guardrails(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/hub", response_class=Response)
def hub():
    """Landing page — all key links in one place."""
    hub_file = core.ACP / "hub" / "index.html"
    if not hub_file.exists():
        raise HTTPException(404, "hub/index.html not found")
    return Response(hub_file.read_bytes(), media_type="text/html")


def _ai_base_url_error(val: str) -> str | None:
    """None when the value is acceptable. Empty is always allowed — it means "use the deploy
    default", which is how a burst-GPU detach gets back to the CPU endpoint."""
    if val and not val.startswith(("http://", "https://")):
        return "ai_base_url must be an http(s) URL (or empty to use the deploy default)"
    return None


# Per-field validators for the runtime AI endpoint settings, declared here rather than inlined in
# the write loop so update_settings can run EVERY one before it writes ANY field. Only ai_base_url
# has a rule today, and it is listed first, which is the only reason the previous
# validate-and-write-in-one-pass loop was safe: add a rule to a later field, or reorder the tuple,
# and a rejected PUT would have written the fields ahead of the bad one and then 422'd. The SPA
# sends the base URL and the vision model in a single Apply, so a partial write there is
# indistinguishable to the admin from "the setting will not save".
_AI_VALIDATORS = {"ai_base_url": _ai_base_url_error}


class SettingsUpdate(BaseModel):
    # A PUT naming a field this model does not have used to return 200 and change nothing —
    # the request echoed back as a success. That cost two debugging cycles on a production
    # vision-model override (2026-07-30/31): the setting was "saved" repeatedly and the worker
    # went on using the old value, with no error anywhere to explain it. A typo'd or renamed
    # field is now a 422 the caller can see, which matters most for the admin scope grid: a
    # scope that silently fails to save is indistinguishable from one that saved and is being
    # ignored, and only one of those is a bug the operator can act on.
    model_config = ConfigDict(extra="forbid")

    ai_enabled: bool | None = None
    # The operator scan scope. Accepts what the setting accepts — a preset NAME, or the scope as
    # DATA. `dict` is listed first so an admin UI can PUT the grid's own state without
    # stringifying it; "" clears the scope back to no restriction.
    scan_scope: dict[str, list[str]] | str | None = None
    drive_mirror_enabled: bool | None = None
    drive_mirror_folder: str | None = None
    auto_apply_validated: bool | None = None
    ai_base_url: str | None = None          # runtime AI endpoint override; "" clears → env default
    ai_vision_model: str | None = None
    ai_text_model: str | None = None


@router.get("/settings")
def get_settings(request: Request = None):
    """Platform settings. ai_enabled=false → deterministic-only mode platform-wide
    (overrides per-scan ?ai=true and blocks /ai/explain). drive_mirror_enabled=false
    (ADR 0010) → remediated fixes stay Blob-only, no automatic Drive copy."""
    user = (getattr(getattr(request, "state", None), "user_email", None) or "").strip()
    return {"ai_enabled": core.store.get_ai_enabled(),
            "drive_mirror_enabled": core.store.get_drive_mirror_enabled(),
            "drive_mirror_folder": core.store.get_drive_mirror_folder(),
            "auto_apply_validated": core.store.get_auto_apply_validated(),
            # Runtime AI endpoint override (GPU burst) — empty string = env default in use.
            "ai_base_url": core.store.get_setting("ai_base_url", "") or "",
            "ai_vision_model": core.store.get_setting("ai_vision_model", "") or "",
            "ai_text_model": core.store.get_setting("ai_text_model", "") or "",
            # The RAW setting, not the resolved map: this is the admin edit surface, and an
            # editor must show what is stored so a save round-trips. /config reports the
            # RESOLVED scope for everything that renders it — the two are different questions
            # and conflating them is how an editor starts overwriting what it never loaded.
            "scan_scope": core.store.get_setting("scan_scope", "") or "",
            "release_destination": _release_destination(user) if user else None,
            "release_templates": _release_templates(user) if user else []}


@router.put("/settings")
def update_settings(body: SettingsUpdate, request: Request):
    """Admin: set platform settings. Persisted across restarts. Audited."""
    _require_admin(request)
    if body.drive_mirror_enabled is not None:
        core.store.set_drive_mirror_enabled(body.drive_mirror_enabled)
        core.store.log_decision(
            "admin", "settings.drive_mirror_enabled",
            detail=f"drive_mirror_enabled set to {body.drive_mirror_enabled}")
    if body.drive_mirror_folder is not None:
        folder = body.drive_mirror_folder.strip() or "Remediated"
        core.store.set_drive_mirror_folder(folder)
        core.store.log_decision(
            "admin", "settings.drive_mirror_folder",
            detail=f"drive_mirror_folder set to {folder}")
    # SCOPE. Validated BEFORE writing, and rejected with a reason rather than stored: a scope
    # that cannot be parsed fails open at read time (assessment_policy.parse_scope_setting), so
    # storing a broken one would leave the operator looking at a saved value the engine silently
    # ignores. That is precisely the failure /config's `error` key exists to surface, and it is
    # better never to create it. `""` is always legal — it clears the scope.
    if body.scan_scope is not None:
        from store import parse_scope_setting
        raw = (json.dumps(body.scan_scope) if isinstance(body.scan_scope, dict)
               else str(body.scan_scope).strip())
        if raw and raw != "{}":
            problem = parse_scope_setting(raw)[1]
            if problem:
                raise HTTPException(422, f"scan_scope: {problem}")
        else:
            raw = ""            # {} and "" both mean no restriction; store the simpler one
        core.store.set_setting("scan_scope", raw)
        core.store.log_decision("admin", "settings.scan_scope",
                                detail=f"scan_scope set to {raw or '(no restriction)'}")

    ai_updates = [(key, val.strip())
                  for key, val in (("ai_base_url", body.ai_base_url),
                                   ("ai_vision_model", body.ai_vision_model),
                                   ("ai_text_model", body.ai_text_model))
                  if val is not None]
    # Validate EVERY field before writing ANY of them — see _AI_VALIDATORS. All-or-nothing is
    # order-independent; the old single-pass loop was correct only by the accident of which field
    # carried the only rule.
    for key, val in ai_updates:
        validator = _AI_VALIDATORS.get(key)
        problem = validator(val) if validator else None
        if problem:
            raise HTTPException(422, problem)
    for key, val in ai_updates:
        core.store.set_setting(key, val)
        core.store.log_decision("admin", f"settings.{key}",
                                detail=f"{key} set to {val or '(deploy default)'} — takes effect "
                                       "on every replica within ~30s, no restart")
    if ai_updates:
        # This replica switches immediately; the others follow via the TTL refresh. Once for the
        # batch, not once per field — the refresh re-reads all three settings anyway.
        try:
            import ai as _ai
            _ai._override_checked["at"] = 0.0
            _ai._maybe_refresh_endpoint()
        except Exception:
            swallowed("routes.system.update_settings: refreshing the AI endpoint after a settings "
                      "update failed")
    if body.auto_apply_validated is not None:
        core.store.set_auto_apply_validated(body.auto_apply_validated)
        core.store.log_decision(
            "admin", "settings.auto_apply_validated",
            detail=f"auto_apply_validated set to {body.auto_apply_validated} — "
                   "cross-checked ungrounded vision drafts "
                   f"{'auto-apply' if body.auto_apply_validated else 'queue for one-click approval'}")
    if body.ai_enabled is not None:
        core.store.set_ai_enabled(body.ai_enabled)
        core.store.log_decision(
            "admin", "settings.ai_enabled",
            detail=f"ai_enabled set to {body.ai_enabled}")
    return get_settings(request)


# ── per-user scan-scope override (ADR 0035 stage 2 — the non-admin surface) ────────────
# The owner default is admin-gated above; this lets a SIGNED-IN USER set their OWN scan-scope
# override, keyed to their email, never able to write anyone else's. The override can only WIDEN the
# owner mandate — the widen-only union in active_scope keeps every owner criterion/format regardless
# of what is stored here — so this surface can never be used to skip a check the owner required.
class MyScopeUpdate(BaseModel):
    # Same shape the admin scan_scope accepts — a preset NAME or the scope as DATA. "" is a REAL
    # value here: the user opting into NO restriction (assess everything), which is distinct from
    # HAVING no override (to clear the override and fall back to the owner default, use DELETE).
    scan_scope: dict[str, list[str]] | str | None = None
    release_timezone: str | None = None
    release_destination: dict | None = None
    release_templates: list[dict] | None = None


def _release_destination(user: str) -> dict | None:
    raw = core.store.get_user_setting(user, "release_destination")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _validate_release_destination(value: dict) -> dict:
    provider = str(value.get("provider") or "").strip().lower()
    if provider not in {"drive", "sharepoint"}:
        raise HTTPException(422, "release_destination.provider must be drive or sharepoint")
    folder_id = str(value.get("folder_id") or "").strip()
    folder_name = str(value.get("folder_name") or "").strip()
    if not folder_id or len(folder_id) > 500:
        raise HTTPException(422, "release_destination.folder_id is required")
    if not folder_name or len(folder_name) > 255:
        raise HTTPException(422, "release_destination.folder_name is required")
    # Persist only stable provider identifiers and the user-facing label. Tokens, URLs and
    # arbitrary caller fields must never become durable preferences.
    return {"provider": provider, "folder_id": folder_id, "folder_name": folder_name}


def _release_templates(user: str) -> list[dict]:
    raw = core.store.get_user_setting(user, "release_templates")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _validate_release_templates(values: list[dict]) -> list[dict]:
    if len(values) > 20:
        raise HTTPException(422, "at most 20 Release templates can be saved")
    cleaned, seen = [], set()
    for raw in values:
        if not isinstance(raw, dict):
            raise HTTPException(422, "each Release template must be an object")
        name = str(raw.get("name") or "").strip()
        if not name or len(name) > 80:
            raise HTTPException(422, "each Release template needs a name of 80 characters or fewer")
        if name.casefold() in seen:
            raise HTTPException(422, "Release template names must be unique")
        seen.add(name.casefold())
        method = str(raw.get("method") or "publish").strip().lower()
        if method not in {"publish", "download", "acp"}:
            raise HTTPException(422, "Release template method must be publish, download, or acp")
        destination = raw.get("destination")
        if destination is not None:
            destination = _validate_release_destination(destination)
        download_format = str(raw.get("download_format") or "zip").strip().lower()
        if download_format not in {"zip", "original"}:
            raise HTTPException(422, "download_format must be zip or original")
        package_name = str(raw.get("package_name") or "").strip()
        release_folder_name = str(raw.get("release_folder_name") or "").strip()
        if len(package_name) > 100 or len(release_folder_name) > 100:
            raise HTTPException(422, "template file and folder names must be 100 characters or fewer")
        cleaned.append({
            "name": name, "method": method, "destination": destination,
            "preserve_hierarchy": raw.get("preserve_hierarchy") is not False,
            "include_manifest": raw.get("include_manifest") is not False,
            "include_verification_report": bool(raw.get("include_verification_report")),
            "download_format": download_format, "package_name": package_name,
            "release_folder_name": release_folder_name,
        })
    return cleaned


def _require_user(request: Request) -> str:
    """The signed-in user's email, or 401. Per-user settings are keyed to identity, so — unlike the
    admin gate, which no-ops in local dev — this REQUIRES a stamped user: there is no per-user
    override without a user, and falling back to the shared 'demo' owner would let one user's
    override leak onto everyone on a shared-estate deployment."""
    email = (getattr(request.state, "user_email", None) or "").strip()
    if not email:
        raise HTTPException(401, "sign-in required for per-user settings")
    return email


@router.get("/settings/mine")
def get_my_settings(request: Request):
    """This signed-in user's OWN scan-scope override, plus the owner default it widens onto — both as
    the RAW stored values (the edit surface, mirroring GET /settings), so an editor round-trips.
    `scan_scope` is "" when the user has no override; the RESOLVED effective map is /config's job."""
    user = _require_user(request)
    return {
        "scan_scope": core.store.get_user_setting(user, "scan_scope") or "",
        "owner_default": core.store.get_setting("scan_scope", "") or "",
        "release_timezone": core.store.get_user_setting(user, "release_timezone") or "America/Chicago",
        "release_destination": _release_destination(user),
        "release_templates": _release_templates(user),
    }


@router.put("/settings/mine")
def update_my_settings(body: MyScopeUpdate, request: Request):
    """Set THIS user's own scan-scope override. Validated BEFORE writing — a malformed scope is a 422
    and is never stored (same discipline as the admin PUT), because a stored-but-unparseable override
    is silently ignored at read time. `{}` and "" both store as "" (no restriction)."""
    user = _require_user(request)
    if "release_templates" in body.model_fields_set:
        templates = _validate_release_templates(body.release_templates or [])
        core.store.set_user_setting(user, "release_templates", json.dumps(templates))
        core.store.log_decision(user, "settings.mine.release_templates",
                                detail=f"{len(templates)} Release templates saved")
        if (body.scan_scope is None and body.release_timezone is None and
                "release_destination" not in body.model_fields_set):
            return {"scan_scope": core.store.get_user_setting(user, "scan_scope") or "",
                    "release_timezone": core.store.get_user_setting(user, "release_timezone") or "America/Chicago",
                    "release_destination": _release_destination(user),
                    "release_templates": templates}
    if "release_destination" in body.model_fields_set:
        if body.release_destination is None:
            core.store.clear_user_setting(user, "release_destination")
            destination = None
        else:
            destination = _validate_release_destination(body.release_destination)
            core.store.set_user_setting(user, "release_destination", json.dumps(destination))
        core.store.log_decision(user, "settings.mine.release_destination",
                                detail="release destination cleared" if destination is None else
                                f"release destination set to {destination['provider']}:{destination['folder_id']}")
        if body.scan_scope is None and body.release_timezone is None:
            return {"scan_scope": core.store.get_user_setting(user, "scan_scope") or "",
                    "release_timezone": core.store.get_user_setting(user, "release_timezone") or "America/Chicago",
                    "release_destination": destination}
    if body.release_timezone is not None:
        import publish as _publish
        zone = body.release_timezone.strip() or "America/Chicago"
        if zone not in _publish.RELEASE_TIMEZONES:
            raise HTTPException(422, "release_timezone must be UTC, a US timezone, or Asia/Kolkata")
        core.store.set_user_setting(user, "release_timezone", zone)
        core.store.log_decision(user, "settings.mine.release_timezone",
                                detail=f"release folder timezone set to {zone}")
        if body.scan_scope is None:
            return {"scan_scope": core.store.get_user_setting(user, "scan_scope") or "",
                    "release_timezone": zone}
    if body.scan_scope is None:
        return {"scan_scope": core.store.get_user_setting(user, "scan_scope") or ""}
    from store import parse_scope_setting
    raw = (json.dumps(body.scan_scope) if isinstance(body.scan_scope, dict)
           else str(body.scan_scope).strip())
    if raw and raw != "{}":
        problem = parse_scope_setting(raw)[1]
        if problem:
            raise HTTPException(422, f"scan_scope: {problem}")
    else:
        raw = ""            # {} and "" both mean 'no restriction' — store the simpler one
    core.store.set_user_setting(user, "scan_scope", raw)
    core.store.log_decision(user, "settings.mine.scan_scope",
                            detail=f"per-user scan_scope set to {raw or '(no restriction)'}")
    return {"scan_scope": raw}


@router.delete("/settings/mine")
def clear_my_settings(request: Request):
    """Remove THIS user's override so their scans fall back to the owner default. Distinct from PUT
    with "" (a real override meaning 'no restriction'): DELETE means 'I have no preference, use the
    owner's'. Idempotent."""
    user = _require_user(request)
    core.store.clear_user_setting(user, "scan_scope")
    core.store.log_decision(user, "settings.mine.scan_scope", detail="per-user scan_scope cleared")
    return {"scan_scope": ""}


# ── AI provider gateway config (ADR 0019 §6, secret-ref design) ────────────────
class AIProviderUpdate(BaseModel):
    # extra='forbid' is a load-bearing security guard: the model has NO key/api_key field, so a
    # client that tries to submit a key value (rather than a secret reference NAME) is rejected
    # with 422 instead of the value being silently accepted. The key never transits this endpoint.
    model_config = ConfigDict(extra="forbid")
    provider: str
    enabled: bool | None = None
    endpoint: str | None = None
    deployment: str | None = None
    model: str | None = None
    key_secret_ref: str | None = None       # the NAME of an ops-provisioned env/Key-Vault secret


class SecondOpinionPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    criteria: list[str]
    confidence_threshold: str = "low"
    max_requests_per_scan: int = 25
    max_requests_per_day: int = 250
    max_daily_cost_usd: float = 10.0
    estimated_cost_per_request_usd: float = 0.01


class RemediationPilotUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    categories: list[str]
    max_calls: int = 100
    max_spend_usd: float = 5.0
    min_sample: int = 10
    max_failure_rate: float = 0.10
    min_acceptance_rate: float = 0.90
    max_edit_rate: float = 0.20
    min_validation_clear_rate: float = 0.95


@router.get("/ai/second-opinion-policy")
def get_second_opinion_policy(request: Request):
    """Owner-visible consent policy; credentials and provider output are never part of it."""
    _require_admin(request)
    from second_opinion_policy import load_policy
    return load_policy(core.store)


@router.put("/ai/second-opinion-policy")
def put_second_opinion_policy(body: SecondOpinionPolicyUpdate, request: Request):
    """Set policy for future scans. Existing scans retain their immutable snapshot."""
    _require_admin(request)
    from second_opinion_policy import SETTING_KEY, normalize_policy
    policy = normalize_policy(body.model_dump())
    if body.confidence_threshold.lower() not in ("low", "medium", "high"):
        raise HTTPException(422, "confidence_threshold must be low, medium, or high")
    if policy["enabled"] and not policy["criteria"]:
        raise HTTPException(422, "at least one eligible criterion is required when enabled")
    core.store.set_setting(SETTING_KEY, json.dumps(policy, sort_keys=True))
    actor = getattr(request.state, "user_email", None) or "admin"
    core.store.log_decision(actor, "settings.second_opinion_policy",
                            detail=(f"enabled={policy['enabled']} · criteria="
                                    f"{','.join(policy['criteria']) or '(none)'} · threshold="
                                    f"{policy['confidence_threshold']} · future scans only"))
    return policy


@router.get("/ai/remediation-pilot")
def get_remediation_pilot(request: Request):
    """Live, owner-visible pilot state including the server's automatic stop decision."""
    _require_admin(request)
    from remediation_pilot import pilot_status
    return pilot_status(core.store)


@router.put("/ai/remediation-pilot")
def put_remediation_pilot(body: RemediationPilotUpdate, request: Request):
    """Arm or stop only the evidence-approved assisted lanes; arbitrary lanes are rejected."""
    _require_admin(request)
    from remediation_pilot import (ALLOWED_CATEGORIES, SETTING_KEY, normalize_policy,
                                   pilot_status)
    unknown = sorted(set(body.categories) - set(ALLOWED_CATEGORIES))
    if unknown:
        raise HTTPException(422, f"categories are not approved for this pilot: {', '.join(unknown)}")
    if body.enabled and not body.categories:
        raise HTTPException(422, "at least one approved category is required when enabled")
    policy = normalize_policy(body.model_dump())
    core.store.set_setting(SETTING_KEY, json.dumps(policy, sort_keys=True))
    actor = getattr(request.state, "user_email", None) or "admin"
    core.store.log_decision(actor, "settings.remediation_model_pilot",
                            detail=(f"enabled={policy['enabled']} · categories="
                                    f"{','.join(policy['categories']) or '(none)'} · "
                                    "server stop gates active"))
    return pilot_status(core.store)


@router.get("/ai/providers")
def get_ai_providers(request: Request):
    """Admin: the configurable cloud AI providers as SAFE views — endpoint, model, whether the
    referenced secret is present, and who owns it — never a key value (ADR 0019 §6). Admin-gated:
    provider governance config isn't exposed to non-owners.

    `secret_write` tells the page whether THIS deployment can accept a pasted key at all (a Key
    Vault is configured and its SDK is installed). The field exists so the UI never renders an
    input that cannot work: without it the only way to discover an unconfigured vault is to type
    a live credential into a box and have it rejected."""
    _require_admin(request)
    import providers as _providers
    import secret_store as _secrets
    store = _secrets.active_secret_store()
    ok, reason = store.writable()
    return {"providers": _providers.list_provider_views(),
            "secret_write": {"available": ok, "kind": store.kind, "reason": reason}}


@router.put("/ai/providers")
def put_ai_provider(body: AIProviderUpdate, request: Request):
    """Admin: set ONE provider's non-secret config. The key itself is never submitted here — the
    admin's ops team provisions it as a container/Key-Vault secret and this stores only the secret's
    NAME (key_secret_ref). A value that looks like a key (not an env-var name) is rejected."""
    _require_admin(request)
    import providers as _providers
    provider = (body.provider or "").strip().lower()
    if provider not in _providers.CLOUD_PROVIDERS:
        raise HTTPException(422, f"unknown provider '{provider}' — one of {list(_providers.CLOUD_PROVIDERS)}")
    existing = core.store.get_ai_provider_config(provider) or {}
    endpoint = existing.get("endpoint")
    if body.endpoint is not None:
        endpoint = body.endpoint.strip() or None
        if endpoint and not endpoint.startswith(("http://", "https://")):
            raise HTTPException(422, "endpoint must be an http(s) URL")
    ref = existing.get("key_secret_ref")
    if body.key_secret_ref is not None:
        ref = body.key_secret_ref.strip() or None
        # Reject a pasted key: a secret reference is an env-var NAME, not the credential. This is
        # the guard that keeps a key out of the database even if an admin misunderstands the field.
        if ref and not _SECRET_REF_RE.match(ref):
            raise HTTPException(422, "key_secret_ref must be an environment-variable NAME "
                                     "(e.g. AZURE_OPENAI_API_KEY), not a key value")
    enabled = body.enabled if body.enabled is not None else bool(existing.get("enabled"))
    if enabled:
        # ENABLE ONLY WHAT CAN ACTUALLY RUN. Until this guard, enabling was unconditional: the row
        # stored enabled=true whatever else was blank, `_adapter_for` then returned None at call
        # time, and every document silently stayed on the local path. The Settings page said the
        # provider was on and nothing was ever sent to it. An enable switch that does nothing is
        # worse than one that refuses, because it reads as consent having been honoured.
        #
        # The would-be config is validated, not the stored one — an admin fills the fields and
        # ticks the box in a single save, so checking `existing` would refuse the first correct
        # save and accept nothing after it.
        candidate = {**existing, "provider": provider, "endpoint": endpoint,
                     "deployment": (body.deployment.strip() if body.deployment is not None
                                    else existing.get("deployment")) or None,
                     "model": (body.model.strip() if body.model is not None
                               else existing.get("model")) or None,
                     "key_secret_ref": ref}
        readiness = _providers.activation_readiness(provider, candidate)
        if not readiness["ready"]:
            # 422 with the reason, not a bare refusal: "missing model, key_secret_ref" is fixable
            # by the admin reading it, and "the environment secret named X is not present" is a
            # different person's job (ops provisions the value; this app never stores it).
            raise HTTPException(422, {"error": "cannot enable this provider yet",
                                      "provider": provider,
                                      "detail": readiness["detail"],
                                      "missing": readiness["missing"],
                                      "secret_resolves": readiness["secret_resolves"]})
    who = getattr(request.state, "user_email", None) or "admin"
    core.store.upsert_ai_provider_config(
        provider,
        enabled=enabled,
        endpoint=endpoint,
        deployment=(body.deployment.strip() if body.deployment is not None else existing.get("deployment")) or None,
        model=(body.model.strip() if body.model is not None else existing.get("model")) or None,
        key_secret_ref=ref,
        updated_by=who,
    )
    # Audit records the config change WITHOUT the key value (only the reference name).
    core.store.log_decision("admin", f"settings.ai_provider.{provider}",
                            detail=f"provider={provider} enabled={enabled} endpoint={endpoint or '—'} "
                                   f"key_secret_ref={ref or '—'} (key value never handled here)")
    return {"providers": _providers.list_provider_views()}


class AIProviderSecretWrite(BaseModel):
    value: str


@router.post("/ai/providers/{provider}/secret")
def put_ai_provider_secret(provider: str, body: AIProviderSecretWrite, request: Request):
    """Admin: set one provider's API key by writing it to the deployment's Key Vault.

    The one place in this product where a key VALUE is accepted, and it is accepted only to hand
    straight to the vault: nothing is written to the database except the resulting reference name
    (`keyvault:acp-ai-<provider>-key`), nothing is logged but that name, and no read path can
    return the value — `provider_view` has never carried it and still does not.

    A deployment with no vault configured REFUSES (422) rather than falling back to storing the
    value anywhere else. That refusal is the feature: it is what keeps "we could not do this
    safely" from turning into "we did it unsafely".

    The vault secret's name is derived from the provider, never supplied by the caller — a name
    over HTTP would let one admin overwrite another provider's secret, or something else in a
    shared vault, through a field that looks like a label.
    """
    _require_admin(request)
    import providers as _providers
    import secret_store as _secrets
    provider = (provider or "").strip().lower()
    if provider not in _providers.CLOUD_PROVIDERS:
        raise HTTPException(422, f"unknown provider '{provider}' — one of {list(_providers.CLOUD_PROVIDERS)}")
    try:
        ref = _secrets.write_provider_secret(provider, body.value)
    except ValueError:
        raise HTTPException(422, "the key value is empty")
    except RuntimeError as e:
        # "no vault is configured", or "the SDK is not installed in this image" — an operator's
        # fix, and one an admin cannot make from this page, so it is reported verbatim.
        raise HTTPException(422, {"error": "this deployment cannot store a key value", "detail": str(e)})
    except Exception as e:
        # A vault that refused the write (identity lacks secrets/set, network, throttling). The
        # TYPE and message are surfaced because "it did not work" sends someone to read code.
        raise HTTPException(502, {"error": "the key vault rejected the write",
                                  "detail": f"{type(e).__name__}: {e}"})

    existing = core.store.get_ai_provider_config(provider) or {}
    core.store.upsert_ai_provider_config(
        provider,
        enabled=bool(existing.get("enabled")),
        endpoint=existing.get("endpoint"),
        deployment=existing.get("deployment"),
        model=existing.get("model"),
        key_secret_ref=ref,
        updated_by=getattr(request.state, "user_email", None) or "admin",
    )
    # The audit row names the reference, never the value — same rule as the config route above.
    core.store.log_decision("admin", f"settings.ai_provider.{provider}.secret",
                            detail=f"provider={provider} key_secret_ref={ref} "
                                   f"(written to the key vault; value never stored or logged)")
    return {"providers": _providers.list_provider_views()}


@router.get("/decisions")
def decisions(scan_id: str | None = None, limit: int = 500):
    """Immutable decision audit log — every consequential action (scan mode, HITL
    review, settings change, auto-routing). Append-only; filter by scan_id."""
    return core.store.list_decisions(scan_id=scan_id, limit=limit)


@router.get("/jobs")
def jobs(request: Request, status: str | None = None, limit: int = 100):
    """Async job-queue visibility (ADR 0004): queue depth by status + recent jobs.
    Owner-scoped — a user sees only their OWN jobs (stats, list, dead-letters), so
    filenames in job payloads / error text never leak across tenants. The worker
    count is global (shared infra, not sensitive)."""
    owner = getattr(request.state, "user_email", None) or "demo"
    # worker_tier_status() is a strict superset of worker_tier_alive() — same freshness check,
    # plus the beat's own timestamp and age. Discover's processing panel already distinguishes
    # connection freshness (its SSE stream) from progress freshness (inventory last changing);
    # this is the third of the PRD's three timestamps — whether the ASSIGNED WORKER is still
    # alive — which nothing here exposed before. "alive" below is unchanged in shape (still a
    # bare bool at the same key), so this is additive, not a breaking change to the response.
    _wt = core.store.worker_tier_status()
    return {"workers": core.WORKERS,
            # Standalone worker container's heartbeat (#113) — in the split topology the
            # API's own pool is 0, so Monitor must show the tier that actually runs jobs.
            "worker_tier_alive": _wt["alive"],
            "worker_heartbeat_at": _wt["heartbeat_at"],
            "worker_heartbeat_age_s": _wt["age_s"],
            # The worker container's own core.WORKERS (its real concurrency, carried in the
            # heartbeat's JSON envelope) — None for an old bare-ISO beat or one that never
            # carried it. This is real "ACP-ready worker slots" capacity, unlike `workers`
            # above, which is this API container's OWN pool (0 in the split topology). Busy
            # vs. available within that slot count is NOT tracked here — it needs
            # instrumentation inside worker.py's pool itself — but a caller can already
            # approximate "busy" for free from `stats.running` below once this is non-None.
            "worker_tier_pool_size": _wt.get("pool_size"),
            # Conservative starting recommendation. Local AI workloads are memory- and
            # GPU-constrained, not CPU-constrained; 4 is a safe floor the user can raise.
            "suggested_workers": 4,
            "runtime_mode": core._RUNTIME_MODE,
            # Global like `workers`/`worker_tier_alive` above, not owner-scoped: the question
            # this answers ("is the shared worker tier actually draining its queue") is about the
            # tier, not this caller's own jobs — scoping it to owner would go dark the moment
            # THIS user has nothing queued, even while every other tenant's queue is stalled.
            # Only id/type/created_at are exposed — no payload, so no filenames cross tenants.
            "oldest_queued": core.store.oldest_queued_job(),
            "stats": core.store.job_stats(owner=owner),
            "dead_letters": core.store.dead_letter_breakdown(owner=owner),
            "jobs": core.store.list_jobs(status=status, limit=limit, owner=owner)}


#: Process states that hold work without accepting more. A draining worker keeps heartbeating for
#: the whole bounded shutdown — ACP_SHUTDOWN_DRAIN_SECONDS is 540 on every worker lane
#: (deploy/public/redeploy.sh) — precisely so its running jobs keep their process attribution, and
#: worker_telemetry.WorkerInstanceReporter.draining() says so in as many words. Excluding these
#: states from capacity discarded that attribution one layer further up.
_OCCUPIED_PROCESS_STATES = frozenset({"draining", "unhealthy"})


def _replica_capacity(instances: list[dict], *, now: datetime,
                      freshness_seconds: int | None = None) -> dict[str, dict]:
    """Aggregate process heartbeats into physical replicas, then into service roles.

    ``worker_id`` identifies a process (role:replica:process), so counting rows as replicas
    inflates the fleet whenever a container runs more than one worker process. Capacity remains
    the sum of fresh process pools, while replica health/counts and drawer rows use the stable
    ``replica_id``. Rows without a replica id retain the legacy one-row-per-worker behavior.
    """
    freshness_seconds = (freshness_seconds if freshness_seconds is not None
                         else core.WORKER_INSTANCE_FRESHNESS_SECONDS)
    replicas: dict[tuple[str, str], dict] = {}
    for instance in instances:
        worker_id = str(instance.get("worker_id") or "")
        role = worker_id.split(":", 1)[0] if ":" in worker_id else "mixed"
        replica_id = str(instance.get("replica_id") or worker_id or "unknown")
        key = (role, replica_id)
        try:
            beat = datetime.fromisoformat(str(instance.get("last_heartbeat_at")).replace("Z", "+00:00"))
            age_s = max(0, (now - beat).total_seconds())
        except (TypeError, ValueError):
            beat, age_s = None, float("inf")
        fresh = age_s <= freshness_seconds
        state = instance.get("state")
        healthy_process = fresh and state in {"ready", "busy"}
        # A fresh process that is DRAINING or UNHEALTHY still holds the jobs it already claimed.
        # It will not claim more, so none of its free slots are capacity — but its occupied slots
        # are genuinely busy, and dropping them is what made a nine-minute drain read as
        # "0 of 20 slots (0%)" with documents in flight. See _OCCUPIED_PROCESS_STATES.
        occupied_process = fresh and state in _OCCUPIED_PROCESS_STATES
        replica = replicas.setdefault(key, {
            **instance, "worker_id": worker_id, "replica_id": replica_id,
            "process_count": 0, "concurrency_limit": 0, "active_job_count": 0,
            "fresh": False, "healthy": False, "occupied": False, "age_s": None,
        })
        replica["process_count"] += 1
        replica["fresh"] = replica["fresh"] or fresh
        replica["healthy"] = replica["healthy"] or healthy_process
        replica["occupied"] = replica["occupied"] or occupied_process
        if healthy_process:
            replica["concurrency_limit"] += max(0, int(instance.get("concurrency_limit") or 0))
            replica["active_job_count"] += max(0, int(instance.get("active_job_count") or 0))
        elif occupied_process:
            # Slots and busy move together: this process is fully utilised by construction and
            # offers no availability. Counting its concurrency_limit instead would invent free
            # capacity on a container that is on its way out.
            held = max(0, int(instance.get("active_job_count") or 0))
            replica["concurrency_limit"] += held
            replica["active_job_count"] += held
        if age_s != float("inf") and (replica["age_s"] is None or age_s < replica["age_s"]):
            replica["age_s"] = round(age_s, 1)
            replica["last_heartbeat_at"] = beat.isoformat() if beat else None
            replica["revision_name"] = instance.get("revision_name")
            replica["software_version"] = instance.get("software_version")
        if healthy_process and instance.get("state") == "busy":
            replica["state"] = "busy"
        elif healthy_process and replica.get("state") != "busy":
            replica["state"] = "ready"

    per_role: dict[str, dict] = {}
    for (role, _replica_id), replica in replicas.items():
        row = per_role.setdefault(role, {"role": role, "capacity_source": "worker_instances",
            "freshness_threshold_seconds": freshness_seconds, "healthy_replicas": 0,
            "stale_replicas": 0, "occupied_replicas": 0, "worker_slots": 0, "busy_slots": 0,
            "instances": []})
        # Healthy AND occupied replicas both contribute; only healthy ones offer availability,
        # which the per-process branch above has already encoded in the numbers.
        if replica["healthy"] or replica["occupied"]:
            row["worker_slots"] += replica["concurrency_limit"]
            row["busy_slots"] += replica["active_job_count"]
        if replica["healthy"]:
            row["healthy_replicas"] += 1
        elif replica["occupied"]:
            # Counted separately from both: it is not healthy (it takes no new work) and not
            # stale (it is reporting). Without this row an operator sees the slot total move
            # during a rollout with nothing on the screen explaining why.
            row["occupied_replicas"] += 1
        elif not replica["fresh"]:
            row["stale_replicas"] += 1
            replica["state"] = "stale"
        row["instances"].append(replica)
        measured = replica.get("last_heartbeat_at")
        if measured and (not row.get("measured_at") or measured > row["measured_at"]):
            row["measured_at"] = measured
    for row in per_role.values():
        row["instances"].sort(key=lambda item: str(item.get("replica_id") or ""))
    return per_role


def _running_workflow_count(workflows: list[dict]) -> int:
    """Workflows with queue work that can truthfully appear under Running jobs."""
    return sum(1 for row in workflows
               if row.get("status") in ("running", "waiting", "stopping"))


def _admin_activity_snapshot() -> dict:
    wt = core.store.worker_tier_status()
    worker_roles = core.store.worker_roles_status()
    stats = core.store.job_stats(owner=None)
    runs = core.store.admin_live_activity()
    _unlinked = getattr(core.store, "unlinked_active_jobs_count", None)
    try:
        unlinked_active_jobs = int(_unlinked()) if callable(_unlinked) else None
    except Exception:
        unlinked_active_jobs = None
    _list_instances = getattr(core.store, "list_worker_instances", None)
    instances = _list_instances() if callable(_list_instances) else []
    # Which replica is running what, from `locked_by` — ACP's own data, so it is as fresh as the
    # stream itself rather than waiting on the 30s Azure capacity cache. getattr-guarded like
    # every other optional store method here, so a FakeStore or an older store reads as
    # unavailable rather than raising.
    _by_replica = getattr(core.store, "running_jobs_by_replica", None)
    try:
        job_attribution = _by_replica() if callable(_by_replica) else {
            "available": False, "replicas": [], "attributed": None, "unattributed": None,
            "reason": "This deployment's job store does not report per-replica attribution."}
    except Exception:
        swallowed("routes.system._admin_activity_snapshot: reading per-replica job attribution failed")
        job_attribution = {"available": False, "replicas": [], "attributed": None,
                           "unattributed": None,
                           "reason": "Per-replica job attribution could not be read."}
    freshness_seconds = core.WORKER_INSTANCE_FRESHNESS_SECONDS
    now = datetime.now(timezone.utc)
    per_role = _replica_capacity(instances, now=now, freshness_seconds=freshness_seconds)

    # The shared heartbeat is last-writer-wins. In production each dedicated service writes its
    # own role heartbeat, so summing the live role pools is the only honest total capacity.
    # Fall back to the legacy shared heartbeat for older/single-pool deployments.
    live_role_slots = sum(int(row.get("pool_size") or 0) for row in worker_roles.values()
                          if row.get("alive"))
    slots = live_role_slots or (wt.get("pool_size") if wt.get("pool_size") is not None
                                else core.WORKERS)
    running = sum(int(r.get("running") or 0) for r in runs)
    queued = sum(int(r.get("queued") or 0) for r in runs)
    _running_by_type = getattr(core.store, "running_jobs_by_type", None)
    running_by_type = _running_by_type() if callable(_running_by_type) else None
    _list_events = getattr(core.store, "list_orchestration_events", None)
    lifecycle_events = _list_events(limit=200) if callable(_list_events) else []
    _list_stage_events = getattr(core.store, "list_workflow_stage_events", None)
    stage_events = _list_stage_events() if callable(_list_stage_events) else lifecycle_events
    recovery = _recovery_summary(stage_events)
    for role, row in per_role.items():
        stage = "discover" if role == "discovery" else role
        if running_by_type is None:
            jobs = sum(int(r.get("running") or 0) for r in runs if (r.get("stage") or "unknown") == stage)
        elif role == "discovery":
            jobs = sum(running_by_type.get(kind, 0) for kind in core.DISCOVERY_LANE_JOB_TYPES)
        elif role == "assess":
            jobs = sum(running_by_type.get(kind, 0) for kind in core.ASSESS_LANE_JOB_TYPES)
        elif role == "remediate":
            jobs = sum(running_by_type.get(kind, 0) for kind in core.remediation_job_types())
        elif role == "release":
            jobs = sum(running_by_type.get(kind, 0) for kind in core.RELEASE_LANE_JOB_TYPES)
        else:
            jobs = sum(running_by_type.values())
        row["jobs_in_flight"] = jobs
        if role == "release":
            try:
                row["queue"] = core.store.worker_lane_queue(core.RELEASE_LANE_JOB_TYPES)
                row["queue"]["available"] = True
            except Exception:
                row["queue"] = {"available": False, "claimable": None, "oldest_created_at": None}
        reported_busy = row["busy_slots"]
        row["reported_busy_slots"] = reported_busy
        row["busy_slots"] = min(row["worker_slots"], reported_busy)
        row["available_slots"] = max(0, row["worker_slots"] - row["busy_slots"])
        row["unattributed_running"] = max(0, jobs - row["busy_slots"])
        row["utilization_pct"] = min(100, round(row["busy_slots"] / row["worker_slots"] * 100)) if row["worker_slots"] else None
        row["status"] = ("stale" if not row["healthy_replicas"] and row["stale_replicas"] else
                         "saturated" if row["worker_slots"] and row["busy_slots"] >= row["worker_slots"] else
                         "degraded" if row["stale_replicas"] or row["unattributed_running"] else "online")
        alerts = []
        if row["stale_replicas"]:
            alerts.append({"code": "stale_replicas", "severity": "warning",
                           "message": f"{row['stale_replicas']} registered replica(s) have stale heartbeats."})
        if row["unattributed_running"]:
            alerts.append({"code": "unattributed_running", "severity": "warning",
                           "message": f"{row['unattributed_running']} running job record(s) are not attributed to live worker slots."})
        if reported_busy > row["worker_slots"]:
            alerts.append({"code": "active_exceeds_concurrency", "severity": "critical",
                           "message": "Reported active slots exceed configured concurrency; utilization remains capped."})
        revisions = sorted({str(item.get("revision_name")) for item in row["instances"]
                            if item.get("fresh") and item.get("revision_name")})
        row["revision_distribution"] = {
            revision: sum(1 for item in row["instances"]
                          if item.get("fresh") and str(item.get("revision_name")) == revision)
            for revision in revisions}
        # Only revisions that ACCEPT work count as mixed. `revisions` above is every fresh
        # revision, and a draining old revision is fresh by design for its whole bounded shutdown
        # (up to nine minutes) — so this warned on every rollout, for the entire drain, about the
        # one state a rollout is supposed to pass through. Two revisions both claiming jobs is the
        # genuinely mixed case: a promotion that did not retire the old one, or a stuck scale-down.
        # `revision_distribution` deliberately keeps the draining revision so the drawer can still
        # show it; this is about what is worth an alert, not what is worth displaying.
        serving = {str(item.get("revision_name")) for item in row["instances"]
                   if item.get("healthy") and item.get("revision_name")}
        if len(serving) > 1:
            alerts.append({"code": "mixed_revisions", "severity": "warning",
                           "message": f"{len(serving)} revisions are both accepting work."})
        queued_for_role = sum(int(run.get("queued") or 0) for run in runs
                              if (run.get("stage") or "unknown") == stage)
        # `healthy_replicas`, not `worker_slots`. The two were interchangeable until draining
        # replicas began contributing their held slots: a lane whose only replica is draining now
        # reports non-zero worker_slots while being unable to accept a single queued job, which
        # would have silently retired this alert during exactly the rollout window it is for.
        if queued_for_role and not row["healthy_replicas"]:
            alerts.append({"code": "no_capacity_with_queue", "severity": "critical",
                           "message": f"{queued_for_role} job(s) are queued with no fresh reported capacity."})
        row["alerts"] = alerts
        row["recent_lifecycle_events"] = [event for event in lifecycle_events
            if str(event.get("worker_id") or "").startswith(f"{role}:")
            and str(event.get("kind") or "").startswith("worker.")][-20:]
    instance_slots = sum(row["worker_slots"] for row in per_role.values())
    instance_busy = sum(row["busy_slots"] for row in per_role.values())
    if instances:
        slots = instance_slots
    by_stage: dict[str, dict] = {}
    for run in runs:
        stage = run.get("stage") or "unknown"
        stage_row = by_stage.setdefault(stage, {"runs": 0, "running": 0, "queued": 0,
                                                  "completed": 0, "total": 0, "findings": None})
        stage_row["runs"] += 1
        for field in ("running", "queued", "completed", "total"):
            stage_row[field] += int(run.get(field) or 0)
        # Summed only where it was counted. A stage whose runs report no findings gets None rather
        # than 0, so "no findings yet" and "findings not counted for this stage" stay different —
        # only assess runs carry a count (see store.admin_live_activity).
        if run.get("findings") is not None:
            stage_row["findings"] = int(stage_row.get("findings") or 0) + int(run["findings"])
    # The queue's own composition and rates, for the Live Operations queue visualization. Guarded
    # because an older store may not carry it: the drawer renders a missing row as "Not reported"
    # rather than as zero, so degrading to absent is honest and degrading to {} would not be.
    composition = None
    _qc = getattr(core.store, "queue_composition", None)
    if callable(_qc):
        try:
            composition = _qc()
        except Exception:
            composition = None
    # Canonical delivery and stop-acknowledgement health. These are global operational facts on
    # the admin surface, like worker capacity and the shared queue. Missing methods during a
    # rolling deploy remain explicitly unavailable; an old replica must never manufacture a
    # healthy zero for tables it cannot read.
    outbox_health = None
    _outbox_health = getattr(core.store, "stage_outbox_health", None)
    if callable(_outbox_health):
        try:
            outbox_health = _outbox_health()
        except Exception:
            swallowed("routes.system._admin_activity_snapshot: reading stage outbox health failed")
    cancellation_health = None
    _cancel_health = getattr(core.store, "stage_cancellation_health", None)
    if callable(_cancel_health):
        try:
            cancellation_health = _cancel_health()
        except Exception:
            swallowed("routes.system._admin_activity_snapshot: reading cancellation health failed")
    if queued and not wt.get("alive"):
        pressure = "stalled"
    elif queued and slots and (instance_busy if instances else running) >= slots:
        pressure = "saturated"
    elif queued:
        pressure = "busy"
    else:
        pressure = "healthy"
    workflows = _workflow_rows(runs, stage_events, _liveops_canonical_lineages(runs, stage_events))
    # "Running jobs" is a live-work count, not a count of every workflow that did not end in
    # success. Failed and stopped workflows remain available under their explicit filters, but
    # counting them in this headline produces a non-zero tab beside an empty Active view.
    running_workflows = _running_workflow_count(workflows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs": runs,
        "workflows": workflows,
        "summary": {
            "active_runs": sum(1 for r in runs if r.get("status") == "active"),
            "recent_runs": sum(1 for r in runs if r.get("status") == "recent"),
            "active_users": len({r.get("owner") for r in runs if r.get("owner")}),
            "waiting_users": len({r.get("owner") for r in runs if r.get("owner") and r.get("queued")}),
            "queued": queued,
            "running": running,
            "completed_jobs": int(stats.get("done") or 0),
            "worker_slots": slots,
            "available_slots": max(0, int(slots or 0) - (instance_busy if instances else running)),
            "utilization_pct": (min(100, round((instance_busy / slots) * 100))
                                if instances and slots else None),
            "pressure": pressure,
            "scheduling_policy": "tenant_fair_least_loaded",
            "worker_tier_alive": bool(wt.get("alive")),
            "worker_roles": worker_roles,
            "dedicated_release_workers": core.dedicated_release_enabled(),
            "worker_capacity_by_role": per_role,
            "by_stage": by_stage,
            "active_workflows": sum(1 for row in workflows if row["status"] != "completed"),
            "running_workflows": running_workflows,
            "recent_workflows": sum(1 for row in workflows if row["status"] == "completed"),
            "workflow_correlation": {
                "attributed_stage_runs": len(runs),
                "unlinked_active_jobs": unlinked_active_jobs,
                "complete": unlinked_active_jobs == 0 if unlinked_active_jobs is not None else None,
            },
            "recovery": recovery,
            **({"canonical_delivery": outbox_health} if outbox_health is not None else {}),
            **({"cancellation_acknowledgements": cancellation_health}
               if cancellation_health is not None else {}),
            # During mixed-version rollout an empty registry is unavailable, not zero capacity.
            # Once any process has reported, worker_capacity_by_role contains the fresh/stale
            # split and every raw instance needed by the authorized operations drawer.
            # So per-replica ACP health is reported as unavailable, with the reason, and what IS
            # known is reported per SERVICE from the role heartbeats.
            # Whether distributed tracing is on, and why not when it is off. Live Operations
            # offers a trace drill-down from a workflow tile; a link to traces that do not exist
            # is worse than no link, so the UI is given the reason rather than a bare boolean.
            "tracing": _tracing_status(),
            "worker_instance_attribution": {"available": bool(instances),
                "reason": None if instances else "Per-replica capacity is not yet reporting. Jobs in flight are available, but slot utilization cannot be calculated honestly."},
            # Distinct from the block above: that one is about SLOT capacity per replica, this is
            # about which replica holds each running JOB. They can disagree honestly — a replica
            # whose telemetry row has gone stale can still be visibly holding claims.
            "job_attribution": job_attribution,
            # Absent, not empty, when the store cannot answer — see the guard above.
            **({"queue": composition} if composition else {}),
        },
    }


def _recovery_summary(events: list[dict] | None) -> dict:
    """Bounded aggregate over the same 24-hour durable stage stream used by the map.

    No actor, owner, filename or error detail is returned. A completed cancellation is matched
    to its request by workflow, stage and execution correlation so unrelated cancellations do
    not inflate the recovery success figure.
    """
    events = events or []
    def _key(row):
        return (str(row.get("scan_id") or ""), str(row.get("stage") or ""),
                str(row.get("correlation_id") or ""))

    requested = {_key(row): row for row in events
                 if row.get("kind") == "workflow.stage_cancel_requested"}
    cancelled = {_key(row): row for row in events if row.get("kind") == "job.stage_cancelled"}
    resolved_keys = requested.keys() & cancelled.keys()
    resolved = len(resolved_keys)
    durations = []
    for key in resolved_keys:
        try:
            started = datetime.fromisoformat(str(requested[key].get("occurred_at")).replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(cancelled[key].get("occurred_at")).replace("Z", "+00:00"))
            durations.append(max(0, int((ended - started).total_seconds())))
        except (TypeError, ValueError):
            continue
    durations.sort()
    median_seconds = (durations[len(durations) // 2] if len(durations) % 2 else
                      round((durations[len(durations) // 2 - 1] + durations[len(durations) // 2]) / 2)) \
        if durations else None
    actions = [row for row in events if row.get("kind") in (
        "workflow.stage_cancel_requested", "workflow.stage_resumed")]
    latest = max((str(row.get("occurred_at") or "") for row in actions), default="") or None
    return {
        "window_hours": 24,
        "cancel_requests": len(requested),
        "cancel_resolved": resolved,
        "cancel_pending": max(0, len(requested) - resolved),
        "cancel_success_pct": round(resolved / len(requested) * 100) if requested else None,
        "median_cancel_seconds": median_seconds,
        "resumes": sum(1 for row in events if row.get("kind") == "workflow.stage_resumed"),
        "latest_action_at": latest,
    }


def _liveops_canonical_lineages(runs: list[dict], lifecycle_events: list[dict] | None = None) -> dict[str, dict]:
    """Read canonical stage state without making Live Ops unavailable during a rolling deploy."""
    reader = getattr(core.store, "canonical_stage_lineage", None)
    if not callable(reader):
        return {}
    scan_ids = {str(row.get("scan_id") or "").strip() for row in runs}
    scan_ids.update(str(row.get("scan_id") or "").strip() for row in lifecycle_events or [])
    lineages = {}
    for scan_id in sorted(scan_ids - {""}):
        try:
            lineage = reader(scan_id)
        except Exception:
            swallowed("routes.system._liveops_canonical_lineages: reading lineage failed")
            continue
        if lineage and lineage.get("available"):
            remediate = next((row for row in lineage.get("stages", [])
                              if row.get("stage") == "remediate"), None)
            facts_reader = getattr(core.store, "remediation_run_facts", None)
            if remediate and callable(facts_reader):
                try:
                    import remediation_run
                    snapshot = remediation_run.build_snapshot(facts_reader(scan_id))
                    remediate["finding_accounting"] = {
                        "finding_reconciliation": snapshot.get("finding_reconciliation"),
                        "fixes": snapshot.get("fixes"),
                        "review": snapshot.get("review"),
                        "last_durable_update_at": snapshot.get("latest_progress_at"),
                        "revision": snapshot.get("revision"),
                    }
                except Exception:
                    swallowed("routes.system._liveops_canonical_lineages: reading finding accounting failed")
            lineages[scan_id] = lineage
    return lineages


def _workflow_rows(runs: list[dict], lifecycle_events: list[dict] | None = None,
                   canonical_lineages: dict[str, dict] | None = None) -> list[dict]:
    """Turn stage aggregates into the durable workflow contract used by Live Ops.

    New scans carry a persisted workflow execution and revision. Its external id deliberately
    equals the first scan id in revision 1, preserving every existing deep link while making the
    relationship first-class; legacy rows use that same fallback. Stage ids are deterministic
    because the current queue model has one aggregate stage per scan; individual attempts remain
    inspectable in the stage detail.
    """
    # Event-only stages are projection inputs, not queue rows. Work on a copy so returning the
    # canonical workflow history cannot silently widen the endpoint's separate `runs` contract.
    runs = list(runs)
    grouped: dict[str, dict] = {}
    event_rows: dict[str, list[dict]] = {}
    safe_kinds = {"job.stage_started", "job.stage_completed", "job.stage_failed",
                  "job.stage_cancelled", "workflow.stage_cancel_requested",
                  "workflow.stage_resumed"}
    # This is a cross-user operations endpoint, so project only the closed lifecycle vocabulary
    # and counted detail. Free-text messages, filenames and raw payloads never cross this boundary.
    for event in lifecycle_events or []:
        scan_id = str(event.get("scan_id") or "")
        if not scan_id or event.get("kind") not in safe_kinds:
            continue
        detail = event.get("detail") or {}
        event_rows.setdefault(scan_id, []).append({
            "event_id": event.get("event_id"), "kind": event.get("kind"),
            "stage": event.get("stage"), "occurred_at": event.get("occurred_at"),
            "correlation_id": event.get("correlation_id"), "attempt": event.get("attempt"),
            "error_class": event.get("error_class"),
            "detail": {key: detail[key] for key in ("documents", "completed", "failed")
                       if key in detail},
        })
    for rows_for_scan in event_rows.values():
        rows_for_scan.sort(key=lambda row: (str(row.get("occurred_at") or ""),
                                            str(row.get("event_id") or "")))
    durable_stages: dict[tuple[str, str], dict] = {}
    for event in lifecycle_events or []:
        if event.get("kind") not in ("job.stage_started", "job.stage_completed",
                                      "job.stage_failed", "job.stage_cancelled"):
            continue
        key = (str(event.get("scan_id") or ""), str(event.get("stage") or ""))
        if not all(key):
            continue
        state = durable_stages.setdefault(key, {})
        transition = event["kind"].removeprefix("job.stage_")
        previous = state.get(transition)
        if not previous or (str(event.get("occurred_at") or ""), str(event.get("event_id") or "")) > \
                (str(previous.get("occurred_at") or ""), str(previous.get("event_id") or "")):
            state[transition] = event
    represented = {(str(run.get("scan_id") or ""), str(run.get("stage") or "")) for run in runs}
    # A terminal stage remains part of the flow after its queue rows leave the recent tail. Only
    # a terminal event can be reconstructed without live jobs; a lone old start is not evidence that
    # work is still active, so it is deliberately not synthesized.
    for key, state in durable_stages.items():
        terminal = max((event for name, event in state.items() if name != "started"),
                       key=lambda event: str(event.get("occurred_at") or ""), default=None)
        if key in represented or not terminal:
            continue
        completed = terminal
        started = state.get("started") or {}
        detail = completed.get("detail") or {}
        runs.append({
            "scan_id": key[0], "stage": key[1], "owner": completed.get("owner_email"),
            "source": completed.get("source") or "unknown",
            "status": ("recent" if completed.get("kind") == "job.stage_completed" else
                       "cancelled" if completed.get("kind") == "job.stage_cancelled" else "failed"),
            "running": 0, "queued": 0, "failed": 0,
            "completed": int(detail.get("documents") or 0),
            "total": int(detail.get("documents") or 0), "max_attempts_seen": completed.get("attempt"),
            "started_at": started.get("occurred_at"), "updated_at": completed.get("occurred_at"),
        })
    # Canonical executions define the workflow's stage set. Queue rows and lifecycle events are
    # intentionally lossy operational projections: synchronous stages have no job, and terminal
    # jobs eventually age out of both projections. Seed any missing canonical stage before the
    # telemetry pass so those durable facts cannot disappear from Live Operations.
    represented = {(str(run.get("scan_id") or ""), str(run.get("stage") or "")) for run in runs}
    run_by_scan = {}
    for run in runs:
        run_by_scan.setdefault(str(run.get("scan_id") or ""), run)
    for scan_id, lineage in (canonical_lineages or {}).items():
        exemplar = run_by_scan.get(str(scan_id)) or {}
        for canonical in (lineage or {}).get("stages", []):
            stage = str(canonical.get("stage") or "").strip()
            key = (str(scan_id), stage)
            if not stage or key in represented:
                continue
            runs.append({
                "scan_id": str(scan_id), "stage": stage,
                "workflow_id": canonical.get("workflow_id") or lineage.get("workflow_id"),
                "workflow_revision": (canonical.get("workflow_revision") or
                                      lineage.get("workflow_revision") or 1),
                "owner": (lineage.get("owner_email") or lineage.get("owner") or
                          exemplar.get("owner") or "unknown"),
                "source": lineage.get("source") or exemplar.get("source") or "unknown",
                "running": 0, "queued": 0, "failed": 0, "completed": 0, "total": 0,
                "started_at": canonical.get("created_at"),
                "updated_at": canonical.get("last_durable_update_at"),
                "max_attempts_seen": 0,
            })
            represented.add(key)
    stage_order = {"discover": 0, "assess": 1, "remediate": 2, "release": 3}
    now = datetime.now(timezone.utc)
    for run in runs:
        scan_id = str(run.get("scan_id") or "").strip()
        stage = str(run.get("stage") or "").strip()
        if not scan_id or not stage:
            continue
        workflow = grouped.setdefault(scan_id, {
            "workflow_id": run.get("workflow_id") or scan_id,
            "workflow_revision": int(run.get("workflow_revision") or 1),
            "scan_id": scan_id,
            "owner_display_name": run.get("owner") or "unknown",
            "source": run.get("source") or "unknown",
            "created_at": run.get("started_at"),
            "updated_at": run.get("updated_at"),
            "status": "completed",
            "current_stage": None,
            "available_next_actions": [],
            "events": event_rows.get(scan_id, []),
            "stages": [],
        })
        if str(run.get("started_at") or "") < str(workflow.get("created_at") or run.get("started_at") or ""):
            workflow["created_at"] = run.get("started_at")
        if str(run.get("updated_at") or "") > str(workflow.get("updated_at") or ""):
            workflow["updated_at"] = run.get("updated_at")
        stage_status = ("running" if int(run.get("running") or 0) else
                        "waiting" if int(run.get("queued") or 0) else
                        "failed" if int(run.get("failed") or 0) else "completed")
        durable = durable_stages.get((scan_id, stage), {})
        durable_start = durable.get("started") or {}
        durable_terminal = max((event for name, event in durable.items() if name != "started"),
                               key=lambda event: str(event.get("occurred_at") or ""), default={})
        durable_completion = durable_terminal if durable_terminal.get("kind") == "job.stage_completed" else {}
        durable_failure = durable_terminal if durable_terminal.get("kind") in ("job.stage_failed", "job.stage_cancelled") else {}
        if durable_failure and not int(run.get("running") or 0) and not int(run.get("queued") or 0):
            stage_status = "cancelled" if durable_failure.get("kind") == "job.stage_cancelled" else "failed"
        heartbeat = run.get("current_job_heartbeat_at")
        stalled = False
        if stage_status == "running" and heartbeat:
            try:
                beat = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
                if beat.tzinfo is None:
                    beat = beat.replace(tzinfo=timezone.utc)
                stalled = (now - beat).total_seconds() > 360
            except (TypeError, ValueError):
                stalled = False
        # A stage-completed event is the authoritative once-only transition for new executions.
        # Queue aggregation remains the rolling-deploy fallback for jobs completed before the
        # emitter existed, and continues to provide live document counts.
        if durable_completion and stage_status == "completed":
            completed_at = durable_completion.get("occurred_at")
        else:
            completed_at = run.get("updated_at") if stage_status == "completed" else None
        workflow["stages"].append({
            "stage": stage,
            "stage_run_id": (durable_terminal.get("correlation_id")
                             or durable_start.get("correlation_id")
                             or f"{scan_id}:{stage}"),
            "attempt": max(1, int(run.get("max_attempts_seen") or 0)),
            "status": stage_status,
            "total": int(run.get("total") or 0),
            "completed": int(run.get("completed") or 0),
            "active": int(run.get("running") or 0),
            "waiting": int(run.get("queued") or 0),
            "failed": int(run.get("failed") or 0),
            "started_at": durable_start.get("occurred_at") or run.get("started_at"),
            "completed_at": completed_at,
            "completion_recorded": bool(durable_completion),
            "terminal_outcome": ("failed" if durable_failure.get("kind") == "job.stage_failed" else
                                 "cancelled" if durable_failure else
                                 "completed" if durable_completion else None),
            "error_class": durable_failure.get("error_class") or run.get("last_error_class"),
            "latest_progress_at": run.get("updated_at"),
            "stalled": stalled,
            "paused": run.get("paused") is True,
            "cancel_requested": run.get("cancel_requested") is True,
            "cancel_requested_at": run.get("cancel_requested_at"),
            "waiting_reason": "worker_heartbeat_stale" if stalled else None,
            "next_retry_at": None,
        })
    for workflow in grouped.values():
        lineage = (canonical_lineages or {}).get(workflow["scan_id"])
        canonical_by_stage = {row.get("stage"): row for row in (lineage or {}).get("stages", [])}
        for stage in workflow["stages"]:
            canonical = canonical_by_stage.get(stage["stage"])
            stage["canonical"] = canonical
            if not canonical:
                continue
            counts = (canonical.get("counts") or {}).get("work_items") or {}
            stage.update({
                "stage_run_id": canonical.get("execution_id") or stage["stage_run_id"],
                "status": ({"succeeded": "completed", "cancelled": "cancelled",
                            "failed": "failed", "integrity_failed": "failed",
                            "queued": "waiting", "paused": "waiting"}
                           .get(canonical.get("state"), "running")),
                "total": counts.get("total"), "completed": counts.get("completed"),
                "active": counts.get("processing"), "waiting": counts.get("queued"),
                "failed": counts.get("failed"), "cancelled": counts.get("cancelled"),
                "skipped": counts.get("skipped"),
                "latest_progress_at": canonical.get("last_durable_update_at"),
                "cancel_requested": bool((canonical.get("control") or {}).get("cancel_requested")),
                "cancel_requested_at": (canonical.get("control") or {}).get("cancel_requested_at"),
            })
            state = canonical.get("state")
            if state == "cancelled":
                stage["terminal_outcome"] = "cancelled"
            elif state in ("failed", "integrity_failed"):
                stage["terminal_outcome"] = "failed"
            elif state == "succeeded":
                stage["terminal_outcome"] = "completed"
            workflow["workflow_revision"] = int(
                canonical.get("workflow_revision") or (lineage or {}).get("workflow_revision") or 1)
            canonical_updated = canonical.get("last_durable_update_at")
            if str(canonical_updated or "") > str(workflow.get("updated_at") or ""):
                workflow["updated_at"] = canonical_updated
        workflow["stages"].sort(key=lambda row: (stage_order.get(row["stage"], 99), row["stage"]))
        active = [row for row in workflow["stages"] if row["status"] != "completed"]
        if active:
            workflow["current_stage"] = active[-1]["stage"]
            workflow["status"] = ("stopping" if any(row.get("cancel_requested") and row["status"] != "cancelled"
                                                      for row in active) else
                                  "running" if any(row["status"] == "running" for row in active) else
                                  "waiting" if any(row["status"] == "waiting" for row in active) else
                                  "stopped" if all(row["status"] == "cancelled" for row in active) else
                                  "failed")
        elif workflow["stages"]:
            workflow["current_stage"] = workflow["stages"][-1]["stage"]
    return sorted(grouped.values(), key=lambda row: str(row.get("updated_at") or ""), reverse=True)


def _redact_foreign_filenames(runs, viewer: str) -> list[dict]:
    """Strip document NAMES from runs the viewer does not own.

    The name is the one field on a run that identifies another tenant's content. Everything else
    it reports — stage, job type, phase, WCAG criterion, runtime, attempts, queue and completion
    counts — describes the SYSTEM, so an operator can still see that someone else's remediate
    stage is retrying or stuck. Only the name goes.

    APPLIED TO ADMINS TOO, which is the whole point: a non-admin's runs are already filtered to
    their own below, so an admin's fleet-wide view is the only path by which a cross-tenant
    filename can reach a screen. Before #1574 that was one name per stage; it is now up to
    _IN_FLIGHT_LIMIT of them.

    `None` plus a flag, never a placeholder string. "Withheld from you" and "the handler did not
    report one" are different facts about the same field, and a UI given only an empty value will
    state whichever it happens to assume.

    Rows are COPIED before redaction. They belong to the snapshot this was handed, which the
    caller still holds, and a per-viewer edit must not reach back into it.
    """
    out = []
    for row in runs:
        if str(row.get("owner") or "").strip().lower() == viewer:
            out.append(row)
            continue
        scrubbed = dict(row)
        scrubbed["current_file"] = None
        scrubbed["file_redacted"] = True
        in_flight = scrubbed.get("in_flight")
        if isinstance(in_flight, list):
            scrubbed["in_flight"] = [
                ({**job, "file": None, "file_redacted": True} if isinstance(job, dict) else job)
                for job in in_flight]
        out.append(scrubbed)
    return out


def _scope_activity_snapshot(snapshot: dict, viewer: str) -> dict:
    """Admins see fleet workflows; other signed-in users receive only their own identities.

    Aggregate capacity remains fleet-wide operational context.  Only the records that name or
    identify another user are filtered here, in one place shared by the initial read and SSE.

    Filename redaction runs on BOTH paths rather than only the admin one. For a non-admin the
    foreign rows are gone already, so it changes nothing — and that is exactly why it belongs
    here: the guarantee then holds because of one rule, not because of the interaction between
    two, and a future change to the filter cannot quietly widen it.
    """
    if core.is_admin(viewer):
        scoped = dict(snapshot)
        scoped["runs"] = _redact_foreign_filenames(snapshot.get("runs", []), viewer)
        return scoped
    scoped = dict(snapshot)
    scoped["runs"] = _redact_foreign_filenames(
        [row for row in snapshot.get("runs", [])
         if str(row.get("owner") or "").strip().lower() == viewer], viewer)
    scoped["workflows"] = [row for row in snapshot.get("workflows", [])
                           if str(row.get("owner_display_name") or "").strip().lower() == viewer]
    summary = dict(snapshot.get("summary") or {})
    summary.update({
        "active_runs": sum(1 for row in scoped["runs"] if row.get("status") == "active"),
        "recent_runs": sum(1 for row in scoped["runs"] if row.get("status") == "recent"),
        "active_workflows": sum(1 for row in scoped["workflows"] if row.get("status") != "completed"),
        "running_workflows": _running_workflow_count(scoped["workflows"]),
        "recent_workflows": sum(1 for row in scoped["workflows"] if row.get("status") == "completed"),
        "active_users": 1 if scoped["runs"] else 0,
        "waiting_users": 1 if any(row.get("queued") for row in scoped["runs"]) else 0,
    })
    scoped["summary"] = summary
    return scoped


def _tracing_status() -> dict:
    """Application Insights status for the live map. Guarded like every other optional block: a
    branch without the telemetry module reports it as unavailable rather than failing the
    snapshot."""
    try:
        import telemetry  # noqa: PLC0415
        return telemetry.status()
    except Exception:
        return {"enabled": False, "reason": "telemetry module unavailable", "correlation": "off"}


def _azure_block():
    """The shared Azure capacity reading, or None when it cannot be taken.

    Guarded on every axis because the live map must not go dark for an Azure problem: the control
    module may not be importable, may not expose the cache on an older branch, and the read itself
    may raise. None means "no Azure block" — the reader keeps whatever it last had rather than
    replacing a real reading with an empty one.
    """
    try:
        from routes import control as _control  # noqa: PLC0415 — optional, and imported lazily
        fn = getattr(_control, "cached_capacity", None)
        return fn() if callable(fn) else None
    except Exception:
        return None


# Environment variables whose VALUES must never appear in a support bundle. Matched on the NAME,
# because the value of a variable called ACP_DB_PASSWORD is a secret whatever it looks like, and a
# scanner that recognised only high-entropy strings would pass a password of "changeme".
_SECRET_ENV_HINTS = ("secret", "password", "passwd", "token", "key", "credential", "connection",
                     "conn_str", "connectionstring", "sas", "dsn", "url")

# Names that CONTAIN a hint but are not secrets — public URLs and feature flags. Kept short and
# explicit; anything not listed is treated as a secret, which is the safe direction.
_SECRET_ENV_EXEMPT = frozenset({"ACP_PUBLIC_URL", "ACP_BASE_URL", "ACP_SITE_URL"})


def _secret_env_values() -> set[str]:
    """Every environment value this process holds that could be a credential.

    THE ALLOW-LIST IS THE NAME, AND THE CHECK IS THE VALUE. `verify` below greps the ASSEMBLED
    bundle for each of these literals, which is the only redaction that can be verified rather
    than promised: field-level care cannot catch a credential that arrives inside a message
    somebody added last week.
    """
    import os
    out: set[str] = set()
    for name, value in os.environ.items():
        v = (value or "").strip()
        if len(v) < 6 or name in _SECRET_ENV_EXEMPT:
            # Under six characters a "secret" is more likely to be a substring of ordinary prose
            # ("true", "1", "acp") and grepping for it would redact the whole bundle.
            continue
        if any(h in name.lower() for h in _SECRET_ENV_HINTS):
            out.add(v)
    return out


def _bundle_leaks(bundle: dict, secrets: set[str]) -> list[str]:
    """Names of bundle sections containing a literal secret value. Empty is the only good answer."""
    import json as _json
    leaked = []
    for section, payload in bundle.items():
        try:
            text = _json.dumps(payload)
        except (TypeError, ValueError):
            text = str(payload)
        if any(sv in text for sv in secrets):
            leaked.append(section)
    return sorted(leaked)


@router.get("/admin/support-bundle")
def admin_support_bundle(request: Request, response: Response):
    """A redacted diagnostics export an operator can attach to a support ticket (PRD §13, §20.6).

    §20.6 IS AN ABSOLUTE: "Secrets never appear in configuration output or support bundles." This
    route treats that as something to CHECK rather than to intend. The bundle is assembled from an
    allow-list of readings, and then every value this process holds under a credential-shaped
    environment name is grepped for in the assembled result. If one is present the bundle is NOT
    returned — a 500 naming the offending section, and nothing else, because a bundle that leaks
    is worse than no bundle and an error a developer must fix is better than a quiet redaction
    that hides the bug.

    AN ALLOW-LIST OF READINGS, NOT A DUMP WITH THINGS REMOVED. Every section below is composed
    from readings this application already exposes — build provenance, dependency readiness, the
    audit trail's shape, counts. `os.environ` is never serialised, not even filtered: a filtered
    environment ships the variable somebody adds tomorrow.

    NO DOCUMENT NAMES, NO USER IDENTITIES. PRD §13 names both. Counts are carried and identities
    are not, so the bundle says how much work exists without saying whose or about what.

    Admin-only, for the same reason `/admin/audit-events` is.
    """
    import datetime as _dt
    import os
    _require_admin(request)
    response.headers["Cache-Control"] = "no-store"

    info = _build_info()
    try:
        audit = core.store.list_audit_events(limit=25)
    except Exception:  # noqa: BLE001 — a diagnostics export must survive a degraded database
        swallowed("routes.system.admin_support_bundle: reading the audit trail failed")
        audit = []

    bundle = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        # WHICH BYTES ARE RUNNING. The first question any support ticket has to answer, and the
        # one #1529 added `commit` for.
        "release": {
            "version": info.get("version"),
            "commit": info.get("commit"),
            "built_at": info.get("built_at"),
            "version_stamped": info.get("version_stamped"),
        },
        # WHERE it is running. Enum-shaped words from the deployment document, never endpoints:
        # a hostname is not a credential but it is a customer's topology.
        #
        # REPORTED VERBATIM, NEVER COMPARED. ADR 0048's rule is that nothing may BRANCH on
        # `ACP_PLATFORM` — the moment application code did, "one package, four clouds" would stop
        # being true. Provenance-reporting is the use the variable exists for (and this bundle is
        # named in that rule's own test), so the values are copied through as-is: normalising
        # `ACP_AI_LOCAL_ONLY` into a boolean here would be a comparison on a provenance label, and
        # a support bundle is more useful anyway when it says exactly what the workload was given.
        "deployment": {
            "platform": os.environ.get("ACP_PLATFORM") or None,
            "profile": os.environ.get("ACP_DEPLOY_PROFILE") or None,
            "environment": os.environ.get("ACP_DEPLOY_ENV") or None,
            "ai_local_only": os.environ.get("ACP_AI_LOCAL_ONLY") or None,
        },
        # WHETHER ITS DEPENDENCIES ARE THERE — presence and reachability as booleans, never the
        # connection strings that would say where they are.
        "dependencies": {
            "database_configured": bool(os.environ.get("DATABASE_URL")),
            "redis_configured": bool(os.environ.get("REDIS_URL")),
            # `ACP_BLOB_ACCOUNT` ONLY, and deliberately not `OBJECT_STORAGE`. The chart projects
            # both, but `tests/test_packaging_seams.py` records OBJECT_STORAGE as DEAD wiring —
            # required by the contract and read by nothing — so an installation can have it set
            # and store nothing at all. Reading it here would report object storage as configured
            # on exactly the installations that silently drop every remediated document.
            "object_storage_configured": bool(os.environ.get("ACP_BLOB_ACCOUNT")),
            "pdf_engine": pdf_engine_status().get("available"),
        },
        # The audit trail's SHAPE, not its contents beyond the platform events it already scopes
        # itself to. Types and a count, so a ticket can show that auditing works.
        "audit": {
            "recent_event_types": sorted({str(e.get("type")) for e in audit if e.get("type")}),
            "recent_event_count": len(audit),
            "covers": sorted(core.store.PLATFORM_AUDIT_PREFIXES),
        },
        "redacted": True,
    }

    secrets = _secret_env_values()
    leaked = _bundle_leaks(bundle, secrets)
    if leaked:
        # NOT REDACTED AND RETURNED. A section that leaked once will leak again in a shape the
        # scrubber does not recognise; refusing makes it a bug somebody fixes rather than a
        # silent near-miss. The offending VALUE is never named — that would put it in the error
        # this route exists to keep it out of.
        raise HTTPException(500, {
            "code": "support_bundle_would_leak",
            "message": "the assembled support bundle contained a credential-shaped environment "
                       "value and was not returned",
            "sections": leaked,
        })
    return bundle


@router.get("/admin/audit-events")
def admin_audit_events(request: Request, response: Response, limit: int = Query(100, ge=1, le=1000),
                       since: str | None = Query(None)):
    """The platform audit trail: deployment, configuration and capacity changes (PRD §13).

    ADMIN-ONLY, ENFORCED HERE. `_require_admin` rather than `_require_user`: an audit log names who
    changed what and when, and any allow-listed user could otherwise read the platform's whole
    administrative history. The SPA hiding a tab is not enforcement.

    WHAT IT DELIBERATELY DOES NOT CARRY. `decision_log` holds per-document decisions as well as
    platform ones, and those rows carry `file` — a customer's document name. PRD §13 requires
    document names to stay out of exported diagnostics, and an audit export lands in support
    tickets, so `list_audit_events` filters to an allow-list of platform action prefixes and the
    shape has no `file` field at all. A prefix allow-list rather than a blocklist, because a
    blocklist exports document names the day somebody adds an action nobody thought to exclude.

    A FRESH INSTALLATION IS NOT EMPTY. Startup records `deployment.started` (§15, §20.12), so an
    installation that has changed nothing still answers with the release it is running — which is
    what makes "no events" mean "audit logging is broken" rather than "nothing has happened yet".
    """
    _require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    events = core.store.list_audit_events(limit=limit, since=since)
    return {
        "events": events,
        "count": len(events),
        # Named so a reader can see the boundary rather than inferring it from what happens to be
        # present, and so a narrowing of the allow-list is visible in the response itself.
        "covers": sorted(core.store.PLATFORM_AUDIT_PREFIXES),
        "document_scoped_excluded": True,
    }


@router.get("/admin/activity")
def admin_activity(request: Request, response: Response):
    """Payload-sanitized cross-user processing topology for signed-in workspace users.

    Carries the Azure capacity block too, so a page that has just loaded has the infrastructure
    reading immediately rather than waiting for the stream's next Azure frame."""
    _require_user(request)
    response.headers["Cache-Control"] = "no-store"
    viewer = str(getattr(request.state, "user_email", "") or "").strip().lower()
    snapshot = _scope_activity_snapshot(_admin_activity_snapshot(), viewer)
    azure = _azure_block()
    if azure is not None:
        snapshot["azure"] = azure
    return snapshot


@router.post("/admin/activity/workflows/{scan_id}/stages/{stage}/cancel")
def cancel_workflow_stage(scan_id: str, stage: str, request: Request):
    """Platform-admin recovery action: stop only the selected workflow stage."""
    _require_admin(request)
    if stage not in ("discover", "assess", "remediate", "release"):
        raise HTTPException(400, "this stage cannot be cancelled here")
    scan = core.store.get_scan(scan_id)
    if scan is None:
        raise HTTPException(404, "workflow not found")
    actor = str(getattr(request.state, "user_email", "") or "").strip().lower()
    if stage == "discover":
        if not core.store.cancel_scan(scan_id):
            raise HTTPException(409, "no active discovery execution was found")
        core.store._update_workflow_stage(scan_id, "discover", "cancelled")
        owner = core.store._stage_owner(scan_id)
        if owner:
            document_count = scan.get("files")
            if not isinstance(document_count, (int, float)):
                document_count = (scan.get("summary") or {}).get("files", 0)
            core.store.append_orchestration_event(
                event_id=core.store._stage_event_id(
                    scan_id, "discover", f"{scan_id}:discover", "cancelled"),
                owner_email=owner, kind="job.stage_cancelled", scan_id=scan_id,
                workflow=scan_id, stage="discover", correlation_id=f"{scan_id}:discover",
                error_class="cancelled",
                detail={"documents": int(document_count or 0), "cancelled": 1,
                        "stage_execution_id": f"{scan_id}:discover"})
            core.store.append_orchestration_event(
                owner_email=owner, kind="workflow.stage_cancel_requested", scan_id=scan_id,
                workflow=scan_id, stage="discover", correlation_id=f"{scan_id}:discover",
                detail={"requested_by": actor, "scope": "active discovery"})
        return {"workflow_id": scan_id, "stage": stage, "found": True,
                "cancelled": 1, "requested": 0}
    result = core.store.request_stage_cancel(scan_id, stage, actor=actor)
    if not result.get("found"):
        raise HTTPException(409, "no durable stage execution was found")
    return {"workflow_id": scan_id, "stage": stage, **result}


@router.post("/admin/activity/workflows/{scan_id}/stages/remediate/resume")
def resume_workflow_remediation(scan_id: str, request: Request):
    """Platform-admin recovery action: release a remediation hold already recorded by ACP."""
    _require_admin(request)
    if core.store.get_scan(scan_id) is None:
        raise HTTPException(404, "workflow not found")
    if not core.store.remediation_run_paused(scan_id):
        raise HTTPException(409, "remediation is not paused")
    actor = str(getattr(request.state, "user_email", "") or "").strip().lower()
    result = core.store.resume_remediation_run(scan_id, actor=actor)
    owner = core.store._stage_owner(scan_id)
    if owner:
        core.store.append_orchestration_event(
            owner_email=owner, kind="workflow.stage_resumed", scan_id=scan_id,
            workflow=scan_id, stage="remediate", correlation_id=scan_id,
            detail={"requested_by": actor, "released": result.get("released", 0)})
    return {"workflow_id": scan_id, "stage": "remediate", "paused": False, **result}


def _activity_signature(snapshot: dict) -> str:
    """Stable identity for every activity field that can change the live UI."""
    return json.dumps(
        {"runs": snapshot.get("runs", []),
         "workflows": snapshot.get("workflows", []),
         "summary": snapshot.get("summary", {})},
        sort_keys=True, default=str,
    )


@router.get("/admin/activity/stream")
async def admin_activity_stream(request: Request):
    """Authenticated SSE snapshots for the live multi-user traffic map."""
    import asyncio

    _require_user(request)
    viewer = str(getattr(request.state, "user_email", "") or "").strip().lower()

    async def _gen():
        last = None
        last_measured = None
        idle = 0
        while not await request.is_disconnected():
            snapshot = _scope_activity_snapshot(
                await asyncio.to_thread(_admin_activity_snapshot), viewer)
            # Durable workflow rows can change without a queue row or aggregate moving (for
            # example, a stage records its terminal outcome after the last job leaves the live
            # tail). Include them in change detection so that the UI receives those transitions
            # immediately instead of waiting for an unrelated job or summary change.
            signature = _activity_signature(snapshot)
            if signature != last:
                last = signature
                idle = 0
                yield f"event: activity\ndata: {json.dumps(snapshot, default=str)}\n\n"
            else:
                idle += 1
                if idle >= 5:
                    idle = 0
                    yield ": keep-alive\n\n"
            # Azure rides the SAME stream, on its OWN event, and that separation is the point.
            # The activity frame fires on any job change — several times a minute under load —
            # while the Azure block is a comparatively large payload (fourteen metrics, each with
            # its own fifteen-minute series) that Azure itself only resamples once a minute.
            # Attaching it to every activity frame would multiply the stream's size for data that
            # had not changed.
            #
            # Emitted when the reading was actually REFRESHED (measured_at moved), not when its
            # values changed. A reading that comes back identical is still news: it is what makes
            # "Azure Monitor · 20s ago" true. Gating on the values instead would leave the UI
            # showing a stale age for a figure that had just been re-measured — understating
            # freshness, which is the direction that misleads.
            azure = await asyncio.to_thread(_azure_block)
            measured = azure.get("measured_at") if isinstance(azure, dict) else None
            if azure is not None and measured != last_measured:
                last_measured = measured
                idle = 0
                yield f"event: azure\ndata: {json.dumps(azure, default=str)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(_gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-store",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@router.get("/jobs/{job_id}")
def queue_job(job_id: str, request: Request):
    """Status of one durable-queue job, owner-scoped via its scan — lets the UI show
    REAL progress for a single-file remediation (queued → running → done/dead)
    instead of a timed guess. Slim view only: payload can hold another tenant's
    filenames, so it is never returned.

    `phase` and `locked_at` (2026-08-22) round this out into what AssessRunner's deferred poll
    needed and didn't have: not just "queued vs running" but WHAT a running job is doing right
    now (phase, written by the handler as it works — same field the queue panel already reads)
    and WHEN a worker actually claimed it (locked_at), so "waiting for a worker" and "a worker
    has been on this for 40s" read as the different situations they are, instead of both showing
    an identical, silent 0%."""
    j = core.store.get_job(job_id)
    owner = getattr(request.state, "user_email", None) or "demo"
    if j is None or not j.get("scan_id") or core.store.get_scan(j["scan_id"], owner=owner) is None:
        raise HTTPException(404, "job not found")
    # `attempt`/`max_attempts` are named for the SSE frame, not the column (jobs.attempts), so a
    # reader has ONE name for this fact whichever way it arrived. Without them the retry and
    # interrupted cards could only ever say "attempt N" while a live event frame happened to be
    # in hand — a page reload dropped the number, and the card silently degraded to a card that
    # does not mention which attempt you are watching.
    #
    # Safe for the slim view: a counter is not tenant data. The payload stays excluded.
    return {"id": j["id"], "type": j["type"], "status": j["status"],
            "attempt": j.get("attempts"), "max_attempts": j.get("max_attempts"),
            "attempts": j.get("attempts"), "max_attempts": j.get("max_attempts"), "error": j.get("last_error"),
            "scan_id": j.get("scan_id"), "phase": j.get("phase"), "locked_at": j.get("locked_at")}


@router.post("/admin/jobs/clear-dead")
def clear_dead_jobs(request: Request):
    """Delete the caller's OWN unrecoverable dead-lettered jobs. Owner-scoped so a
    user can't purge another tenant's queue. Re-run the originating action to retry."""
    owner = getattr(request.state, "user_email", None) or "demo"
    return {"purged": core.store.purge_dead_jobs(owner=owner)}


class AIProviderTest(BaseModel):
    # Same extra='forbid' guard as the update model, and for the same reason: this endpoint has
    # no field that could carry a key, and a client that invents one is rejected rather than
    # having the value quietly ignored (or worse, logged with the request).
    model_config = ConfigDict(extra="forbid")
    provider: str


@router.post("/ai/providers/test")
def test_ai_provider(body: AIProviderTest, request: Request):
    """Admin: send a SYNTHETIC probe image to one provider and report what came back.

    NO CUSTOMER DOCUMENT IS SENT. The bytes are providers.probe_image_bytes() — a 64×64 black
    square on white, generated in-process from stdlib zlib. That is what makes this safe to press
    on a provider nobody has agreed to send documents to yet: it is how an admin learns whether
    the credential and the route work BEFORE any real content could go anywhere.

    Works on a provider that is NOT yet enabled, deliberately — testing before enabling is the
    whole point, and requiring the switch first would invert the order. It does require the
    configuration to be complete and the referenced secret to resolve, and says which of the two
    is missing when it is not.

    The response carries no secret: provider, model, zone, latency, outcome reason, real token
    counts and real cost. Never the key, never the value behind key_secret_ref, and not even the
    model's caption of the probe.
    """
    _require_admin(request)
    import providers as _providers
    provider = (body.provider or "").strip().lower()
    if provider not in _providers.CLOUD_PROVIDERS:
        raise HTTPException(422, f"unknown provider '{provider}' — one of {list(_providers.CLOUD_PROVIDERS)}")
    result = _providers.test_connection(provider)
    # Audited like any other admin action, and for a real reason beyond bookkeeping: this is the
    # one path that can make an outbound call to a third party from the Settings page, so who
    # pressed it and what came back belongs in the record. The detail names the outcome, never
    # a credential.
    core.store.log_decision(
        getattr(request.state, "user_email", None) or "admin",
        f"settings.ai_provider.{provider}.test",
        detail=f"connection test → ok={result.get('ok')} reason={result.get('reason')} "
               f"model={result.get('model') or '—'} zone={result.get('zone') or '—'} "
               f"latency_ms={result.get('latency_ms') or '—'} (synthetic probe image; "
               f"no customer document sent)")
    return result


@router.get("/ai/providers/health")
def get_all_ai_provider_health(request: Request,
                               window_hours: int = Query(24, ge=1, le=168)):
    """Admin: health snapshot for ALL cloud vision providers in one call.

    Returns the same fields as the per-provider endpoint, keyed by provider name.
    This is the route the Live Operations panel uses to avoid N round-trips.
    Must be registered BEFORE the /{provider}/health route so FastAPI resolves
    the literal path 'health' here rather than treating it as a provider name.
    """
    _require_admin(request)
    import providers as _providers
    return {
        "window_hours": window_hours,
        "providers": {
            p: core.store.ai_provider_health_stats(p, window_hours=window_hours)
            for p in _providers.CLOUD_PROVIDERS
        },
    }


@router.get("/ai/providers/{provider}/health")
def get_ai_provider_health(provider: str, request: Request,
                           window_hours: int = Query(24, ge=1, le=168)):
    """Admin: endpoint health snapshot for one cloud vision provider, derived from the
    ai_calls table (ADR 0019). All numbers are real aggregates — nothing fabricated (ADR 0016).

    Useful for HuggingFace Dedicated Endpoints specifically: surfaces latency percentiles,
    throttle events (http_429), and cold-start signals (successful calls > 30 s) so Live
    Operations can detect a scale-to-zero wake without polling the HF API directly.

    window_hours: how far back to look (default 24 h, max 168 h / 1 week).
    """
    _require_admin(request)
    import providers as _providers
    if provider not in _providers.CLOUD_PROVIDERS:
        raise HTTPException(422, f"unknown provider '{provider}' — one of {list(_providers.CLOUD_PROVIDERS)}")
    return core.store.ai_provider_health_stats(provider, window_hours=window_hours)
