"""Shared state, config, and helpers for the acp control plane.

Everything that the route modules (routes/*.py) and the access-gate middleware
need in common lives here: env config, the Store singleton, the in-memory JOBS
map, GIS token verification, the Drive client factory, the scheduler, and the
Langfuse remediation span. The route modules import from this module; this module
imports no route module (no cycles).
"""
from __future__ import annotations
import json
import os
import sys
import threading
import time as _time
from pathlib import Path

# Resolve sibling modules (scanner/store/rubric/report/ai/lf) and ../scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from apscheduler.schedulers.background import BackgroundScheduler

from scanner import run_scan
from store import Store
from rubric import Rubric
from swallowed import swallowed

ACP = Path(__file__).resolve().parent.parent

# ── Config (env) ──────────────────────────────────────────────────────────────
ACCESS_CODE = os.environ.get("ACP_ACCESS_CODE")
GOOGLE_CLIENT_ID = os.environ.get("ACP_GOOGLE_CLIENT_ID") or None
# Microsoft Entra app (client) and directory (tenant) ids for the SharePoint/OneDrive connect.
# Served to the SPA via /config so a deployment can be pointed at a tenant WITHOUT rebuilding the
# bundle — the same runtime-config pattern as GOOGLE_CLIENT_ID, and the reason the frontend prefers
# these over its build-time VITE_AZURE_* fallback. Tenant defaults to 'common' only if unset.
AZURE_CLIENT_ID = os.environ.get("ACP_AZURE_CLIENT_ID") or None
AZURE_TENANT_ID = os.environ.get("ACP_AZURE_TENANT_ID") or None


def _load_microsoft_tenants() -> list[dict]:
    """Every Microsoft/Entra app registration this deployment can connect SharePoint/OneDrive
    through, as a data-source connection — NOT the identity provider that gates sign-in to ACP
    itself (that stays AZURE_CLIENT_ID/AZURE_TENANT_ID alone; SignIn.jsx's "Sign in with
    Microsoft" is unchanged by this).

    Single-tenant Entra app registrations (docs/sharepoint-app-registration.md) reach exactly one
    organization's SharePoint/OneDrive each — this deployment was structurally unable to reach a
    second, separate production tenant's estate no matter how a user signed in. `ACP_AZURE_CLIENT_ID`/
    `ACP_AZURE_TENANT_ID` become slot "primary"; `ACP_AZURE_CLIENT_ID_2`/`ACP_AZURE_TENANT_ID_2` (and
    `_3`, `_4`, ...) add more, each requiring its own app registration and admin consent in ITS OWN
    tenant (docs/sharepoint-app-registration.md's steps, repeated per tenant — nothing here
    substitutes for that). `_N_LABEL` names the slot for the connect-source picker; unset falls back
    to "Tenant N". Contiguous numbering only — a gap (2 set, 3 unset, 4 set) stops the scan, since a
    silently-skipped slot is worse than an obviously-missing one.
    """
    tenants = []
    if AZURE_CLIENT_ID and AZURE_TENANT_ID:
        tenants.append({"key": "primary",
                        "label": os.environ.get("ACP_AZURE_TENANT_LABEL") or "Primary",
                        "client_id": AZURE_CLIENT_ID, "tenant_id": AZURE_TENANT_ID})
    n = 2
    while True:
        cid = os.environ.get(f"ACP_AZURE_CLIENT_ID_{n}")
        tid = os.environ.get(f"ACP_AZURE_TENANT_ID_{n}")
        if not (cid and tid):
            break
        tenants.append({"key": f"tenant{n}",
                        "label": os.environ.get(f"ACP_AZURE_TENANT_{n}_LABEL") or f"Tenant {n}",
                        "client_id": cid, "tenant_id": tid})
        n += 1
    return tenants


MICROSOFT_TENANTS = _load_microsoft_tenants()
# Deployment environment. ACP_DEPLOY_ENV is canonical; ACP_ENV is a legacy alias, still read.
#
# ACP_ENV used to mean two different things: this, and the Container Apps *environment name* in
# deploy.sh / standup.sh. docs/production-hardening.md told operators to set ACP_ENV=production,
# which the deploy scripts read as an ACA environment name -- so it never reached the container,
# IS_PROD stayed False on the public demo, and the X-E2E-Key bypass stayed live. The scripts now
# use ACP_ACA_ENV for the environment name and refuse ACP_ENV outright, so the name has exactly
# one meaning again.
#
# The alias is kept because reading it can only make IS_PROD *more* likely true, which is the
# safe direction: an existing container that sets ACP_ENV=production must not silently drop out
# of production mode on upgrade. Set ACP_DEPLOY_ENV in anything new.
IS_PROD = (os.environ.get("ACP_DEPLOY_ENV") or os.environ.get("ACP_ENV") or "").lower() in ("production", "prod")

# Test/demo auth bypasses (X-E2E-Key, X-Demo-Key). FAIL-CLOSED: off unless an operator
# explicitly opts in, and refused in production regardless of the opt-in.
#
# These were previously enabled whenever IS_PROD was false — i.e. enabled by the ABSENCE of
# an env var. Since nothing set ACP_ENV on the container, IS_PROD was False in production and
# the X-E2E-Key gate bypass was live on the public demo. A security control must not depend on
# a variable being present; make the safe state the default and require an explicit opt-in for
# the dangerous one, so no missing/renamed/typo'd variable can reopen the backdoor.
TEST_BYPASS_ENABLED = (
    os.environ.get("ACP_ENABLE_TEST_BYPASS", "").strip().lower() in ("1", "true", "yes")
    and not IS_PROD
)
# Smoke/e2e test key: requests with X-E2E-Key bypass the access gate. Only when the bypass
# is explicitly enabled AND the key is set.
E2E_KEY = (os.environ.get("ACP_E2E_KEY") or None) if TEST_BYPASS_ENABLED else None
# Comma-separated domains allowed in GIS mode. DENY-BY-DEFAULT: empty unless the
# operator configures ACP_ALLOWED_DOMAINS, so a fresh deploy admits no one until
# explicitly opened to a domain.
ALLOWED_DOMAINS = [
    d.strip() for d in os.environ.get("ACP_ALLOWED_DOMAINS", "").split(",") if d.strip()
]
# Comma-separated individual emails allowed in GIS mode, in addition to the
# domains above. Lets you permit a specific outside account (e.g. a personal
# gmail used for a demo) without opening the whole gmail.com domain.
ALLOWED_EMAILS = {
    e.strip().lower() for e in os.environ.get("ACP_ALLOWED_EMAILS", "").split(",") if e.strip()
}
# The one email that can NEVER be removed from the list (anti-lockout safety). Defaults
# to ACP_OWNER_EMAIL, else the first ACP_ALLOWED_EMAILS entry.
OWNER_EMAIL = (os.environ.get("ACP_OWNER_EMAIL", "").strip().lower()
               or (sorted(ALLOWED_EMAILS)[0] if ALLOWED_EMAILS else ""))
# Additional Platform Admins beyond the single protected OWNER_EMAIL. They get the SAME admin
# rights — the scope editor, platform Settings (AI mode, Drive mirror, worker pool, reset), and
# access management — so a team can have more than one admin instead of everything routing through
# one person. Unlike OWNER_EMAIL these are NOT the anti-lockout owner: they can be removed. Comma-
# separated; case-insensitive; empty by default (a fresh deploy still has exactly one owner).
ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get("ACP_ADMIN_EMAILS", "").split(",") if e.strip()
}

# OPEN-ACCESS MODEL. When on (the default), there is NO separate admin view: every user who is
# admitted at all (email_allowed — owner, allow-listed tester, or an allowed domain) sees the same
# screens and can use the same non-destructive features, including creating lifecycle rules. It is
# deliberately NOT a bypass of the login perimeter — an unauthenticated caller (empty email) is
# still nobody — and it does NOT open the genuinely destructive, irreversible actions, which stay
# owner-only via _require_owner: wiping all data (POST /admin/reset), managing who can log in
# (PUT /admin/allowlist, POST /admin/invite, PUT /admin/admins), and authorising/performing a file
# move-or-trash (disposition approve/execute). Set ACP_OPEN_ACCESS=0 to restore role-based admin
# (owner + ACP_ADMIN_EMAILS + owner-promoted store admins).
OPEN_ACCESS = os.environ.get("ACP_OPEN_ACCESS", "1").strip().lower() not in ("0", "false", "no", "off")
# Public base URL for the verify-this-report QR code (R15). When set, the PDF embeds a QR pointing
# to {PUBLIC_URL}/public/verify/{scan_id}. When unset the QR encodes a plain scan-id URI instead.
PUBLIC_URL = os.environ.get("ACP_PUBLIC_URL", "").rstrip("/")

# ── just-in-time roster (owner decision, 2026-09-05) ──────────────────────────
# The workspace role a person is given the first time they successfully sign in without already
# being on the roster. Owner's wording: "Domain-wide access permits authentication, not automatic
# privileges... assign a configurable least-privilege default role, ideally Viewer."
#
# WHY LEAST PRIVILEGE IS THE DEFAULT AND NOT THE OTHER ONE. `ACP_ALLOWED_DOMAINS` is a statement
# about who may AUTHENTICATE — anyone at the company — and it is set once, at deploy, by whoever
# wired up the tenant. Reading it as a grant means one environment variable silently decides what
# several thousand people may do inside the product. Viewer is the role that makes the domain
# decide only the thing it was written to decide.
#
# SET IT EMPTY (or "none") TO HOLD NEW PEOPLE PENDING. That is not the same as giving them
# nothing by accident: the record is still created, they still appear on the People screen, and
# they see an "Access pending" screen that says an administrator has to act — rather than an
# application with every tab hidden, which looks broken and gets reported as a bug.
_DEFAULT_SIGNIN_ROLE_RAW = os.environ.get("ACP_DEFAULT_SIGNIN_ROLE", "viewer").strip().lower()
DEFAULT_SIGNIN_ROLE = "" if _DEFAULT_SIGNIN_ROLE_RAW in ("", "none", "pending") else _DEFAULT_SIGNIN_ROLE_RAW

# Emails this process has already reconciled against the roster. Purely a cost guard: the people
# records live in ONE json blob (store.upsert_person rewrites the whole list), so touching it on
# every authenticated request would be absurd. A miss costs one read; a hit costs nothing. The
# set is per-process and per-restart, which is the right trade — the check it skips is idempotent,
# so the worst case of losing it is one extra read per person per deploy.
_rostered: set[str] = set()
_roster_lock = threading.Lock()


def is_owner(email: str | None) -> bool:
    """The single protected owner (ACP_OWNER_EMAIL) — the root of trust that manages who else is an
    admin, and the one identity that can never be demoted or removed. True for everyone only when no
    owner is configured (local dev / demo / no-auth)."""
    if not OWNER_EMAIL:
        return True
    return (email or "").strip().lower() == OWNER_EMAIL


def is_admin(email: str | None) -> bool:
    """May this identity use the platform-admin surfaces (scope editor, platform Settings, the
    lifecycle rule builder, access-management views)? True for the protected OWNER_EMAIL, any
    ACP_ADMIN_EMAILS entry (env, permanent), any email the owner promoted from Settings (store
    `admin_emails`), or — when no owner is configured (local dev / demo / no-auth) — everyone.

    Under the OPEN_ACCESS model (the default, see the constant above) it is additionally True for ANY
    authenticated, admitted user: there is no separate admin view, so everyone who can sign in gets
    the same screens and features. An empty email (unauthenticated) is still never an admin — the
    login perimeter is unchanged — and the destructive actions are separately owner-gated.

    This is the single source of truth both the SPA flag (is_scope_owner) and the API gate
    (_require_admin) consult, so they can never disagree about who sees the admin surface."""
    if not OWNER_EMAIL:
        return True
    e = (email or "").strip().lower()
    if not e:
        return False
    if e == OWNER_EMAIL or e in ADMIN_EMAILS:
        return True
    if OPEN_ACCESS:
        return True   # any authenticated, admitted user — same views/features for everyone
    try:
        return e in get_store().get_admins()
    except Exception:
        return False


def suspended_in_store(email: str | None) -> bool:
    """Does this person's stored record say `suspended`? RAISES if the store cannot be read.

    THE SINGLE DEFINITION OF SUSPENDED, and the reason it is split from `is_suspended` is the
    failure policy rather than the lookup — the lookup is the same three lines it replaced in
    three separate modules (app._capability_gate_suspended, routes.system._is_suspended,
    routes.workspace_roles_admin._suspended), which is three chances for the next person to change
    one of them.

    THE TWO POLICIES ARE BOTH DELIBERATE AND THEY POINT OPPOSITE WAYS.

    Callers deciding CAPABILITIES want this one, which raises. PRD §14: "Failure to load
    permissions must fail closed for sensitive operations." An unreadable store there means we
    cannot establish what somebody may do, and a 500 is the honest answer to that.

    The authentication PERIMETER wants `is_suspended`, which swallows. A suspension check can only
    ever remove access, so refusing on a failed read protects nothing and converts one unreadable
    settings row into a workspace-wide outage for people who were never suspended.

    Reads only the `people` records, not `people_with_access`. That merge additionally pulls the
    allowlist and the admin list to answer "who should the People screen list"; the question here
    is narrower — "is there a stored record, and does it say suspended".
    """
    target = (email or "").strip().lower()
    if not target:
        return False
    return any(r.get("email") == target and r.get("status") == "suspended"
               for r in get_store().get_people())


def is_suspended(email: str | None) -> bool:
    """Has an administrator withdrawn this person's access? PRD §14: "a suspended user has no
    effective permissions."

    Read LIVE on every call, deliberately un-memoised. The just-in-time roster memo next door
    (`_rostered`) is safe to keep in process because it answers "have we seen them before", a fact
    that only ever moves one way. Suspension is the opposite: it is the act of taking access away
    RIGHT NOW, and a memo would mean an owner clicking Suspend had to wait for a redeploy to be
    obeyed — which is the same class of bug as the one this function exists to close, wearing a
    performance optimisation as a disguise.

    Delegates the lookup to `suspended_in_store` so the perimeter and the capability layer cannot
    drift about who is suspended; only the failure policy differs, and that difference is stated
    there.
    """
    try:
        return suspended_in_store(email)
    except Exception:
        # Fail OPEN, matching the allowlist read below, and the reasoning is worth stating because
        # the instinct here is fail-closed. This check can only ever REMOVE access, so refusing on
        # a failed read does not protect a resource — it turns one unreadable settings row into a
        # workspace-wide outage for people who are not suspended and never were. The store that
        # would have to fail for this to matter is the same one `get_allowlist()` reads two lines
        # down, so a real failure already degrades the gate; this must not additionally weaponise
        # it. PRD §14's "fail closed for sensitive operations" governs the capability decision in
        # workspace_roles, which is where a refusal protects something.
        swallowed("core.is_suspended: reading the people records failed")
        return False


def email_allowed(email: str) -> bool:
    """True if an email may use the app: the protected owner (or an additional admin), the runtime
    test-user list managed from Settings → Test users, or an allowed domain. ACP_ALLOWED_EMAILS is a
    one-time SEED for that list (see seed_allowlist_once), NOT a separate permanent grant
    — so a user removed from the list is genuinely revoked.

    SUSPENSION BEATS EVERY GRANT BELOW THE OWNER, and that ordering is the fix of 2026-09-05.

    Before it, suspending somebody worked only by accident and only for some people. `PUT
    /admin/people` drops a suspended person from the ALLOWLIST, so an allow-listed person really
    was refused here — not because their suspension was read, but because their grant had been
    deleted. A DOMAIN-admitted person has no allowlist entry to delete, so the final `endswith`
    admitted them exactly as before: the owner clicked Suspend, the screen said suspended, and
    nothing whatsoever happened.

    That mattered most in the configuration this product actually ships in. Below the `navigation`
    rung — `off` and `observe`, and `off` is the default — `workspace_roles.access_for_email`
    returns `legacy_access()` without consulting `is_suspended` at all, because not-yet-enforcing
    means "preserve current access" for everyone. So the RBAC layer's own suspension check, which
    is correct, does not bite until the rollout reaches `navigation`. Measured on the store:
    a suspended domain user resolved to 10 tabs and 22 capabilities at `off` and at `observe`, and
    to 0 and 0 at `navigation` and `enforce`. Until the ladder is climbed, THIS is the only place
    a suspension can take effect, which is why it belongs at the perimeter rather than beside the
    other checks.

    The owner is exempt and stays first, before any store read can fail or refuse. `update_person`
    already refuses to modify OWNER_EMAIL (409), so a suspended owner is not a state the product
    can reach — but the ordering is what makes that a guarantee rather than a coincidence, and an
    owner locked out is the one failure with no recovery path.

    Env admins (ACP_ADMIN_EMAILS) are NOT exempt, which is a deliberate change from "admins are
    always admitted". Suspension is only ever set by `PUT /admin/people`, which is owner-gated, so
    it is always a deliberate act by the one identity that can also undo it. Leaving env admins
    admitted would have preserved precisely the silent no-op this function is being fixed to stop
    — and `update_person` already strips a suspended person from the managed admin list, so the
    two halves of "admin" would otherwise disagree about the same click.
    """
    email = (email or "").lower()
    if email and email == OWNER_EMAIL:
        return True
    if is_suspended(email):
        return False
    if email and email in ADMIN_EMAILS:
        return True
    try:
        if email in get_store().get_allowlist():
            return True
    except Exception:
        swallowed("core.email_allowed: reading the e-mail allowlist failed")
    return any(email.endswith("@" + d.lower()) for d in ALLOWED_DOMAINS)


def people_with_access() -> list[dict]:
    """Everyone the People screen lists, merged from the three places access is actually granted.

    ACCESS AND A `people` ROW ARE NOT THE SAME THING, and that is the whole reason this exists.
    A person reaches the People screen by any of three routes:

      * a `people` record — added through Settings, carrying a provider and an invite status
      * the ALLOWLIST alone — seeded from ACP_ALLOWED_EMAILS or added before the people table
        existed. They can sign in; there is no record describing them. The screen shows them as
        "Provider not recorded".
      * being the protected OWNER_EMAIL, who is always present whether stored or not

    Only the first of those puts a row in `store.get_people()`. Anything that asks "does this
    person exist?" by reading that table alone therefore disagrees with the screen the
    administrator is looking at — it says no to somebody whose name is on the list, in a row with
    working controls beside it.

    That is not hypothetical. `assign_person_role` did exactly that and answered
    `404 person not found` when an administrator used the role dropdown on an allowlist-only
    person, on the People screen that had just rendered them. The list and the write have to
    answer from the same set, so they both come here.

    NOT the same question as `email_allowed`, which additionally admits any address under an
    allowed DOMAIN. Domain-wide users can sign in without appearing here, deliberately: this is
    the enumerable roster the People screen manages, not the perimeter.
    """
    st = get_store()
    records = {r["email"]: r for r in st.get_people()}
    admins = set(st.get_admins()) | set(ADMIN_EMAILS)
    for email in st.get_allowlist():
        records.setdefault(email, {"email": email, "provider": None, "status": "access_ready",
                                   "role": "admin" if email in admins else "user"})
    if OWNER_EMAIL:
        records[OWNER_EMAIL] = {**records.get(OWNER_EMAIL, {}),
                                "email": OWNER_EMAIL, "status": "active",
                                "role": "owner", "protected": True}
    return sorted(records.values(), key=lambda r: r["email"])


def note_signed_in(email: str | None, provider: str | None = None) -> dict | None:
    """Put a person on the roster the first time they sign in. Returns the record, or None.

    THE GAP THIS CLOSES. `email_allowed` admits three kinds of identity: the owner, an allow-listed
    address, and anyone under `ACP_ALLOWED_DOMAINS`. Only the first two are ENUMERABLE — a domain
    is a rule, not a list — so a domain-admitted user could sign in and use the product while
    appearing nowhere an administrator could see them, let alone narrow them. There was no way to
    give one of them a role, because the People screen had no row to put the dropdown on.

    Worse than a dead end, and this is the part worth stating plainly: they were not blocked, they
    were silently ELEVATED. `workspace_roles._enforced_decision` hands an unassigned signed-in user
    the default Platform User role — every workflow tab at Operate — so setting one environment
    variable was, in effect, granting the whole company operator access to the workspace. The owner
    decision of 2026-09-05 reverses that for people who arrive this way: authentication is what the
    domain buys; privileges are assigned.

    WHAT IT DELIBERATELY DOES NOT DO:

      * It does not touch the ALLOWLIST. A record is a record; the allowlist is a GRANT that
        outlives the domain rule. Adding domain users to it would quietly convert "everyone at
        acme.com may sign in, until we say otherwise" into a permanent per-person entitlement that
        survives removing the domain — the opposite of the revocation an administrator expects.
      * It does not enumerate the directory. Only people who actually sign in get a row, which is
        also why this is cheap: the roster grows to the size of the team using ACP, not the size of
        the company.
      * It does not touch anybody already on the roster, so an administrator's decisions are never
        re-defaulted by a later sign-in. Existing users are backfilled lazily and exactly once.
    """
    who = (email or "").strip().lower()
    if not who or "@" not in who:
        return None
    if who in _rostered:
        return None
    if person_with_access(who) is not None:
        _rostered.add(who)          # already known — nothing to create, and never ask again
        return None

    with _roster_lock:
        if who in _rostered:
            return None
        # Re-read inside the lock. Two workers racing on one person's first request would
        # otherwise both build a record, and upsert_person is a read-modify-write over the whole
        # people blob — the second would be writing over a list it read before the first landed.
        if person_with_access(who) is not None:
            _rostered.add(who)
            return None

        from datetime import datetime, timezone

        import workspace_roles as wr

        st = get_store()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        via = "allowlist" if who in set(st.get_allowlist()) | ALLOWED_EMAILS else "domain"
        record = st.upsert_person({
            "email": who,
            "provider": (provider or "").strip().lower() or None,
            "status": "access_ready" if DEFAULT_SIGNIN_ROLE else "pending",
            "role": "user",
            "admitted_via": via,
            "first_signed_in_at": now,
        })
        # PRD §12 wants the ACTOR on every consequential change. Nobody clicked this one, and
        # saying so is more useful than attributing it to the person themselves — which would read
        # in the log as though they had granted it to themselves.
        st.log_decision("system", "person.first_sign_in",
                        detail=f"{who} · admitted via {via} · "
                               f"{('default role ' + DEFAULT_SIGNIN_ROLE) if DEFAULT_SIGNIN_ROLE else 'access pending'}")
        if DEFAULT_SIGNIN_ROLE:
            # Through assign_role, so the role.assigned audit row and the assigned-by/assigned-at
            # fields are written by the same code path an administrator's assignment uses. A
            # second way to set a role is a second way for the audit trail to be incomplete.
            record = wr.assign_role(st, email=who, role_id=DEFAULT_SIGNIN_ROLE,
                                    actor="system:first-sign-in")
        _rostered.add(who)
        return record


def forget_rostered(email: str | None = None) -> None:
    """Drop the process-local memo, so the next sign-in reconciles against the store again.

    Needed wherever a person is REMOVED — otherwise this process would remember them as rostered
    and never rebuild the record, and they would be back to invisible after their next sign-in.
    """
    if email is None:
        _rostered.clear()
        return
    _rostered.discard((email or "").strip().lower())


def person_with_access(email: str | None) -> dict | None:
    """One person from `people_with_access`, or None. Lower-cased and stripped, because that is
    how every caller receives an address off the wire."""
    target = (email or "").strip().lower()
    if not target:
        return None
    return next((p for p in people_with_access() if p.get("email") == target), None)


def is_scope_owner(email: str | None) -> bool:
    """May this identity edit the scan scope? Scope writes go through PUT /settings, which is
    admin-only (_require_admin), so this is the same gate the API enforces — it exists so the SPA can
    hide the scope editor for non-admins POST-auth instead of letting a non-admin attempt a write
    that the server will 403.

    Delegates to is_admin(), so additional Platform Admins (ACP_ADMIN_EMAILS) — not just the single
    owner — get the editor. Name kept for SPA/`/me` compatibility. True when no owner is configured
    (local dev / demo / no-auth — _require_admin is a no-op there, so everyone may edit)."""
    return is_admin(email)


def seed_allowlist_once(st: Store) -> None:
    """One-time bootstrap: copy ACP_ALLOWED_EMAILS into the editable runtime list so
    pre-existing users appear in Settings → Test users and can be removed. Guarded by a
    marker so it never resurrects a user the admin later deletes.

    Takes the Store explicitly: it runs from inside get_store(), before the singleton is
    published, so it cannot reach it through the module attribute."""
    try:
        if st.get_setting("allowlist_seeded") == "true":
            return
        if ALLOWED_EMAILS:
            st.set_allowlist(sorted(set(st.get_allowlist()) | ALLOWED_EMAILS))
        st.set_setting("allowlist_seeded", "true")
    except Exception:
        swallowed("core.seed_allowlist_once: seeding the allowlist once failed")
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]
# Same knob and same reasoning as scanner._DRIVE_HTTP_TIMEOUT_S — shared here rather than
# imported from scanner because core.py must not depend on it (scanner already imports core-
# adjacent modules; core stays the lower layer). Kept as one env var name across both so a
# deploy-time override covers both call sites with a single setting.
_DRIVE_HTTP_TIMEOUT_S = int(os.environ.get("ACP_DRIVE_HTTP_TIMEOUT_S", "60"))
HITL_WEBHOOK = os.environ.get("HITL_WEBHOOK_URL", "")

# ── Shared singletons ─────────────────────────────────────────────────────────
# `store` is built on FIRST USE, never at import. Store() opens — and creates — the database
# in its constructor, so a module-level `store = Store()` meant that merely *importing* core
# touched the disk: a CLI script, a test module collected by pytest, a tool reading the route
# table. Worse, whatever store._SQLITE_PATH happened to say at that instant was frozen into
# core.store for the life of the process, so anything that repointed the path afterwards was
# silently talking to a different database than core was.
#
# Access it as `core.store` (PEP 562 __getattr__ below) or `core.get_store()`. Both resolve to
# the same object; neither runs until someone actually needs a database.
_store_lock = threading.Lock()


def get_store() -> Store:
    """The process-wide Store, constructed on first use.

    Resolves through the module's own `store` attribute rather than a private slot, so a test
    doing `monkeypatch.setattr(core, "store", fake)` is honoured by core's *internal* callers
    too (finalize_scan, the scheduled scan, the job workers) — not just by the modules that
    read `core.store` from outside.
    """
    st = globals().get("store")
    if st is None:
        with _store_lock:
            st = globals().get("store")          # another thread may have won the race
            if st is None:
                st = Store()
                globals()["store"] = st          # publish before seeding: seeding uses `st`
                seed_allowlist_once(st)
    return st


def __getattr__(name: str):
    """PEP 562: only consulted when normal attribute lookup fails. Once get_store() publishes
    `store` into the module globals (or a test monkeypatches it), this stops being called."""
    if name == "store":
        return get_store()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


JOBS: dict[str, dict] = {}


# ── The active rubric ─────────────────────────────────────────────────────────
# IT LIVES IN THE DATABASE, AND IT USED TO LIVE IN THE CONTAINER. `PUT /rubric` wrote
# `config/rubric.active.json` inside whichever API replica served the request, and this function
# read that same path. Nothing mounts a volume there, so it was one container's ephemeral layer:
#
#   - other API replicas kept the previous rubric (standard-production's floor is two);
#   - EVERY WORKER CONTAINER kept it too, and workers are where scoring happens — the handlers
#     stamp `rubric_hash` per file (handlers.py) and no worker ever receives the PUT, so a change
#     was invisible to the tier that applies it even with a single API replica;
#   - it was lost on restart or redeploy.
#
# `rubric_hash` is recorded against every scanned file, so replicas under different policies also
# recorded different hashes for the same configuration. The endpoint calls this "the GLOBAL
# scoring policy" and is owner-only precisely because it decides how every tenant is scored, so
# none of that was defensible. It affected Compose and Container Apps as much as Kubernetes —
# every deployment runs the worker as a separate container.
_RUBRIC_SETTING = "rubric_active"

# CACHED, BECAUSE THE CALLERS ARE PER-FILE. `_scan_file` and `_workspace_scan_file` are job
# handlers — one job per document — so a 986-file scan called this ~1000 times. Reading a file
# ~1000 times was free; a database round trip ~1000 times is not. The TTL bounds how stale a
# worker's copy can be after a PUT: seconds, against the "never, until redeploy" this replaces.
#
# No lock. Two threads may both load and one assignment wins; the loser did redundant work and
# neither can observe a torn value, because the tuple is replaced wholesale.
_RUBRIC_CACHE_S = float(os.environ.get("ACP_RUBRIC_CACHE_S") or 5)
_rubric_cache: tuple[Rubric, float] | None = None


def _load_rubric() -> Rubric:
    """The setting if there is one, else the files — which is also the upgrade path.

    An installation that has a `rubric.active.json` from before this change keeps being scored by
    it until someone PUTs, rather than silently reverting to the defaults on deploy. Nothing is
    migrated into the database from it: that file is per-container and ephemeral, so there is no
    single copy to promote and picking one replica's would be arbitrary.
    """
    raw = None
    try:
        raw = get_store().get_setting(_RUBRIC_SETTING)
    except Exception:
        # The store is unreachable, or the schema predates app_settings. Scoring must not stop
        # because a setting could not be read; the files below are what it did before.
        raw = None
    if raw:
        try:
            return Rubric(json.loads(raw))
        except Exception:
            # A corrupt setting must not 500 every scan in the fleet. Fall back, loudly.
            print(f"[rubric] {_RUBRIC_SETTING} is not valid rubric JSON; using the file rubric",
                  file=sys.stderr, flush=True)
    return Rubric.load_active(ACP / "config")


def active_rubric(*, fresh: bool = False) -> Rubric:
    """The rubric in force. `fresh=True` skips the cache — for writers, and for anything that
    reports what is in force rather than scoring with it."""
    global _rubric_cache
    if not fresh:
        cached = _rubric_cache
        if cached is not None and _time.monotonic() < cached[1]:
            return cached[0]
    rubric = _load_rubric()
    _rubric_cache = (rubric, _time.monotonic() + _RUBRIC_CACHE_S)
    return rubric


def invalidate_rubric_cache() -> None:
    """Called by the writer so the replica that served the PUT answers from the new rubric. The
    others catch up within the TTL; nothing has to be broadcast."""
    global _rubric_cache
    _rubric_cache = None


# ── GIS token verification (cached) ───────────────────────────────────────────
# token → (email, monotonic_expiry). Tokens live 1h; we cache 9 min.
_gis_cache: dict[str, tuple[str, float]] = {}


def verify_gis_token(token: str) -> str | None:
    now = _time.monotonic()
    cached = _gis_cache.get(token)
    if cached:
        email, exp = cached
        if now < exp:
            return email
        del _gis_cache[token]
    import urllib.request as _ur
    import json as _json
    try:
        with _ur.urlopen(
            f"https://www.googleapis.com/oauth2/v1/tokeninfo?access_token={token}",
            timeout=5,
        ) as r:
            data = _json.load(r)
    except Exception:
        return None
    if "error" in data:
        return None
    email = data.get("email", "")
    _gis_cache[token] = (email, now + 540)
    return email


# ── Microsoft (Entra) token verification (cached) ─────────────────────────────
# token → (email, monotonic_expiry). Same posture and cache window as GIS above.
#
# #239 added a "Sign in with Microsoft" button but never taught this backend to authenticate the
# resulting user — the access gate only accepted Google tokens, so every Microsoft sign-in 401'd
# the moment the SPA made its first call ("session expired", immediately). This closes that gap.
#
# We verify the SAME way the Google path does — by asking the provider rather than validating a JWT
# locally: call Microsoft Graph /me with the delegated access token MSAL already holds (it carries
# User.Read). A 200 proves the token is a live Microsoft token for a real user, and hands back the
# identity; email_allowed() then decides access exactly as for Google. This matches the existing
# security model (valid provider token + allow-listed identity) rather than adding a stricter,
# audience-pinned JWKS check that the Google lane does not have either — if we tighten one, we
# tighten both, deliberately and together.
_ms_cache: dict[str, tuple[str, float]] = {}


def _graph_me_email(token: str) -> str | None:
    """GET https://graph.microsoft.com/v1.0/me and return the user's email/UPN, or None. Split out
    so tests can substitute the network call without patching urllib."""
    import urllib.request as _ur
    import json as _json
    req = _ur.Request(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with _ur.urlopen(req, timeout=5) as r:
            data = _json.load(r)
    except Exception:
        return None
    # `mail` is the routable address; `userPrincipalName` is the sign-in name and the reliable
    # fallback when a mailbox isn't provisioned (common on dev-tenant test accounts).
    email = (data.get("mail") or data.get("userPrincipalName") or "").strip()
    return email or None


def verify_ms_token(token: str) -> str | None:
    now = _time.monotonic()
    cached = _ms_cache.get(token)
    if cached:
        email, exp = cached
        if now < exp:
            return email
        del _ms_cache[token]
    email = _graph_me_email(token)
    if not email:
        return None
    _ms_cache[token] = (email, now + 540)
    return email


# ── Access-gate path policy ───────────────────────────────────────────────────
# Paths that bypass all auth (needed before the user has a token).
ALWAYS_PUBLIC = {"/healthz", "/readyz", "/config", "/hub", "/ai/status", "/alerts/webhook",
                 "/capability", "/monitor/estate",
                 # The container-local readiness probe (routes/system.py). Public because the
                 # platform's probe carries no credential and never will — a rollout gate that
                 # needed one would fail every replica it was meant to admit. It returns a
                 # boolean and a fault class, no data.
                 "/probe/readyz",
                 # The health/heartbeat Swagger document (routes/openapi_health.py) — a curated,
                 # non-sensitive description of the endpoints above plus the owner-scoped
                 # progress/heartbeat routes. Public on purpose: the whole point is a monitor or
                 # integrator can read it without signing in. See that module's own docstring for
                 # why this is a separate document rather than making FastAPI's own /docs public.
                 "/openapi/health.json", "/docs/health"}
# Shared secret for the Grafana alert webhook (public path, key-validated).
ALERT_KEY = os.environ.get("ACP_ALERT_KEY", "acp-alert-demo-key")

# A single freshness contract for emitters, API aggregation, UI labels and tests. Two missed
# 15-second beats make a process stale by default; deployments may tune it without code changes.
try:
    WORKER_INSTANCE_FRESHNESS_SECONDS = max(
        1, int(os.environ.get("ACP_WORKER_INSTANCE_FRESHNESS_SECONDS", "30")))
except ValueError:
    WORKER_INSTANCE_FRESHNESS_SECONDS = 30
# Shared secret for the production monitor's aggregate endpoint (public path, key-validated —
# the same posture as ALERT_KEY above, and deliberately NOT the X-E2E-Key gate bypass).
#
# The monitor's deep checks used to authenticate with X-E2E-Key, which cannot work in
# production BY DESIGN: E2E_KEY is None whenever IS_PROD, so the header the monitor sent was
# never going to be honoured on the one deployment worth monitoring. Setting ACP_E2E_KEY would
# not have fixed it; enabling ACP_ENABLE_TEST_BYPASS would have "fixed" it by reopening the
# whole-gate backdoor that fail-closed default exists to keep shut.
#
# So this key unlocks exactly one read-only route that returns COUNTS — no filenames, no
# owners, no document content. NO DEFAULT VALUE: a monitoring credential that ships with a
# well-known fallback is a backdoor, so an unset key disables the route rather than opening it.
MONITOR_KEY = os.environ.get("ACP_MONITOR_KEY") or None
# FAIL-CLOSED gate (2026-08-22, replacing a fail-open manually-maintained allowlist).
#
# The previous design was a hand-maintained tuple of "protected" prefixes (API_PREFIXES) with
# everything else served unauthenticated by default — the natural default for a small surface,
# but one that silently missed FIVE separate route groups over five weeks as the app grew
# (/campaigns, /disposition, then /assess, /analytics, /control, /org-memory, /scope,
# /sharepoint — the last including an unauthenticated file-upload endpoint). Each shipped
# unauthenticated not because anyone decided it should be public, but because nobody remembered
# to extend a list a mile from the route definition. tests/test_auth_route_coverage.py is the
# guard that should have caught this and, for a stretch, silently couldn't (see that file's own
# history) — a second line of defense is not a substitute for removing the failure mode itself.
#
# This checks the REAL, REGISTERED route table instead: any path FastAPI would actually dispatch
# to requires auth by default, unless explicitly listed in ALWAYS_PUBLIC (or the one path-pattern
# carve-out below). A new APIRouter is protected automatically the moment it's included — there
# is no list left to forget.
#
# `_PROTECTED_ROUTES` is populated two ways, both safe against the app.py/core.py import order
# (app.py imports core, not the reverse — core.py cannot import app.py at ITS OWN module level
# without creating a cycle):
#   1. Explicitly: app.py calls register_protected_routes() at module level, immediately after
#      every router is included — the normal path in the real running app.
#   2. Lazily: if is_public() is ever reached before that (a test that imports core directly
#      without going through app.py first, or a future startup-ordering change), _protected_routes()
#      below imports `app` itself — a LOCAL import, deferred until first actual use, which is safe
#      specifically because is_public() is only ever called from inside the request-handling
#      middleware, never at either module's IMPORT time. By the time any request is served,
#      app.py has already finished executing top to bottom and sits fully built in sys.modules,
#      so this local import is a cache hit, not a re-execution — no cycle, regardless of which
#      module happened to import first.
_PROTECTED_ROUTES: list | None = None


def register_protected_routes(routes) -> None:
    """Called by app.py at module level, immediately after `for _router in ROUTERS:
    app.include_router(_router)` completes. Stores the real APIRoute objects so is_public() can
    match a live request path against them via Starlette's own Route.matches() — not a
    hand-rolled startswith(), which is exactly the kind of parallel, driftable string-matching
    scheme this redesign exists to avoid. Parameterized routes (/scans/{sid}/trace/{kind},
    /scope/rules/{rule_id}, .../{id:path}) only match correctly through the framework's own
    path-compilation; a prefix/startswith scheme cannot see path parameters at all.

    Also the override a test reaches for directly, to check is_public()'s MATCHING logic against
    a small synthetic route set without booting the whole real app."""
    global _PROTECTED_ROUTES
    _PROTECTED_ROUTES = list(routes)


def _protected_routes() -> list:
    """The route list is_public() matches against — registered explicitly (the normal path) or,
    failing that, imported lazily on first use. See the module comment above for why the lazy
    import is safe here specifically (deferred past both modules' own import time)."""
    global _PROTECTED_ROUTES
    if _PROTECTED_ROUTES is None:
        from app import app as _fastapi_app
        _PROTECTED_ROUTES = enumerate_api_routes(_fastapi_app)
    return _PROTECTED_ROUTES


def enumerate_api_routes(app) -> list:
    """Enumerate effective HTTP API endpoints, including nested router prefixes.

    FastAPI's lazy included-router candidates carry the final path and matcher.
    Reading original_router.routes alone loses nested inclusions and prefixes;
    these same effective routes must drive authentication and capability audits.
    Older FastAPI versions already expose flat APIRoute objects.
    """
    from fastapi.routing import APIRoute

    def expand(route):
        if isinstance(route, APIRoute):
            return [route]
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            return [leaf for child in candidates() for leaf in expand(child)]
        if isinstance(getattr(route, "original_route", None), APIRoute):
            return [route]
        router = getattr(route, "original_router", route)
        return [leaf for child in getattr(router, "routes", ()) for leaf in expand(child)]

    return [leaf for route in app.routes for leaf in expand(route)]


def _matches_a_protected_route(path: str) -> bool:
    """True iff `path` matches ANY registered API route's pattern, by method or not — even a
    request with the wrong HTTP method for a real endpoint (Match.PARTIAL) is still a real API
    path and must be gated, not waved through because the verb didn't line up. Only Match.NONE
    against every registered route means "not a recognized backend path at all" — the SPA's own
    client-side routes and static assets, which is the only thing this now falls through for.

    Builds the minimal ASGI scope Route.matches() actually reads (type/path/method/path_params —
    verified against the installed Starlette's own Route.matches source) rather than requiring
    callers to construct a full request scope; the method is a placeholder since it never affects
    whether NONE vs. PARTIAL/FULL is returned for THIS gate's purposes."""
    from starlette.routing import Match
    scope = {"type": "http", "path": path, "method": "GET", "path_params": {}}
    for route in _protected_routes():
        match, _ = route.matches(scope)
        if match != Match.NONE:
            return True
    return False


def match_registered_route(path: str, method: str):
    """The registered APIRoute this request will actually dispatch to, or None.

    Sibling of _matches_a_protected_route above, and separate from it because the two want
    different answers. That one asks "is this a real API path at all", so a method mismatch still
    counts. This one is used to look a route up in the capability map (PRD §11), where the METHOD
    is half the key — GET /admin/roles and DELETE /admin/roles/{id} are different permissions —
    so only a FULL match will do. A PARTIAL match (right path, wrong verb) resolves to no route
    here, which is correct: FastAPI will answer 405, and there is no capability to check on a
    request that reaches no endpoint.

    Returns the route object rather than its path so callers get the PATTERN (`/scans/{sid}`),
    not the concrete path (`/scans/abc123`) — the map is keyed on patterns, and keying it on
    concrete paths would mean a table with one row per scan.
    """
    from starlette.routing import Match
    scope = {"type": "http", "path": path, "method": (method or "GET").upper(),
             "path_params": {}}
    for route in _protected_routes():
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


import re as _re

# The Langfuse trace routes in routes/scans.py, all documented "Public" — the redirect targets
# (/scans/{sid}/trace/{kind}, /trace/session, /trace/file/{filename:path}) are plain <a>
# navigations with no auth header, and their /exists, /data, /history siblings are read by the
# same panels. ANCHORED, by shape: `trace` must be the segment right after the scan id. This was
# `path.startswith("/scans/") and "/trace/" in path`, a substring test, so a document inside a
# folder named `trace` opened EVERY /scans/{sid}/files/{filename:path}/... route (and
# /scans/{sid}/decisions/{filename:path}, a PUT) to anonymous callers. Singular "trace" only —
# the authed /scans/{sid}/traces JSON endpoint does not match.
_PUBLIC_TRACE_ROUTE = _re.compile(
    r"^/scans/[^/]+/trace/(?:session(?:/data)?|[^/]+(?:/exists)?|file/.+)$")
_TRACE_ROUTE_TEMPLATE_PREFIX = "/scans/{sid}/trace/"


def _is_public_trace_path(path: str) -> bool:
    """The shape alone is not enough: /scans/jobs/trace/stream has it, and the router dispatches
    it to GET /scans/jobs/{job_id}/stream (registered earlier), not to a trace route. So a path is
    a public trace path only if EVERY registered route whose pattern matches it — any method, since
    the gate does not know which one the router will pick — is itself a trace route."""
    if not _PUBLIC_TRACE_ROUTE.match(path):
        return False
    from starlette.routing import Match
    scope = {"type": "http", "path": path, "method": "GET", "path_params": {}}
    return all(route.path.startswith(_TRACE_ROUTE_TEMPLATE_PREFIX)
               for route in _protected_routes()
               if route.matches(scope)[0] != Match.NONE)


def is_public(path: str) -> bool:
    if path in ALWAYS_PUBLIC:
        return True
    if _is_public_trace_path(path):
        return True
    # R15 verify-this-report endpoint: anyone can check a scan's digest without signing in.
    if path.startswith("/public/"):
        return True
    if _matches_a_protected_route(path):
        return False
    return True


# ── Drive client factory ──────────────────────────────────────────────────────
def drive_service(request=None):
    """Drive client for the request. A per-user GIS token (X-Drive-Token) scans that
    user's Drive; otherwise ADC (demo identity). In GIS mode a token is required."""
    from fastapi import HTTPException
    from googleapiclient.discovery import build
    token = request.headers.get("x-drive-token") if request is not None else None
    if token:
        from google.oauth2.credentials import Credentials
        # GIS tokens are short-lived (1 h) and carry no refresh_token, so leave `expiry` None:
        # google-auth reports `expired` False for a credential without an expiry and never
        # attempts a refresh. Drive answers 401 if the token really has expired — the honest
        # signal. Setting `expiry = now + 1h` (as this did, while claiming the opposite) invited
        # google-auth to refresh the instant that hour lapsed, raising "credentials do not
        # contain the necessary fields need to refresh the access token".
        creds = Credentials(token=token, scopes=DRIVE_SCOPES)
    elif GOOGLE_CLIENT_ID:
        raise HTTPException(401, "sign in with Google to connect your Drive")
    else:
        import google.auth
        creds, _ = google.auth.default(scopes=DRIVE_SCOPES)
    # See scanner._drive_service's identical fix (found live 2026-08-29) for why this can't stay
    # `credentials=creds`: that shortcut builds its own AuthorizedHttp with no way to bound its
    # socket, so a stalled connection (not just a slow one — one that never returns data or an
    # error) blocks this request thread forever. build() refuses `http=` and `credentials=`
    # together, so the AuthorizedHttp has to be constructed here instead.
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_DRIVE_HTTP_TIMEOUT_S))
    return build("drive", "v3", http=http, cache_discovery=False)


# ── HITL webhook ──────────────────────────────────────────────────────────────
def fire_webhook(items: list[dict], *, event: str = "hitl.queued") -> None:
    """POST HITL events to the configured webhook URL (best-effort, non-blocking).

    Supported events: hitl.queued (new items), hitl.assigned (reviewer set),
    hitl.resolved (approved / rejected / skipped).
    """
    if not HITL_WEBHOOK or not items:
        return
    import threading

    def _post():
        try:
            import httpx
            httpx.post(HITL_WEBHOOK, json={"event": event, "items": items}, timeout=8)
        except Exception as e:
            print(f"HITL webhook failed: {e}", flush=True)
    threading.Thread(target=_post, daemon=True).start()


# ── Langfuse remediation span ─────────────────────────────────────────────────
def emit_remediation_span(scan_id: str, filename: str, drive_write_url: str | None, *,
                          fixes_applied: int | None = None, fixes_skipped: int | None = None,
                          per_rule: dict | None = None):
    """Emit a 'Remediate' span on this file's OWN Langfuse trace (file-centric tracing —
    see lf.file_trace): the same trace that already carries its Discover and (if run)
    Assess spans, so the file's full lifecycle lives in one place.

    fixes_applied/fixes_skipped/per_rule are the deterministic remediation outcome from
    handlers._remediate_file (COUNTS only, PHI-safe). Optional: the manual-upload and
    mark-remediated routes have no fix pass to report and leave them None."""
    try:
        import lf as _lf
        owner = (get_store().get_scan(scan_id) or {}).get("run", {}).get("owner_email")
        trace = _lf.file_trace(scan_id, filename, user=owner)
        _lf.remediate_span(trace, drive_write_url, fixes_applied=fixes_applied,
                           fixes_skipped=fixes_skipped, per_rule=per_rule)
        _lf.flush()
    except Exception:
        swallowed("core.emit_remediation_span: emitting the remediation span failed", scan_id)


# ── Background scheduler (periodic local scans) ───────────────────────────────
# Constructed at import (cheap, no thread), but NOT started: scheduler.start() spawns a
# background thread, and importing a module should not. Every pytest process and every CLI
# script that touched `import core` was running one. app.py starts it from its startup hook
# and shuts it down again on SIGTERM.
#
# add_job()/remove_all_jobs()/get_job() all work on a stopped scheduler — APScheduler holds
# them as pending jobs and runs them once started — so reload_scheduler() may arm it first.
scheduler = BackgroundScheduler()


def start_scheduler() -> bool:
    """Start the background scheduler. Idempotent; returns True if this call started it."""
    if scheduler.running:
        return False
    scheduler.start()
    return True


def stop_scheduler() -> None:
    """Stop the scheduler if running. Safe to call when it never started."""
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _drive_delta_check(cursor_key: str, owner: str | None,
                       build_svc) -> tuple[list[dict], set] | None:
    """The shared core of the Drive delta gate: advance (or seed) the stored cursor at
    `cursor_key` and return (changed_files, removed_ids) from Drive's Changes API, or None
    when there is nothing yet to compare — a first-ever check for this cursor_key, or the
    check itself failed. None is NEVER 'nothing changed': both callers below must fall back
    to a full listing on None, and both do.

    `cursor_key` is what gives the scheduled sweep and an interactive scan two independent
    cursors under one cursor store with no schema change: the sweep's own identity is the
    fixed string "drive" (unchanged since #933), while an interactive caller namespaces its
    key per signed-in user (see _interactive_drive_sync_plan) so two different users' Drive
    accounts never share or clobber each other's delta position.

    `build_svc` is a zero-arg callable returning the Drive service to check against — called
    INSIDE the try below, deferred, so a broken build is covered by the same fallback-to-full-
    listing guarantee as the API calls after it. The scheduled sweep has nothing built yet and
    passes a fresh `lambda: _drive_service(None)` (ADC); an interactive caller already has a
    service built for the very same request it is about to list with and passes THAT one back
    (`lambda: svc`) rather than have this function build a second one from the raw token —
    building a second service per request is both wasteful and, in any caller that mocks
    scanner._drive_service to assert it was called exactly once, a real regression.
    """
    from scanner import drive_changes_since, drive_start_page_token
    cur = get_store().get_sync_cursor(cursor_key)
    try:
        svc = build_svc()
        if not cur or not cur.get("page_token"):
            # No baseline yet for this cursor_key — nothing to compare against. Seed one now
            # so the NEXT check has something to gate on.
            get_store().save_sync_cursor(cursor_key, owner, drive_start_page_token(svc))
            return None
        changed, removed_ids, new_token = drive_changes_since(svc, cur["page_token"])
        get_store().save_sync_cursor(cursor_key, owner, new_token)  # advance regardless of outcome
        return changed, removed_ids
    except BaseException as e:
        # BaseException, deliberately wider than the rest of this codebase's `except
        # Exception`: a broken native dependency in the googleapiclient/google-auth import
        # chain (e.g. a cryptography/cffi ABI mismatch) surfaces as a Rust pyo3_runtime.
        # PanicException, which subclasses BaseException specifically so it is NOT caught by
        # `except Exception` — and this function's whole contract is "never turn uncertainty
        # into an unvouched-for reconstruction," which an uncaught panic here would silently
        # violate by crashing the caller instead of falling back to it. An expired/invalid
        # page token (Drive 404s that after ~1 week unused) and ordinary API/ADC failures
        # fall through here too.
        print(f"drive sync check ({cursor_key}): change-check failed ({e}) — falling back to "
              f"a full listing", flush=True)
        return None


def _whole_drive_prior_inventory_for_account(
        owner: str | None, account_id: str | None) -> tuple[str, list[dict]] | None:
    """Return the newest trustworthy whole-Drive inventory for this Google account.

    A Drive delta describes changes to the account-wide corpus.  It can therefore only be
    applied to an account-wide baseline.  In particular, the newest completed Drive scan may
    be a folder run; using that inventory as the baseline manufactures a tiny result and then
    labels it ``kind=drive``.  Production did exactly that on 2026-09-03 (986-file whole Drive,
    then a 37-file folder run, then a "whole Drive" reconstruction containing 37 files).

    A Discovery-only run is deliberately not used for delta reconstruction yet: the existing
    inventory reader is tied to ``completed_at``.  Falling back to a fresh listing is slower but
    safe; pretending an older completed folder inventory belongs to that newer run is not.
    """
    if not owner:
        return None
    store = get_store()
    try:
        candidates = [r for r in store.list_finished_scans(owner)
                      if r.get("source") == "drive"]
    except AttributeError:
        # Compatibility for small store doubles and older deployments during a rolling update.
        # Production Store implements list_finished_scans; the conservative path there is the
        # scope-aware loop below.
        prior = store.latest_scan_inventory_items(owner, "drive")
        if prior is None or any(r.get("drive_account_id") != account_id for r in prior):
            return None
        return "legacy-unknown-scope", prior

    if not candidates:
        return None
    # Load-bearing: inspect the NEWEST finished Drive boundary; never skip past a newer folder
    # run and silently pair an old whole-Drive scope with some other inventory.
    run = candidates[0]
    scope = run.get("scope") if isinstance(run.get("scope"), dict) else {}
    enumeration = scope.get("enumeration") or {}
    if (scope.get("kind") != "drive" or not run.get("completed_at")
            or not enumeration.get("complete") or scope.get("truncated")):
        return None
    prior = store.latest_scan_inventory_items(owner, "drive")
    if prior is None or any(r.get("drive_account_id") != account_id for r in prior):
        return None
    return run["id"], prior


def _drive_prior_inventory_for_account(owner: str | None, account_id: str | None) -> list[dict] | None:
    """The most recent completed Drive scan's inventory, but ONLY if it was actually run as
    THIS Google account. store.latest_scan_inventory_items has no account-scoped query of its
    own — it returns whatever the most recent 'drive'-source scan for this owner covered,
    regardless of which Google identity a per-request drive_token authenticated as.

    A Drive token is a per-request browser OAuth credential, never a server-bound "connected
    account", so nothing else stops the same ACP owner email presenting a DIFFERENT Google
    account between scans — the same signed-in ACP user can sign into a different Google
    account in the browser's Drive picker from one interactive scan to the next. Reconstructing
    one account's estate from another's inventory would silently show the wrong documents, so
    an account_id mismatch on ANY row is treated the same as 'no prior scan to reconstruct
    from' — never a partial or approximate match, matching _sp_prior_inventory_for_drive's
    identical contract for SharePoint. An empty-but-real prior scan is not a mismatch and still
    returns (as `[]`, not None).

    `account_id` can itself be None (scanner.drive_account_id's own best-effort failure mode) —
    that still participates in the comparison rather than skipping it: None only matches other
    Nones, so an unverifiable CURRENT identity checked against a KNOWN prior one is correctly
    treated as a mismatch, never a silent pass."""
    match = _whole_drive_prior_inventory_for_account(owner, account_id)
    return match[1] if match else None


def _drive_sync_plan(owner: str | None, cursor_key: str = "drive") -> tuple[bool, dict | None]:
    """PRD Phase 3: decide what the scheduled Drive sweep should do, from the stored sync
    cursor. Returns (skip, drive_delta):

      - (True, None)    — a stored cursor confirms nothing changed since the last sweep; skip
                          the scan entirely.
      - (False, {...})  — something changed; {"prior_files", "changed", "removed_ids"} for
                          run_scan's drive_delta= to reconstruct the estate from, without
                          walking Drive.
      - (False, None)   — proceed with TODAY's full, fresh Drive listing: first sweep ever, no
                          prior completed scan to reconstruct from, or the change-check itself
                          failed.

    Uncertainty always resolves to (False, None) — a full, already-correct scan — never to a
    skip, and never to a reconstruction this function can't vouch for. A SKIP is the right
    answer for THIS caller specifically because nobody is waiting on a scheduled sweep's
    result — see _interactive_drive_sync_plan below for why an interactive scan can never use
    the same shortcut.

    The reconstruction baseline is also verified against the CURRENT Google account
    (_drive_prior_inventory_for_account) before it is trusted — see that function's docstring
    for why a Drive token is not guaranteed to be the same identity from one sweep to the
    next (an operator-rotated ADC credential, in this caller's case)."""
    from scanner import _drive_service, drive_account_id
    result = _drive_delta_check(cursor_key, owner, lambda: _drive_service(None))
    if result is None:
        return False, None
    changed, removed_ids = result
    if not changed and not removed_ids:
        return True, None
    try:
        account_id = drive_account_id(_drive_service(None))
    except Exception:
        account_id = None
    prior = _drive_prior_inventory_for_account(owner, account_id)
    if prior is None:
        # A cursor exists but no completed scan does (e.g. every prior sweep since it was
        # seeded has failed before saving, or the most recent one ran as a different Google
        # account) — nothing to reconstruct FROM. A full listing gives the NEXT sweep a real
        # baseline again.
        print(f"scheduled drive sweep: {len(changed)} changed, {len(removed_ids)} removed/"
              f"trashed, but no prior scan to reconstruct from — running a full scan",
              flush=True)
        return False, None
    print(f"scheduled drive sweep: {len(changed)} changed, {len(removed_ids)} removed/"
          f"trashed since last sync — reconstructing the estate instead of a full re-list",
          flush=True)
    from scanner import _drive_file_from_inventory_row
    prior_files = [_drive_file_from_inventory_row(r) for r in prior]
    return False, {"prior_files": prior_files, "changed": changed, "removed_ids": removed_ids}


def _interactive_drive_sync_plan(owner: str, svc) -> dict | None:
    """PRD Phase 3, interactive scans: the same drive_delta reconstruction _drive_sync_plan
    gives the scheduled sweep, but for a user-initiated whole-Drive scan — keyed per SIGNED-
    IN USER (cursor_key=f"drive:{owner}"). `svc` is the Drive service the CALLER already built
    for this same request (from this user's own per-request token, never ADC) — reused here
    rather than built a second time (see _drive_delta_check's docstring for why).

    Returns the SAME drive_delta dict _list()'s drive_delta= already knows how to reconstruct
    from (see drive_reconstructed_listing), or None to fall back to a normal full listing.

    DELIBERATELY NO SKIP, unlike _drive_sync_plan. The scheduled sweep's "nothing changed —
    do nothing" is only correct because nobody is watching it; a user who clicked "scan"
    is watching, and is owed a completed scan every time, even when nothing changed. So
    'nothing changed' here still returns a drive_delta — {"changed": [], "removed_ids":
    set()} reconstructs the prior scan's inventory UNCHANGED, which is a genuine, complete,
    just very fast scan rather than the absence of one.

    Callers restrict this to a whole-Drive request (no folder/folders narrowing) before
    calling — see handlers._scan_discover. Drive's Changes API has no folder filter, so
    reconciling it against a folder-scoped baseline would need a containment check this
    function does not do.

    The prior-scan baseline is also verified against the CURRENT Google account
    (_drive_prior_inventory_for_account) before it is trusted — unlike the scheduled sweep's
    fixed ADC identity, a signed-in ACP user can sign into a DIFFERENT Google account in the
    browser's Drive picker from one interactive scan to the next, and reconstructing from the
    wrong account's inventory would silently show the wrong documents.
    """
    from scanner import drive_account_id
    result = _drive_delta_check(f"drive:{owner}", owner, lambda: svc)
    if result is None:
        return None
    changed, removed_ids = result
    prior = _drive_prior_inventory_for_account(owner, drive_account_id(svc))
    if prior is None:
        # A cursor exists but no completed whole-Drive scan for this user does (their first
        # scan predates this cursor, every prior one failed before saving, or the most recent
        # one was a different Google account) — nothing to reconstruct FROM. A full listing
        # gives the NEXT interactive scan a real baseline.
        return None
    if changed or removed_ids:
        print(f"interactive drive scan ({owner}): {len(changed)} changed, {len(removed_ids)} "
              f"removed/trashed since last sync — reconstructing the estate instead of a full "
              f"re-list", flush=True)
    from scanner import _drive_file_from_inventory_row
    prior_files = [_drive_file_from_inventory_row(r) for r in prior]
    return {"prior_files": prior_files, "changed": changed, "removed_ids": removed_ids}


def _sp_delta_check(cursor_key: str, owner: str | None, token: str,
                    drive_id: str | None) -> tuple[list[dict], set] | None:
    """The shared core of the SharePoint delta gate — the SharePoint mirror of
    _drive_delta_check. Advance (or seed) the stored cursor at `cursor_key` and return
    (changed_files, removed_ids) from Graph's delta query, or None when there is nothing yet
    to compare — a first-ever check for this cursor_key, or the check itself failed. None is
    NEVER 'nothing changed': every caller below must fall back to a full listing on None.

    Best-effort bookkeeping: always advances (or seeds) the stored cursor before returning. A
    SEED call is more expensive than Drive's equivalent (drive_start_page_token): Graph's delta
    API has no free-standing "just give me a baseline" call, so a token-less first call walks
    the ENTIRE current tree to reach the first deltaLink — paid once, here, and discarded rather
    than processed, not paid again until the drive itself resets."""
    from scanner import sp_delta_since
    cur = get_store().get_sync_cursor(cursor_key)
    try:
        if not cur or not cur.get("page_token"):
            _, _, new_link = sp_delta_since(token, drive_id, None)
            get_store().save_sync_cursor(cursor_key, owner, new_link)
            return None
        changed, removed_ids, new_link = sp_delta_since(token, drive_id, cur["page_token"])
        get_store().save_sync_cursor(cursor_key, owner, new_link)  # advance regardless of outcome
        return changed, removed_ids
    except BaseException as e:
        # BaseException — same reasoning as _drive_delta_check's identical catch: a broken
        # native dependency in an HTTP/crypto import chain must never turn uncertainty into an
        # unvouched-for reconstruction.
        print(f"sharepoint sync check ({cursor_key}): change-check failed ({e}) — falling back "
              f"to a full listing", flush=True)
        return None


def _sp_prior_inventory_for_drive(owner: str | None, drive_id: str | None) -> list[dict] | None:
    """The most recent completed SharePoint scan's inventory, but ONLY if it was actually a
    scan of THIS drive_id. store.latest_scan_inventory_items has no drive-scoped query of its
    own — it returns whatever the most recent 'sharepoint'-source scan for this owner covered.

    For the scheduled sweep that is a non-issue (every sweep targets the one configured
    sp_sync.sync_drive_id(), so the most recent scan is always for that same drive) UNLESS an
    operator repoints ACP_SP_SYNC_DRIVE_ID at a different library, in which case the stored
    inventory belongs to the OLD one. For an interactive scan it is a real, expected case: a
    signed-in user can scan a different SharePoint library — or their own OneDrive — from one
    scan to the next, and each has its own identity.

    Either way, reconstructing one drive's estate from another's inventory would silently show
    the wrong documents, so a drive_id mismatch on ANY row is treated the same as 'no prior scan
    to reconstruct from' — never a partial or approximate match. An empty-but-real prior scan
    (a library that was genuinely empty) is not a mismatch and still returns (as `[]`, not
    None) — this is the same 'is a real answer, not missing data' distinction None vs. []
    already carries for _drive_sync_plan's own use of latest_scan_inventory_items."""
    prior = get_store().latest_scan_inventory_items(owner, "sharepoint")
    if prior is None:
        return None
    if any(r.get("drive_id") != drive_id for r in prior):
        return None
    return prior


def _sp_sync_plan(owner: str | None, token: str) -> tuple[bool, dict | None]:
    """PRD Phase 3: the SharePoint mirror of _drive_sync_plan — decide what the scheduled
    sweep should do, from the stored Graph deltaLink. Returns (skip, sp_delta):

      - (True, None)    — a stored cursor confirms nothing changed since the last sweep; skip
                          the scan entirely.
      - (False, {...})  — something changed; {"prior_files", "changed", "removed_ids"} for
                          run_scan's sp_delta= to reconstruct the estate from, without walking
                          SharePoint (scanner.sp_reconstructed_listing).
      - (False, None)   — proceed with TODAY's full, fresh listing: first sweep ever, no prior
                          completed scan to reconstruct from, or the change-check itself failed.

    Uncertainty always resolves to (False, None) — a full, already-correct scan — never to a
    skip, and never to a reconstruction this function can't vouch for. A SKIP is the right
    answer for THIS caller specifically because nobody is waiting on a scheduled sweep's
    result — see _interactive_sp_sync_plan below for why an interactive scan can never use the
    same shortcut. Only ever called once sp_sync.sp_sync_configured() is already true — `token`
    is the dedicated sync app's own token, never a signed-in user's."""
    import sp_sync
    drive_id = sp_sync.sync_drive_id()
    result = _sp_delta_check("sharepoint", owner, token, drive_id)
    if result is None:
        return False, None
    changed, removed_ids = result
    if not changed and not removed_ids:
        return True, None
    prior = _sp_prior_inventory_for_drive(owner, drive_id)
    if prior is None:
        # A cursor exists but no completed scan of THIS drive does (e.g. every prior sweep
        # since it was seeded has failed before saving, or the configured drive changed) —
        # nothing to reconstruct FROM. A full listing gives the NEXT sweep a real baseline.
        print(f"scheduled sharepoint sweep: {len(changed)} changed, {len(removed_ids)} "
              f"removed since last sync, but no prior scan to reconstruct from — running a "
              f"full scan", flush=True)
        return False, None
    print(f"scheduled sharepoint sweep: {len(changed)} changed, {len(removed_ids)} removed "
          f"since last sync — reconstructing the estate instead of a full re-list",
          flush=True)
    from scanner import _sp_file_from_inventory_row
    prior_files = [_sp_file_from_inventory_row(r) for r in prior]
    return False, {"prior_files": prior_files, "changed": changed, "removed_ids": removed_ids}


def _sp_interactive_cursor_key(owner: str | None, drive_id: str | None) -> str:
    """The per-(owner, drive) cursor key for an interactive SharePoint scan — JSON-encoded, not
    an f-string colon-join. `drive_id` comes from scanner._sp_locations, which parses it off a
    CLIENT-supplied `folder` request value with no format validation (`r.partition("/")`):
    a folder like "a:b/root" yields drive_id="a:b". A naive f"sharepoint:{owner}:{drive_id}"
    key made two different (owner, drive_id) pairs collide on the identical string whenever one
    of them happened to contain a literal colon — e.g. owner="x", drive_id="y:z" produced the
    same key as owner="x:y", drive_id="z". `owner` itself is a trusted authenticated email, but
    `drive_id` is not, so the delimiter can't be trusted to never appear in it.

    Practical impact of a collision was always bounded (_sp_prior_inventory_for_drive still
    verifies drive_id per row before trusting any reconstruction), so this was cursor-row
    corruption risk, not a data-leakage one — but json.dumps of the pair is unambiguous by
    construction and costs nothing to get right, so there is no reason to keep relying on hoping
    ":" never collides."""
    import json
    return f"sharepoint:{json.dumps([owner, drive_id])}"


def sp_reconcile_days() -> int:
    """How old a stored delta cursor may get before its library is walked in FULL again.

    A CORRECTNESS control, not a performance knob, and the reason it exists is specific: Graph's
    delta feed reports changes to the driveItem, and a managed-column edit that does not touch
    the driveItem may never appear in it. A library synced incrementally forever would carry a
    stale retention label or records category indefinitely, with nothing anywhere saying so —
    the failure mode is silent and it grows.

    Seven days by default: a week of drift is a week of a rule keying on a column that has since
    changed, which is recoverable; a quarter of it is not. 0 disables the forced reconciliation
    for an operator who has measured their tenant and accepts the risk knowingly.
    """
    try:
        n = int(os.environ.get("ACP_SP_RECONCILE_DAYS", "7") or 7)
    except ValueError:
        return 7
    return max(0, n)


def _sp_cursor_is_stale(cursor: dict | None) -> str | None:
    """The reason this cursor's library is due a full reconciliation, or None to sync it.

    A cursor with no readable `updated_at` is treated as DUE, not as fresh: an unparseable
    timestamp is a fact we do not have, and defaulting the unknown to "recently synced" is how a
    library would quietly never be reconciled again.
    """
    days = sp_reconcile_days()
    if not days:
        return None
    if not cursor:
        return None                       # no cursor at all is a seed, handled by the caller
    from source_staleness import parse_rfc3339
    when = parse_rfc3339(cursor.get("updated_at"))
    if when is None:
        return "the stored cursor has no readable timestamp, so its age cannot be trusted"
    import datetime as _dt
    age = (_dt.datetime.now(_dt.timezone.utc) - when).days
    if age >= days:
        return (f"the delta cursor is {age} days old (ACP_SP_RECONCILE_DAYS={days}) — a full "
                f"re-list catches column edits Graph's delta feed does not report")
    return None


def sp_multi_sync_plan(owner: str, token: str, drive_ids: list[str | None]) -> dict:
    """PRD Phase 3 at ESTATE SCALE: one plan covering several document libraries at once.

    `_interactive_sp_sync_plan` answers for exactly one drive, because Graph's delta query is
    scoped to one drive and has no folder filter. A 30-site estate is 30-plus drives, and the
    question "can this scan skip walking?" stops having a single answer: one library's cursor is
    fresh, another's expired last week, a third has never been synced, a fourth is due its
    periodic reconciliation. Collapsing that to one yes/no means either walking everything
    because one library needs it, or reconstructing everything and quietly serving a stale
    estate for the one that did not.

    So the answer is PER LIBRARY::

        {"delta":  {drive_id: {"prior_files", "changed", "removed_ids"}},
         "full":   {drive_id: "why this one has to be walked"},
         "carried": int}     # documents carried forward without re-reading

    A drive in `full` is walked exactly as it always was. A drive in `delta` is reconstructed.
    One expired cursor degrades ONE library, and the estate is still mostly free.

    UNCERTAINTY ALWAYS RESOLVES TO A FULL WALK of the library in question — never to a skip and
    never to a reconstruction this function cannot vouch for. That is _sp_delta_check's own
    contract (None means "fall back"), applied per drive instead of per scan.
    """
    plan: dict = {"delta": {}, "full": {}, "carried": 0}
    if not drive_ids:
        return plan
    priors = _sp_prior_inventory_by_drive(owner, drive_ids)
    for drive_id in drive_ids:
        key = _sp_interactive_cursor_key(owner, drive_id)
        stale = _sp_cursor_is_stale(get_store().get_sync_cursor(key))
        if stale:
            # Advance the cursor anyway, so the NEXT scan can go incremental again from a fresh
            # baseline. Skipping that would make a reconciled library reconcile forever.
            _sp_delta_check(key, owner, token, drive_id)
            plan["full"][drive_id] = stale
            continue
        result = _sp_delta_check(key, owner, token, drive_id)
        if result is None:
            plan["full"][drive_id] = ("no usable delta cursor for this library yet (first sync, "
                                      "an expired link, or the change-check failed) — walking it "
                                      "in full and seeding one for next time")
            continue
        prior = priors.get(drive_id)
        if prior is None:
            plan["full"][drive_id] = ("no prior scan of this library to reconstruct from — "
                                      "walking it in full to establish a baseline")
            continue
        changed, removed_ids = result
        from scanner import _sp_file_from_inventory_row
        plan["delta"][drive_id] = {
            "prior_files": [_sp_file_from_inventory_row(r) for r in prior],
            "changed": changed, "removed_ids": removed_ids}
        plan["carried"] += max(0, len(prior) - len(changed))
    return plan


def _sp_prior_inventory_by_drive(owner: str | None,
                                 drive_ids: list[str | None]) -> dict[str | None, list[dict]]:
    """The most recent completed SharePoint scan's inventory, PARTITIONED by drive.

    _sp_prior_inventory_for_drive answers the single-drive question by rejecting the whole
    baseline if ANY row belongs to a different drive — correct when a scan covers one library,
    and exactly wrong once a scan covers thirty: every row would "belong to a different drive"
    from the perspective of twenty-nine of them, and no library would ever have a baseline.

    Partitioning instead gives each library its own, and a library with no rows in the prior scan
    simply has none — that library is walked, the others are not. A drive whose partition is
    empty is absent from the result rather than present-and-empty, because those mean different
    things to the caller: absent is "no baseline, walk it", and this function never returns the
    other one for a drive the prior scan genuinely did not cover.
    """
    prior = get_store().latest_scan_inventory_items(owner, "sharepoint")
    if prior is None:
        return {}
    wanted = set(drive_ids)
    out: dict[str | None, list[dict]] = {}
    for row in prior:
        d = row.get("drive_id")
        if d in wanted:
            out.setdefault(d, []).append(row)
    return out


def _interactive_sp_sync_plan(owner: str, token: str, drive_id: str | None) -> dict | None:
    """PRD Phase 3, interactive SharePoint scans: the same delta reconstruction _sp_sync_plan
    gives the scheduled sweep, but for a user-initiated whole-library (or whole OneDrive,
    drive_id=None) SharePoint scan — keyed per (SIGNED-IN USER, DRIVE) via
    _sp_interactive_cursor_key(owner, drive_id). Unlike Drive (one account, one drive, always
    the same) and unlike the scheduled sweep (always the one configured drive), a SharePoint
    user can interactively scan a DIFFERENT library from one scan to the next, so there is no
    single fixed cursor or baseline to assume — both are keyed (and, for the baseline, VERIFIED
    via _sp_prior_inventory_for_drive) per drive, not just per user.

    Returns the SAME sp_delta dict shape scanner.sp_reconstructed_listing already knows how to
    build from, or None to fall back to a normal full listing.

    DELIBERATELY NO SKIP, same reasoning as _interactive_drive_sync_plan: an interactive user is
    owed a completed scan every time, so 'nothing changed' still returns a delta with empty
    changed/removed sets rather than signalling 'do nothing'.

    Callers restrict this to a single whole Graph drive (see
    scanner._sp_whole_library_target / handlers._scan_discover) — sp_delta_since has no folder
    filter of its own, so it cannot honor a sub-folder narrowing, and it is scoped to exactly
    one drive, so it cannot answer for a multi-library site scan either. `token` is the
    signed-in user's own Graph token, never the scheduled sweep's dedicated sync app token."""
    result = _sp_delta_check(_sp_interactive_cursor_key(owner, drive_id), owner, token, drive_id)
    if result is None:
        return None
    changed, removed_ids = result
    prior = _sp_prior_inventory_for_drive(owner, drive_id)
    if prior is None:
        return None
    if changed or removed_ids:
        print(f"interactive sharepoint scan ({owner}, drive={drive_id}): {len(changed)} "
              f"changed, {len(removed_ids)} removed since last sync — reconstructing the "
              f"estate instead of a full re-list", flush=True)
    from scanner import _sp_file_from_inventory_row
    prior_files = [_sp_file_from_inventory_row(r) for r in prior]
    return {"prior_files": prior_files, "changed": changed, "removed_ids": removed_ids}


def _schedule_lifecycle_complete(st, occurrence: dict | None, *, result: str,
                                 changed=None, error=None, scan_id=None) -> None:
    """Best-effort lifecycle + notification writes for new owner schedules.

    Capability checks keep a mixed-version deployment compatible with the legacy singleton.
    The scan result is authoritative even when telemetry or notification persistence is down.
    """
    if not occurrence or not occurrence.get("owner_email"):
        return
    import datetime as _dt
    owner, key = occurrence["owner_email"], occurrence.get("occurrence_key")
    if not key:
        return
    completed = _dt.datetime.now(_dt.timezone.utc).isoformat()
    begin = getattr(st, "begin_schedule_occurrence", None)
    if callable(begin):
        try:
            # Ensures an admission-time skip/failure still has a history row. For a running
            # occurrence this is an idempotent no-op at the Store boundary.
            begin(owner, key, occurrence.get("scheduled_for") or completed, completed,
                  delay_reason=error if result == "skipped" else None)
        except Exception:
            swallowed("core._schedule_lifecycle_complete: creating the occurrence record failed")
    complete = getattr(st, "complete_schedule_occurrence", None)
    if callable(complete):
        try:
            complete(owner, key, result=result, completed_at=completed,
                     changed=changed, error=error)
        except Exception:
            swallowed("core._schedule_lifecycle_complete: completing the occurrence record failed")
    emit = getattr(st, "emit_schedule_notification_for_occurrence", None)
    if callable(emit):
        try:
            message = str(error) if error else (f"Scan {scan_id} completed" if scan_id else None)
            emit(owner, key, result, changed=bool(changed), message=message)
        except Exception:
            swallowed("core._schedule_lifecycle_complete: emitting the schedule notification failed")


def _scheduled_scan_admission(occurrence: dict, *, now=None) -> dict:
    """Re-check queue/admin policy immediately before a scheduled job starts."""
    import datetime as _dt
    st = get_store()
    check = getattr(st, "schedule_admission", None)
    if not callable(check) or not occurrence.get("owner_email"):
        return {"admit": True, "reason": None}
    instant = now or _dt.datetime.now(_dt.timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=_dt.timezone.utc)
    return check(occurrence["owner_email"], occurrence["occurrence_key"],
                 occurrence.get("scheduled_for") or instant.isoformat(), instant.isoformat())


def _do_scheduled_scan(occurrence: dict | None = None):
    """A scheduled sweep. Re-scans the configured source (Drive via the service-account
    ADC identity — no user token is available in the background), stamps it with the
    owner who set the schedule so it shows up in their scan list, and finalizes it."""
    import datetime as _dt
    if occurrence and occurrence.get("owner_email"):
        import scan_schedule as _scan_schedule
        cfg = get_store().get_user_scan_schedule(occurrence["owner_email"])
        # A durable job may start after its owner disabled or changed the schedule. Re-read the
        # owner-scoped row and require the queued local occurrence to still describe it.
        try:
            local_day = _dt.date.fromisoformat(str(occurrence.get("local_date")))
            current_key = _scan_schedule.occurrence_key(cfg, local_day)
        except (TypeError, ValueError, _scan_schedule.ScheduleError):
            current_key = None
        if not cfg.get("enabled") or current_key != occurrence.get("occurrence_key"):
            print("scheduled user sweep skipped — its schedule changed after enqueue", flush=True)
            return
    else:
        cfg = get_store().get_schedule()

    # THE SETTING IS AUTHORITATIVE ON EVERY FIRE, not only when reload_scheduler() runs.
    #
    # `PUT /schedule` calls reload_scheduler() in the process that served the request. Only
    # acp-app has ingress, so that is the ONLY process it can ever reach — while worker_main.py
    # arms its own scheduler with this same job (worker_main:49-50). Turning the schedule off in
    # the UI therefore left the worker's copy running until the container happened to restart,
    # with nothing on any surface saying so.
    #
    # Checking here fixes that without cross-container messaging, which is why it is done here
    # rather than by broadcasting a reload: every process that fires this job reads the same row,
    # so none of them can disagree with the setting for longer than one interval. It also covers
    # a stale job left by a failed reload, which a broadcast would not.
    if not cfg.get("enabled"):
        print("scheduled sweep skipped — the schedule is off (stale job in this process)", flush=True)
        if not occurrence:
            reload_scheduler()  # legacy singleton self-heal
        return

    st = get_store()
    owner = cfg.get("owner_email")
    source = cfg.get("source") or "drive"
    ai = get_store().get_ai_enabled()

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if occurrence:
        begin = getattr(st, "begin_schedule_occurrence", None)
        if callable(begin):
            try:
                begin(owner, occurrence["occurrence_key"],
                      occurrence.get("scheduled_for") or now, now,
                      delay_reason="catch_up" if occurrence.get("catch_up") else None)
            except Exception:
                swallowed("core._do_scheduled_scan: starting the occurrence record failed")

    drive_delta = None
    sp_delta = None
    sp_token = None
    sp_folder = None
    if source == "drive":
        cursor_key = f"drive:scheduled:{str(owner).strip().lower()}" if occurrence else "drive"
        scope = cfg.get("source_scope") or {}
        narrowed = bool(scope.get("include_ids") or scope.get("exclude_ids"))
        skip, drive_delta = (False, None) if narrowed else _drive_sync_plan(owner, cursor_key)
        if skip:
            print("scheduled drive sweep skipped — no changes since the last sync", flush=True)
            get_store().record_sweep_outcome(ok=True, when=now, source=source, skipped=True,
                                             owner=owner if occurrence else None)
            _schedule_lifecycle_complete(st, occurrence, result="skipped", changed=False)
            return
    elif source == "sharepoint":
        import sp_sync
        if sp_sync.sp_sync_configured():
            # The dedicated sync app's own token authenticates BOTH the change-check and, when
            # something changed, the full scan below — this is what makes an unattended
            # SharePoint sweep work at all for the first time, not only more efficient. Without
            # this configured, sp_token stays None and the sweep proceeds exactly as it always
            # has: no unattended SharePoint credential, so it fails with the same
            # PermissionError it always has. Zero behavior change when unconfigured.
            sp_token = sp_sync.app_token()
            # `{drive_id}/root` — the <driveId>/<itemId> folder-location syntax _sp_locations
            # already parses, with Graph's own "root" item-id alias so this targets the WHOLE
            # configured library with no folder narrowing and no site enumeration (an app-only
            # token has no "signed-in user" for the site-less OneDrive default to fall back to).
            sp_folder = f"{sp_sync.sync_drive_id()}/root"
            scope = cfg.get("source_scope") or {}
            narrowed = bool(scope.get("include_ids") or scope.get("exclude_ids"))
            skip, sp_delta = (False, None) if narrowed else _sp_sync_plan(owner, sp_token)
            if skip:
                print("scheduled sharepoint sweep skipped — no changes since the last sync",
                      flush=True)
                get_store().record_sweep_outcome(ok=True, when=now, source=source, skipped=True,
                                                 owner=owner if occurrence else None)
                _schedule_lifecycle_complete(st, occurrence, result="skipped", changed=False)
                return

    try:
        scope = cfg.get("source_scope") or {}
        include_ids = list(scope.get("include_ids") or [])
        exclude_ids = list(scope.get("exclude_ids") or [])
        scan_scope = {}
        if occurrence and (include_ids or exclude_ids):
            scan_scope = {"folders": include_ids or None,
                          "exclude_folders": exclude_ids or None}
        report = run_scan(source, drive_token=None, ai_enabled=ai, user=owner,   # ADC for drive
                          drive_delta=drive_delta, sp_delta=sp_delta,
                          sp_token=sp_token,
                          folder=sp_folder if not include_ids else None,
                          **scan_scope)
        sid = get_store().save_scan(report)
        finalize_scan(sid, ai, source)
        get_store().record_sweep_outcome(ok=True, when=now, source=source, scan_id=sid,
                                         files=report["summary"]["files"],
                                         owner=owner if occurrence else None)
        # A connector delta is the only authoritative changed/no-change signal available here.
        # A narrowed/full first scan has no comparable prior boundary, so do not turn an
        # unknown into a noisy "changes found" notification.
        changed = None
        delta = drive_delta if drive_delta is not None else sp_delta
        if delta is not None:
            changed = bool(delta.get("changed") or delta.get("removed_ids"))
        _schedule_lifecycle_complete(st, occurrence, result="succeeded", changed=changed,
                                     scan_id=sid)
        print(f"scheduled {source} sweep complete: {report['summary']['files']} files "
              f"(owner={owner})", flush=True)
    except Exception as e:
        # DO NOT substitute a different corpus. This used to fall back to `local` and save +
        # finalize that as a scan, which is how a 258-document Drive estate was displaced every
        # five minutes by a 1-file scan of the bundled samples — and because every "latest" view
        # takes scan_runs ordered by completed_at, that fallback became the estate as far as the
        # dashboard, the report and the scan selector were concerned.
        #
        # The fallback was written for "Drive/ADC may be unconfigured in this environment", and
        # for that case it is still wrong: an unconfigured source should report that it is
        # unconfigured, not quietly produce numbers about something else. A sweep that cannot
        # reach its source has nothing to say, so it says nothing and leaves the last real scan
        # standing.
        #
        # But "leaves the last real scan standing" is only honest if somebody is told. Until this
        # was recorded, the sole trace of a failing sweep was this log line inside the container,
        # while the UI kept presenting an hours-old scan as the live estate — which is how a
        # 403 insufficient-scopes loop ran unnoticed on 2026-07-29. /schedule reports it now.
        get_store().record_sweep_outcome(ok=False, when=now, source=source, error=str(e),
                                         owner=owner if occurrence else None)
        _schedule_lifecycle_complete(st, occurrence, result="failed", error=str(e))
        print(f"scheduled {source} sweep FAILED — no scan was saved, the previous scan stands: {e}",
              flush=True)


def _enqueue_scheduled_scan(now=None) -> bool:
    """Offer the current wall-clock occurrence to the durable queue.

    APScheduler exists in every replica. All replicas calculate the same UTC interval bucket,
    while Store.enqueue_scheduled_sweep atomically admits only one queue row for that bucket.
    """
    import datetime as _dt
    import scan_schedule as _scan_schedule
    st = get_store()
    instant = now or _dt.datetime.now(_dt.timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=_dt.timezone.utc)
    schedules = (st.list_enabled_user_scan_schedules()
                 if hasattr(st, "list_enabled_user_scan_schedules") else [])
    request_prewarm = getattr(st, "request_schedule_prewarm", None)
    if callable(request_prewarm):
        for forecast in _scan_schedule.prewarm_candidates(schedules, instant):
            try:
                request_prewarm(forecast["owner_email"], forecast["occurrence_key"],
                                forecast["scheduled_for"], instant.isoformat(),
                                forecast["source"])
            except Exception:
                continue
    admitted = False
    for cfg in schedules:
        due = _scan_schedule.due_or_most_recent_occurrence(cfg, instant)
        if not due:
            continue
        payload = {**due, "owner_email": cfg["owner_email"],
                   "source": cfg.get("source") or "drive",
                   "timezone": cfg["timezone"], "local_time": cfg["local_time"]}
        try:
            if _scan_schedule.in_blackout(cfg, _dt.datetime.fromisoformat(due["scheduled_for"])):
                _schedule_lifecycle_complete(st, payload, result="skipped",
                                             error="user blackout window")
                continue
        except (_scan_schedule.ScheduleError, TypeError, ValueError):
            _schedule_lifecycle_complete(st, payload, result="skipped",
                                         error="invalid blackout window")
            continue
        run_after = None
        if due.get("catch_up"):
            # A fleet restart can recover many tenants at once. Spread catch-up admissions over
            # five minutes with stable owner-based jitter; normal on-time scans remain immediate.
            import hashlib as _hashlib
            digest = _hashlib.sha256(cfg["owner_email"].lower().encode()).hexdigest()
            delay = int(digest[:8], 16) % 300
            run_after = (instant + _dt.timedelta(seconds=delay)).isoformat()
        admission = getattr(st, "schedule_admission", None)
        if callable(admission):
            decision = admission(cfg["owner_email"], due["occurrence_key"],
                                 due["scheduled_for"], instant.isoformat())
            if not decision.get("admit"):
                if decision.get("terminal"):
                    _schedule_lifecycle_complete(st, payload, result="skipped",
                                                 error=decision.get("reason"))
                    continue
                run_after = decision.get("run_after") or run_after
                deferred = getattr(st, "defer_scheduled_sweep", None)
                if callable(deferred) and run_after:
                    deferred(cfg["owner_email"], due["occurrence_key"], run_after,
                             decision.get("reason") or "queue_policy", due["scheduled_for"])
        if run_after:
            accepted = st.enqueue_scheduled_sweep(
                due["occurrence_key"], payload, run_after=run_after)
        else:
            accepted = st.enqueue_scheduled_sweep(due["occurrence_key"], payload)
        admitted = accepted or admitted
    if schedules:
        return admitted

    # Rolling-deploy compatibility: until the old global row is replaced by a user schedule,
    # keep its interval behavior intact.
    cfg = st.get_schedule()
    interval_minutes = int(cfg.get("interval_minutes") or 0)
    if not cfg.get("enabled") or interval_minutes <= 0:
        return False
    bucket = int(instant.timestamp()) // (interval_minutes * 60)
    return st.enqueue_scheduled_sweep(f"{interval_minutes}:{bucket}")


def _next_scheduled_scan_fire(interval_minutes: int, now=None):
    """Return the next UTC interval boundary so independently-started replicas stay aligned."""
    import datetime as _dt
    instant = now or _dt.datetime.now(_dt.timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=_dt.timezone.utc)
    seconds = int(interval_minutes) * 60
    next_epoch = ((int(instant.timestamp()) // seconds) + 1) * seconds
    return _dt.datetime.fromtimestamp(next_epoch, tz=_dt.timezone.utc)


def _derive_review_memory_tick() -> None:
    """Nightly ADR 0021 derivation: for every org, read recent HITL behaviour and PROPOSE
    (never activate) house-style rules. Best-effort; only armed when review memory is on."""
    import memory as _mem
    st = get_store()
    for org in st.list_org_owners():
        try:
            _mem.derive_org_memory(st, org)
        except Exception:
            continue


def reload_scheduler():
    st = get_store()
    cfg = st.get_schedule()
    user_schedules = (st.list_enabled_user_scan_schedules()
                      if hasattr(st, "list_enabled_user_scan_schedules") else [])
    scheduler.remove_all_jobs()
    if user_schedules:
        scheduler.add_job(_enqueue_scheduled_scan, "interval", minutes=1,
                          next_run_time=_next_scheduled_scan_fire(1),
                          id="scheduled_local_scan",
                          coalesce=True, max_instances=1)
    elif cfg["enabled"] and cfg["interval_minutes"] > 0:
        scheduler.add_job(_enqueue_scheduled_scan, "interval", minutes=cfg["interval_minutes"],
                          next_run_time=_next_scheduled_scan_fire(cfg["interval_minutes"]),
                          id="scheduled_local_scan", coalesce=True, max_instances=1)
    # ADR 0021 stage 3 — nightly review-memory derivation, only when the feature is on. Dark
    # by default (flag unset), so it adds no scheduled work to today's deploys.
    if os.environ.get("ACP_REVIEW_MEMORY", "").strip().lower() in ("1", "true", "yes", "on"):
        scheduler.add_job(_derive_review_memory_tick, "cron", hour=3, minute=17,
                          id="review_memory_derive", coalesce=True, max_instances=1)


# NOT called at import: reload_scheduler() reads the schedule out of the database, which would
# build the Store as a side effect of `import core` and defeat the laziness above. app.py calls
# it from its startup hook. This also stops a non-API process that merely imports core (a job
# worker, a CLI script) from arming its own copy of the scheduled scan.


# ── Async job-queue worker pool (ADR 0004) ────────────────────────────────────
# Opt-in: set ACP_WORKERS>0 to run N in-process worker threads that drain the
# `jobs` table (async assess + remediation). Off by default so existing behavior
# is unchanged until the handlers + enqueue paths are wired.
#
# ACP_RUNTIME_MODE controls the default when ACP_WORKERS is not set:
#   "single-node"  — one process handles both API and worker duties; default 4 workers.
#   "distributed"  — a separate worker container handles the queue; default 0 (correct for Azure).
#   "auto"         — backward-compatible default: 0 (matches prior behaviour).
# Explicit ACP_WORKERS always wins regardless of mode.
_RUNTIME_MODE = os.environ.get("ACP_RUNTIME_MODE", "auto")
_WORKER_DEFAULT = "4" if _RUNTIME_MODE == "single-node" else "0"
WORKERS = int(os.environ.get("ACP_WORKERS", _WORKER_DEFAULT) or _WORKER_DEFAULT)
_worker_handles: list = []
_worker_seq = 0          # monotonic id source so scaled-in workers get fresh ids
_MAX_WORKERS = 16        # safety cap on live scaling
_STAGE_BACKFILL_MARKER = "maintenance:stage-execution-backfill-v1:complete"


def _run_stage_execution_backfill_once() -> dict | None:
    """Backfill historical stage batches once per fleet, retrying after interrupted owners."""
    st = get_store()
    if st.get_setting(_STAGE_BACKFILL_MARKER):
        return None
    if not st.claim_maintenance_lease("stage-execution-backfill-v1", lease_seconds=3600):
        return None
    import json as _json
    report = st.backfill_stage_executions()
    st.set_setting(_STAGE_BACKFILL_MARKER, _json.dumps(report, sort_keys=True))
    print(f"[sweeper] canonical stage backfill complete: {report}", flush=True)
    return report


def _discovery_reservation(pool_size):
    # Reserve existing capacity, never the last general-purpose slot.
    try:
        requested = int(os.environ.get("ACP_DISCOVERY_RESERVED_WORKERS", "0"))
    except ValueError:
        requested = 0
    return max(0, min(requested, pool_size - 1))


# THE DISCOVERY STAGE IS TWO JOB TYPES, NOT ONE. `scan_discover` lists the source and fans out
# one `scan_folder` per top-level folder; those folder jobs are what actually enumerate the
# estate. Naming only the entry job means the Discovery lane — whether that is a reserved slot in
# a mixed pool or a whole dedicated service (ACP_WORKER_ROLE=discovery) — takes the first job,
# fans out, and then goes idle while its own follow-on work queues in the processing lane behind
# the content backlog. It covers the starting gun and not the race.
#
# `scan` is deliberately NOT here. Under ACP_DEFER_ANALYSIS_TO_ASSESS=0 it downloads and analyses
# the whole estate, and a lane sized for short metadata work must not be occupied for minutes by
# one content job. It gets queue precedence (store.job_priority) but no dedicated slot.
DISCOVERY_LANE_JOB_TYPES = ("scheduled_sweep", "scan_discover", "scan_folder")

# Stage-owned queues. These tuples are intentionally disjoint: once the generic processing
# service is retired, an Assess backlog cannot consume Remediate capacity and vice versa.
#
# WORKSPACE DISCOVERY IS AN ASSESS-LANE JOB, not a Discovery-lane one, which looks wrong from the
# name and is right from the work. The Discovery lane exists for the connector walk — minutes of
# paginated traffic, which is why it gets reserved capacity at all. workspace_scan_discover reads
# an uploaded workspace out of the database in one indexed SELECT and fans out; it touches no
# connector and holds no worker. Keeping it beside workspace_scan_file also means an uploaded
# estate is assessed end to end by one service, with no cross-lane handoff to stall on.
ASSESS_LANE_JOB_TYPES = (
    "scan", "scan_assess", "scan_batch", "scan_file", "workspace_scan_file",
    "workspace_scan_discover", "scan_finalize", "assess_trace",
)
# `deliver_corrected_copy` is a Remediate-lane job even though it opens no document and applies no
# fix: it finishes the work `remediate_file` started, on the same run, against the same provider
# grant, and it is the retry an operator reaches for when that job's provider write failed. Putting
# it anywhere else would let a Remediate backlog and its own recovery queue behind different
# capacity — the exact cross-lane stall these disjoint tuples exist to prevent.
REMEDIATE_LANE_JOB_TYPES = (
    "vision_proposal_retry",
    "remediate_file", "deliver_corrected_copy", "rescore_file", "apply_approved_values",
    "publish_file", "prepare_release_package", "release_continue", "publish_release_reports",
)


RELEASE_LANE_JOB_TYPES = (
    "publish_file", "prepare_release_package", "release_continue", "publish_release_reports",
)
RELEASE_REPORT_JOB_TYPES = ("publish_release_reports",)


def dedicated_release_enabled():
    """Explicit cutover; older installations keep release work in remediation."""
    return os.environ.get("ACP_DEDICATED_RELEASE_WORKERS", "0") == "1"


def remediation_job_types():
    return tuple(kind for kind in REMEDIATE_LANE_JOB_TYPES
                 if not dedicated_release_enabled() or kind not in RELEASE_LANE_JOB_TYPES)


def _replica_id() -> str:
    """This replica's identity, for a globally unique worker id.

    Read through joblog rather than re-reading the environment, so log attribution and job
    attribution cannot drift. Falls back to a random suffix only when the platform supplies
    nothing: "unknown:w0" from every replica would recreate the collision this exists to remove.
    """
    try:
        import joblog  # noqa: PLC0415
        replica = (joblog.REPLICA or "").strip()
    except Exception:  # noqa: BLE001 — identity must never take the worker pool down
        replica = ""
    if replica and replica != "unknown":
        return replica
    global _REPLICA_FALLBACK
    if _REPLICA_FALLBACK is None:
        import uuid  # noqa: PLC0415
        _REPLICA_FALLBACK = f"unknown-{uuid.uuid4().hex[:8]}"
    return _REPLICA_FALLBACK


_REPLICA_FALLBACK = None


def worker_process_instance_id(role: str | None = None) -> str:
    """Identity for this process, unique across replicas and across restarts."""
    import joblog  # noqa: PLC0415
    worker_role = (role or os.environ.get("ACP_WORKER_ROLE") or "mixed").strip().lower()
    return f"{worker_role}:{_replica_id()}:{joblog.PROC}"


def _worker_job_types(index, pool_size):
    """Route dedicated services before falling back to the mixed pool reservation."""
    from worker import HANDLERS
    role = os.environ.get("ACP_WORKER_ROLE", "mixed").strip().lower()
    if role == "discovery":
        return DISCOVERY_LANE_JOB_TYPES
    if role == "assess":
        return ASSESS_LANE_JOB_TYPES
    if role == "remediate":
        return remediation_job_types()
    if role == "release":
        if pool_size < 3:
            raise ValueError("Release workers require at least three slots: two delivery and one report")
        # One reserved report slot; reports cannot occupy all delivery capacity.
        return RELEASE_REPORT_JOB_TYPES if index == 0 else tuple(
            kind for kind in RELEASE_LANE_JOB_TYPES if kind not in RELEASE_REPORT_JOB_TYPES)
    if role == "processing":
        # Excludes the WHOLE Discovery stage, not just its entry job. Otherwise "isolate
        # Discovery and processing by role" leaks: processing workers claim the scan_folder
        # jobs the discovery service just fanned out, which is both a breach of the isolation
        # and the reason a dedicated discovery service would sit idle mid-scan.
        return tuple(sorted(n for n in HANDLERS if n not in DISCOVERY_LANE_JOB_TYPES and
                            (not dedicated_release_enabled() or n not in RELEASE_LANE_JOB_TYPES)))
    if role != "mixed":
        raise ValueError("ACP_WORKER_ROLE must be mixed, discovery, assess, remediate, release, or processing")
    if index < _discovery_reservation(pool_size):
        return DISCOVERY_LANE_JOB_TYPES
    return tuple(sorted(n for n in HANDLERS if n not in RELEASE_LANE_JOB_TYPES)) if dedicated_release_enabled() else None


def _spawn_worker() -> None:
    import threading
    from worker import JobWorker
    global _worker_seq
    # on_retry=update_job: lets a worker announce "failed, waiting to retry" on the live job
    # poll/SSE stream without worker.py importing core (it is deliberately infra-only — see its
    # own docstring). See _job_is_stale's phase=='retrying' exemption below for why this signal
    # can outlive the normal 90s staleness window (backoff can run up to 600s).
    # GLOBALLY UNIQUE, not "w0". The id was a per-PROCESS sequence, so every replica minted the
    # same handful of names — production ran ten Assess replicas and `locked_by` held only `w0`
    # and `w1`. Counting distinct values therefore counted workers per replica, never replicas,
    # and a diagnosis built on it (2026-09-05, a suspected stuck queue) could not have been right
    # whatever the data said. Prefixing the replica makes the id identify one worker in the fleet.
    #
    # joblog.REPLICA is the same resolution used for log attribution — the Container Apps replica
    # name, else HOSTNAME, else "unknown" — so the two agree rather than inventing a second answer.
    process_id = worker_process_instance_id()
    w = JobWorker(get_store(), worker_id=f"{process_id}:w{_worker_seq}", on_retry=update_job)
    w.job_types = _worker_job_types(len(_worker_handles), WORKERS)
    t = threading.Thread(target=w.run_forever, daemon=True, name=f"jobworker-{_worker_seq}")
    _worker_seq += 1
    t.start()
    _worker_handles.append((w, t))


def set_worker_count(n: int) -> int:
    """Scale the in-process worker pool to n live workers (spawn or stop threads).
    Stopped workers finish their current job before exiting. Live for THIS process
    only -- a restart/redeploy always resets to ACP_WORKERS (see start_workers).
    Returns the new live count."""
    global WORKERS
    n = max(0, min(int(n), _MAX_WORKERS))
    import handlers  # noqa: F401 — ensure job handlers are registered before spawning
    cur = len(_worker_handles)
    if n > cur:
        for _ in range(n - cur):
            _spawn_worker()
    elif n < cur:
        for w, _t in _worker_handles[n:]:
            w.stop()                       # exits after the current job (if any)
        del _worker_handles[n:]
    WORKERS = n
    for index, (worker, _thread) in enumerate(_worker_handles):
        worker.job_types = _worker_job_types(index, n)
    return len(_worker_handles)


def stop_workers() -> None:
    """Graceful drain for shutdown/redeploy. Signals every worker to stop after its
    current job (the loop checks between jobs), briefly joins so idle/near-done workers
    exit cleanly instead of being SIGKILLed and left 'running' until the 30-min lease
    sweeper reclaims them on the next container. A long in-flight job still can't be
    interrupted — it falls back to lease reclaim — but the common idle/between-jobs
    deploy now drains cleanly. Best-effort and time-bounded so shutdown can't hang.
    Flushes Langfuse last so a redeploy doesn't drop the last job's spans."""
    global WORKERS
    import time as _t
    for w, _t2 in _worker_handles:
        try:
            w.stop()
        except Exception:
            swallowed("core.stop_workers: stopping a worker failed")
    # Production sets this to 540s alongside a 600s Container Apps termination grace period
    # (deploy/public/redeploy.sh). Keep the short local/default window so an unstamped developer
    # worker or test cannot hang shutdown for nine minutes.
    deadline = _t.monotonic() + float(os.environ.get("ACP_SHUTDOWN_DRAIN_SECONDS", "20"))
    for _w, t in _worker_handles:
        remaining = deadline - _t.monotonic()
        if remaining > 0:
            try:
                t.join(timeout=remaining)
            except Exception:
                swallowed("core.stop_workers: joining a worker thread failed")
    _worker_handles.clear()
    WORKERS = 0
    try:
        import lf as _lf
        _lf.flush()
    except Exception:
        swallowed("core.stop_workers: flushing Langfuse on shutdown failed")


def reset_langfuse_traces() -> int:
    """Best-effort: delete all traces in the ACP Langfuse project via the public
    API. Returns the count deleted (0 if Langfuse isn't configured, or its version
    doesn't support trace deletion — the Postgres reset still works regardless)."""
    import lf as _lf
    host, pk, sk = _lf._HOST, _lf._PK, _lf._SK
    if not (host and pk and sk):
        return 0
    import base64
    import httpx
    auth = {"Authorization": "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()}
    deleted = 0
    try:
        with httpx.Client(timeout=30) as c:
            ids: list[str] = []
            for page in range(1, 101):                       # cap at 10k traces
                r = c.get(f"{host}/api/public/traces",
                          params={"limit": 100, "page": page}, headers=auth)
                r.raise_for_status()
                data = r.json().get("data", [])
                ids += [t["id"] for t in data if t.get("id")]
                if len(data) < 100:
                    break
            for i in range(0, len(ids), 100):                # bulk delete in batches
                resp = c.request("DELETE", f"{host}/api/public/traces",
                                 json={"traceIds": ids[i:i + 100]}, headers=auth)
                if resp.status_code < 300:
                    deleted += len(ids[i:i + 100])
    except Exception:
        swallowed("core.reset_langfuse_traces: resetting Langfuse traces failed")
    return deleted


# Per-scan auth tokens for the worker pool. With REDIS_URL set they live in Redis
# with a short TTL — SHARED across replicas, so a scan enqueued on one replica is
# processable by a worker on another (enables horizontal scaling). Without it they
# live in process memory (single replica). Either way tokens are NEVER written to
# Postgres; Redis is transient (TTL + no persistence). A job carries only scan_id.
_TOKEN_TTL = 3600                         # GIS tokens live ~1h and don't refresh
SCAN_TOKENS: dict[str, dict] = {}          # in-memory fallback
REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None


class SharedTokenStoreUnavailable(RuntimeError):
    """Shared token storage is unavailable; admission or queued work should retry."""


def _reset_token_redis() -> None:
    """Drop a failed client so the bounded retry opens a fresh connection."""
    global _redis
    failed, _redis = _redis, None
    try:
        if failed is not None:
            failed.close()
    except Exception:
        swallowed("core._reset_token_redis: closing the failed Redis client failed")


def _get_redis():
    global _redis
    if not REDIS_URL:
        return None
    if _redis is None:
        import redis
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                      socket_timeout=3, socket_connect_timeout=3)
    return _redis


def redis_dependency_status() -> dict:
    """Sanitised Redis readiness and topology for deployment safety.

    Never returns a hostname, credential, or URL.  ``topology`` is intentionally coarse: it lets
    operations distinguish the production-grade managed target from a single self-hosted cache
    without publishing connection details through the public readiness endpoint.
    """
    if not REDIS_URL:
        return {"configured": False, "reachable": None, "tls": None,
                "topology": "unconfigured"}
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(REDIS_URL)
        hostname = (parsed.hostname or "").lower()
        topology = "managed" if hostname.endswith((
            ".redis.azure.net", ".redis.cache.windows.net",
            ".redisenterprise.cache.azure.net")) else "self_hosted"
        tls = parsed.scheme.lower() == "rediss"
    except Exception:
        topology, tls = "unknown", None
    try:
        client = _get_redis()
        reachable = bool(client and client.ping())
        reason = None if reachable else "ping did not succeed"
    except Exception as exc:  # no endpoint or credential detail in the response
        reachable = False
        reason = f"{exc.__class__.__name__}: Redis unavailable"
    return {"configured": True, "reachable": reachable, "tls": tls,
            "topology": topology, "reason": reason}


def register_scan_tokens(scan_id: str, *, drive: str | None = None, sp: str | None = None,
                         require_shared: bool = False) -> None:
    # Refreshing one provider must not erase the other provider's still-live credential. This
    # matters now that the SharePoint keep-alive updates a completed scan throughout Release.
    toks = dict(get_scan_tokens(scan_id))
    if drive:
        toks["drive"] = drive
    if sp:
        toks["sp"] = sp
    if not (drive or sp):
        return
    if REDIS_URL:
        import json as _j
        for attempt in range(2):
            try:
                r = _get_redis()
                if r is not None and r.set(f"scantok:{scan_id}", _j.dumps(toks), ex=_TOKEN_TTL):
                    return
            except Exception:
                swallowed("core.register_scan_tokens: registering the scan tokens in Redis failed", scan_id)
            _reset_token_redis()
            if attempt == 0:
                _time.sleep(0.05)
        if require_shared:
            raise SharedTokenStoreUnavailable(
                "shared credential store unavailable; the scan was not started"
            )
        # Compatibility for non-queued callers: keep the credential usable by this process.
    SCAN_TOKENS[scan_id] = toks


def get_scan_tokens(scan_id: str) -> dict:
    shared_read_failed = False
    if REDIS_URL:
        import json as _j
        for attempt in range(2):
            try:
                r = _get_redis()
                v = r.get(f"scantok:{scan_id}") if r is not None else None
                if v:
                    return _j.loads(v)
                shared_read_failed = False
                break
            except Exception:
                shared_read_failed = True
                swallowed("core.get_scan_tokens: reading the scan tokens from Redis failed", scan_id)
                _reset_token_redis()
                if attempt == 0:
                    _time.sleep(0.05)
    local = SCAN_TOKENS.get(scan_id, {})
    if shared_read_failed and not local:
        # A worker on another replica has no local token mirror. Returning {} here
        # would misreport this cache outage as an expired provider session, stopping
        # delivery rather than using the durable queue's bounded transient retries.
        raise SharedTokenStoreUnavailable('shared token store temporarily unavailable')
    return local


def clear_scan_tokens(scan_id: str) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.delete(f"scantok:{scan_id}")
        except Exception:
            swallowed("core.clear_scan_tokens: clearing the scan tokens from Redis failed", scan_id)
    SCAN_TOKENS.pop(scan_id, None)


# ── Threaded-scan progress, readable from any replica ─────────────────────────────────────
# JOBS (the module-level dict above) is written by the in-process scan thread and read by the
# UI's poll. Both used to hit the same dict, which is only true when both land on the SAME
# REPLICA — so acp-app carried ingress session affinity to guarantee it. That affinity is what
# blocks multi-revision mode, and therefore blue-green: ACA refuses
# `ContainerAppInvalidIngressStickySessionRevisionMode`.
#
# Same shape as SCAN_TOKENS directly above: Redis when REDIS_URL is set (it is, in the deployed
# environment, already there for cross-replica scan-token durability), the in-memory dict when it
# is not, so local dev and the test suite are unchanged.
#
# WHAT THIS DOES AND DOES NOT FIX. It makes the PROGRESS READABLE from any replica, which is the
# only thing affinity was buying. The work still runs as a thread on one replica, so it is still
# lost if that replica restarts — exactly as the code comment beside it has always said. NOTE
# (2026-08-21): the comment used to claim the durable queue is the default via `queuedScan`; the
# frontend's actual default is `queuedScan = false` ("session-scoped is the pilot default",
# App.jsx) and the switch to opt into the durable queue was deliberately removed from the UI
# (ScanReviewModal.jsx). So THIS thread-based path is, in practice, the only path a user can
# reach today — which is exactly why a job stuck here needs to fail loudly instead of hanging
# forever (see _JOB_STALE_SECONDS below), not why it's a rare edge case.
_JOB_TTL = 3600                            # a scan poll outlives the scan; nothing needs longer
# Coalesce high-frequency progress ticks: at most one Redis write per window, unless the patch
# carries a phase change, done, or error flag — those always flush immediately.
_JOB_COALESCE_SECONDS = float(os.environ.get("ACP_JOB_COALESCE_SECONDS", "0.5") or "0.5")
_JOB_LAST_REDIS_WRITE: dict[str, float] = {}   # monotonic timestamps, never persisted
# A failed Redis write leaves this replica's in-memory mirror newer than the shared hash. The
# next successful update must repair the whole record rather than only its newest patch.
_JOB_REDIS_DIRTY: set[str] = set()

# scan_id → job_id mapping so scan-based SSE streams can locate the current job without
# the caller having to thread job_id through every API surface.  Written on set_job (when
# scan_id is already in the initial state) and on update_job (when it first appears).
_SCAN_JOB_MAP: dict[str, str] = {}             # in-memory fallback; Redis is canonical

# Durable recovery checkpoint (Redis live-state spec, 2026-08-26): Redis is the fast, frequent
# live source, but it is EPHEMERAL — gone if unreachable, a key TTLs out, or a replica with no
# Redis falls to a JOBS dict no other replica can see. Without a fallback, a caller reading a
# scan's progress with Redis down gets nothing to show at all, not even "last known state, N
# seconds ago". This accumulates the SAME patches update_job already receives (no extra reads —
# scan_id only arrives in the initial set_job call on the durable-queue path, or later via
# update_job on the thread path, so it is cached here rather than re-derived) and flushes to
# Postgres sparsely: on every phase/done transition, and otherwise at most once per
# _CHECKPOINT_INTERVAL_S. That cadence is deliberate — this must stay far below the write volume
# that caused 2026-08-26's Postgres connection exhaustion; it is a recovery aid, not a live feed.
_JOB_SCAN_ID: dict[str, str] = {}              # job_id -> scan_id, for routing checkpoint writes
_JOB_CHECKPOINT_STATE: dict[str, dict] = {}    # job_id -> accumulated patch state
_JOB_LAST_CHECKPOINT: dict[str, float] = {}    # job_id -> monotonic time of last Postgres write
_CHECKPOINT_INTERVAL_S = float(os.environ.get("ACP_CHECKPOINT_INTERVAL_S", "20") or "20")


def _maybe_checkpoint(job_id: str, patch: dict) -> None:
    """Best-effort durable checkpoint — see the module comment above _JOB_SCAN_ID. Never raises:
    a diagnostic write must never fail the job it is describing, and this runs on the same
    worker thread doing the real work."""
    import time as _t
    if patch.get("scan_id"):
        _JOB_SCAN_ID[job_id] = patch["scan_id"]
    scan_id = _JOB_SCAN_ID.get(job_id)
    if not scan_id:
        return
    acc = _JOB_CHECKPOINT_STATE.setdefault(job_id, {})
    acc.update(patch)
    phase_changed = "phase" in patch or "done" in patch or "error" in patch
    now = _t.monotonic()
    last = _JOB_LAST_CHECKPOINT.get(job_id, 0.0)
    if not (phase_changed or now - last >= _CHECKPOINT_INTERVAL_S):
        return
    _JOB_LAST_CHECKPOINT[job_id] = now
    try:
        get_store().checkpoint_scan_progress(scan_id, dict(acc), patch["updated_at"])
    except Exception:
        swallowed("core._maybe_checkpoint: checkpointing scan progress failed")


def _write_scan_job_mapping(scan_id: str, job_id: str) -> None:
    r = _get_redis()
    if r is not None:
        try:
            r.set(f"scan_to_job:{scan_id}", job_id, ex=_JOB_TTL)
            return
        except Exception:
            swallowed("core._write_scan_job_mapping: writing the scan-to-job mapping to Redis "
                      "failed", scan_id)
    _SCAN_JOB_MAP[scan_id] = job_id


def get_job_id_for_scan(scan_id: str) -> str | None:
    """Return the current job_id for a scan (set on the durable queue path when the worker
    claims it, or on the thread path once the scan_id is assigned in update_job)."""
    r = _get_redis()
    if r is not None:
        try:
            return r.get(f"scan_to_job:{scan_id}")
        except Exception:
            swallowed("core.get_job_id_for_scan: reading the job id for this scan from Redis "
                      "failed", scan_id)
    return _SCAN_JOB_MAP.get(scan_id)

# A job stuck on a replica that died (redeploy, crash, OOM) leaves NO trace: the thread that
# would have called update_job() on error or completion is simply gone, so the job sits at
# whatever phase it was last written to, forever, with no error and no timeout. Live incident
# 2026-08-21: a scan queued shortly before a routine merge-triggered redeploy never advanced
# past phase="queued", scan_id=null — Assess correctly reported 0 eligible documents because
# there was, and would always be, no scan to be eligible under. Nothing surfaced that; the
# Discover screen just kept showing "scanning...".
#
# Detected here at READ time, not by a separate sweeper thread/cron: a sweeper is itself just
# another thread that can die with the replica, which is the exact failure mode this exists to
# catch — a mechanism to detect "the replica died" that itself lives on a replica is not a fix.
# A read-time check has no such single point of failure: whichever replica answers the poll
# computes staleness fresh from the shared timestamp, so it works even if every worker replica
# has been recycled since the job was written.
#
# The threshold has to be longer than the gap between liveness signals or a slow-but-alive job
# false-positives as dead. set_job/update_job stamp `updated_at` on EVERY write, and the scan
# thread below (routes/scans.py's `work()`) runs a heartbeat companion thread that touches it
# every _JOB_HEARTBEAT_SECONDS regardless of scan progress — so `updated_at` going stale means
# the REPLICA is gone, not merely that this particular scan is slow. That decouples the staleness
# signal from how long a legitimately large estate takes to crawl.
_JOB_HEARTBEAT_SECONDS = 20
_JOB_STALE_SECONDS = int(os.environ.get("ACP_JOB_STALE_SECONDS", "90") or "90")


def _job_now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _job_is_stale(state: dict) -> bool:
    """True if `state` describes unfinished work with no liveness signal recent enough to trust.
    A MISSING updated_at (a job written before this instrumentation existed, or one whose replica
    died before its first heartbeat) counts as stale, not exempt — that is precisely the case the
    live incident above was.

    phase=='retrying', phase=='reclaimed', and phase=='deployment_requeue' are exempt for the
    same reason 'done' is: all are
    legitimate WAITING states,
    not a stalled one. No worker holds this job while it sits out its backoff — heartbeats
    stop by design — and rate-limit backoff alone can run up to 600s, well past the normal 90s
    staleness window. The exemption is bounded in practice: fail_job's own idempotency guard
    (see worker.py's on_retry call) only ever writes this phase when the job is genuinely
    'queued' for another attempt, and the next real attempt overwrites it with a live phase
    within seconds of being claimed."""
    if state.get("done") or state.get("phase") in (
            "retrying", "reclaimed", "deployment_requeue"):
        return False
    ts = state.get("updated_at")
    if not ts:
        return True
    import datetime as _dt
    try:
        age = (_dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return True                        # unparseable timestamp is as untrustworthy as none
    return age > _JOB_STALE_SECONDS


def set_job(job_id: str, state: dict) -> None:
    """Write a fresh job record, replacing any prior state. Uses a Redis hash (HSET) so
    subsequent update_job calls can atomically patch individual fields without a read-modify-write
    race. seq is initialised to 0; every update_job call increments it, giving the SSE stream a
    cheap change signal without a full diff."""
    state = {**state, "updated_at": _job_now_iso(), "seq": 0}
    _maybe_checkpoint(job_id, state)   # independent of Redis/in-memory below — see its own comment
    # A successful Redis write used to return before populating JOBS. If Redis then failed during
    # update_job, the newer update had no local record to merge into and vanished from both stores.
    JOBS[job_id] = dict(state)
    r = _get_redis()
    if r is not None:
        try:
            import json as _j
            mapping = {k: _j.dumps(v) for k, v in state.items()}
            pipe = r.pipeline()
            pipe.delete(f"job:{job_id}")
            pipe.hset(f"job:{job_id}", mapping=mapping)
            pipe.expire(f"job:{job_id}", _JOB_TTL)
            pipe.execute()
            _JOB_LAST_REDIS_WRITE[job_id] = 0.0   # reset coalesce clock
            _JOB_REDIS_DIRTY.discard(job_id)
            if state.get("scan_id"):
                _write_scan_job_mapping(state["scan_id"], job_id)
            if state.get("done"):
                JOBS.pop(job_id, None)
            return
        except Exception:
            # fall through to in-memory
            _JOB_REDIS_DIRTY.add(job_id)
            swallowed("core.set_job: writing the job state to Redis failed")
            _reset_token_redis()
    if not REDIS_URL:
        import logging as _log
        _log.warning("REDIS_URL not set — job %s state stored in-memory only; "
                     "progress will NOT be visible across replicas", job_id)
    if state.get("scan_id"):
        _write_scan_job_mapping(state["scan_id"], job_id)


def update_job(job_id: str, patch: dict) -> None:
    """Merge patch into the stored job record.

    Redis path uses HSET (atomic per-field write, no read needed) followed by HINCRBY on the seq
    counter — both in a single pipeline. Concurrent heartbeat and progress writes can no longer
    race and overwrite each other's data, unlike the old read-modify-write approach.

    High-frequency ticks (e.g. files_found incrementing during listing) are coalesced: the Redis
    write is skipped when the last write was < _JOB_COALESCE_SECONDS ago AND the patch carries no
    phase/done/error key. Those keys always flush immediately so the UI sees transitions right away.
    The in-memory JOBS dict (same-replica poll fallback) is always updated regardless."""
    import time as _t
    patch = {**patch, "updated_at": _job_now_iso()}
    _maybe_checkpoint(job_id, patch)   # independent of Redis/in-memory below — see its own comment
    local = JOBS.setdefault(job_id, {})
    local.update(patch)
    r = _get_redis()
    if r is not None:
        now = _t.monotonic()
        is_immediate = "phase" in patch or "done" in patch or "error" in patch
        last = _JOB_LAST_REDIS_WRITE.get(job_id, 0.0)
        if is_immediate or now - last >= _JOB_COALESCE_SECONDS:
            try:
                import json as _j
                # After an outage, repair from the complete local mirror. Redis owns `seq`, so
                # never overwrite that cursor with the mirror's older value before HINCRBY.
                outgoing = dict(local) if job_id in _JOB_REDIS_DIRTY else patch
                mapping = {k: _j.dumps(v) for k, v in outgoing.items() if k != "seq"}
                pipe = r.pipeline()
                pipe.hset(f"job:{job_id}", mapping=mapping)
                pipe.hincrby(f"job:{job_id}", "seq", 1)
                pipe.expire(f"job:{job_id}", _JOB_TTL)
                pipe.execute()
                _JOB_LAST_REDIS_WRITE[job_id] = now
                _JOB_REDIS_DIRTY.discard(job_id)
                if patch.get("scan_id"):
                    _write_scan_job_mapping(patch["scan_id"], job_id)
                if local.get("done"):
                    JOBS.pop(job_id, None)
                return
            except Exception:
                _JOB_REDIS_DIRTY.add(job_id)
                swallowed("core.update_job: patching the job state in Redis failed")
                _reset_token_redis()
        else:
            # Coalescing is also an intentionally deferred write. Mark the mirror newer so the
            # next flush carries every suppressed field, not only that later call's patch.
            _JOB_REDIS_DIRTY.add(job_id)
    # scan_id mapping must be written even when the coalesce window suppressed the main write.
    if patch.get("scan_id"):
        _write_scan_job_mapping(patch["scan_id"], job_id)
    # In-memory fallback — either Redis unavailable or coalesce window suppressed the write.
    # Also updates JOBS in the coalesce case so same-replica reads stay current.
    # `local` was updated before the Redis attempt, including when it failed or was coalesced.


def get_job_state(job_id: str, *, reconcile_durable: bool = True) -> dict | None:
    """The poll's answer, with staleness resolved into an honest terminal state rather than left
    for the caller to notice. Never mutates the stored record — every replica computes this fresh,
    so an already-dead job reads the same way from whichever replica answers the next poll.

    Reads from a Redis hash (HGETALL). Falls back to the old JSON-string format during the
    deployment window when string-type keys from pre-HSET instances are still live in Redis.
    Set reconcile_durable=False when the caller owns a bounded-age SQL reading;
    that mode returns progress without SQL fallback or local staleness terminality."""
    r = _get_redis()
    state = None
    if r is not None:
        try:
            import json as _j
            raw = r.hgetall(f"job:{job_id}")
            if raw:
                state = {k: _j.loads(v) for k, v in raw.items()}
        except Exception:
            # Key may be the old JSON-string type (pre-HSET deployment) — try a plain GET.
            try:
                import json as _j
                v = r.get(f"job:{job_id}")
                if v:
                    state = _j.loads(v)
            except Exception:
                swallowed("core.get_job_state: reading the job state from Redis failed")
                _reset_token_redis()
    cache_missing = state is None
    if cache_missing:
        state = JOBS.get(job_id)
    # The shared live cache is optional; durable queue ownership and terminality are not.
    # Another replica cannot see this process's mirror after a Redis outage. Never invent
    # completion from its absence, or let a stale mirror override a durable terminal result.
    if reconcile_durable and REDIS_URL and (cache_missing or _job_is_stale(state) or job_id in _JOB_REDIS_DIRTY):
        try:
            durable = get_store().get_job(job_id)
            if isinstance(durable, dict) and durable.get("status") in (
                    "queued", "running", "done", "dead", "cancelled"):
                status = durable["status"]
                terminal = status in ("done", "dead", "cancelled")
                previous = state or {}
                phase = previous.get("phase")
                if status != "running" or previous.get("done") or phase in (
                        None, "error", "failed", "complete", "done", "cancelled"):
                    phase = {"done": "complete", "dead": "error"}.get(status, status)
                state = {**previous, "job_id": job_id,
                         "phase": phase, "error": None,
                         "scan_id": durable.get("scan_id") or (durable.get("payload") or {}).get("scan_id"),
                         "done": terminal, "updated_at": durable.get("updated_at"),
                         "state_source": "durable_queue"}
                if status == "dead":
                    state["error"] = "The job failed. Check the scan for details."
        except Exception:
            swallowed("core.get_job_state: reading durable queue fallback failed")
    if state is None:
        return None
    if reconcile_durable and _job_is_stale(state) and state.get("state_source") != "durable_queue":
        return {**state, "phase": "error", "done": True,
                "error": (state.get("error") or
                          "scan interrupted — the server likely restarted mid-run; "
                          "please start a new scan")}
    return state


def finalize_scan(scan_id: str, effective_ai: bool, source: str) -> None:
    """Shared post-scan step: audit the run and, in deterministic mode, auto-route
    ai-assisted findings to the HITL queue. Used by both the threaded and queued
    scan paths so they behave identically.

    Finalize-once (ADR 0013): a crash between a fan-out file's bump and completion used to
    re-trigger and double-emit HITL/audit. mark_finalized claims the scan atomically, so
    only the first caller runs this body; duplicate/concurrent scan_finalize jobs no-op."""
    if not get_store().mark_finalized(scan_id):
        return
    get_store().log_decision(
        "system", "scan.completed", scan_id=scan_id,
        detail=f"source={source} mode={'ai-assisted' if effective_ai else 'deterministic'}")
    if not effective_ai:
        created = get_store().queue_hitl_items(scan_id)
        if created:
            fire_webhook(created)
            get_store().log_decision(
                "system", "hitl.auto_routed", scan_id=scan_id,
                detail=f"deterministic mode → {len(created)} ai-assisted findings routed to HITL")


def start_workers() -> int:
    """Spawn the worker pool + a stuck-job sweeper. Pool size = ACP_WORKERS,
    always -- a deploy-time env var must mean what it says. (Previously a
    persisted worker_count setting from a prior live-scale action silently
    overrode ACP_WORKERS on every restart, so setting ACP_WORKERS at deploy
    time had no visible effect once anyone had ever used the +/- live-scale
    buttons.) Live-scaling via the UI still works for the running process; it
    just no longer survives the next restart. No-op when ACP_WORKERS is 0."""
    global WORKERS
    if _worker_handles:
        return len(_worker_handles)
    import threading
    import handlers  # noqa: F401 — registers job handlers with the worker
    WORKERS = max(0, min(WORKERS, _MAX_WORKERS))
    for _ in range(WORKERS):
        _spawn_worker()

    # Always start the sweeper (even at 0 workers) so a later live scale-up is covered.
    #
    # Delegates to sweeper.run_sweep() (ADR 0004 step 5) rather than reimplementing the
    # checks inline. Found live 2026-08-27: run_sweep() already covered four checks —
    # reclaim_stuck_jobs, sweep_exhausted_jobs, sweep_orphaned_scans, rescue_unfinalized_scans
    # — and was fully tested (tests/test_reconciliation_sweeper.py), but nothing in
    # production ever imported it. This inline loop called only two of the four directly,
    # so a queued job past max_attempts was never dead-lettered, and a 'running' scan_runs
    # row with zero outstanding jobs (worker died between fan-out and finalize) was never
    # marked 'interrupted' — both sat silently stuck with no error and no error visible
    # anywhere. Wiring run_sweep() here means the next sweep check added to sweeper.py
    # takes effect in production automatically, not on the next person to remember this
    # loop exists too.
    def _sweep():
        import time as _t
        import sweeper as _sweeper
        import content_workspace_retention as _retention
        import stage_outbox as _stage_outbox
        ticks = 0
        while True:
            try:
                # 30-min lease: scans of large estates legitimately run ~10-15min, so
                # reclaim only clearly-dead jobs. The worker heartbeat (best-effort)
                # extends this further; this is the reliable floor if it can't. Grace
                # window for orphaned-scan detection uses sweeper.py's own env-driven
                # default (ACP_SWEEP_GRACE_S, 600s) — no prior production value to match.
                _sweeper.run_sweep(get_store(), lease_seconds=1800)
                # ADR 0044 / PRD §28: same thread, same "wire it in or it never runs in
                # production" lesson this loop's own history already taught (see the comment
                # above this function) — a separate, untested-in-production thread is exactly
                # how a capability sits fully built and never actually fires.
                _retention.run_content_workspace_retention_sweep(get_store())
                # The shared jobs table is ACP's production transport. Acknowledge canonical
                # outbox messages only after their job/work-item identity is verified there;
                # failures remain retryable and visible in Live Operations.
                _stage_outbox.dispatch_database_jobs_once(
                    get_store(), dispatcher_id=worker_process_instance_id("outbox"), limit=200)
                _run_stage_execution_backfill_once()
                ticks += 1
                if ticks % 60 == 0:      # ~hourly: trim old completed jobs so the jobs
                    d = get_store().purge_done_jobs(older_than_hours=24)   # table + claim index don't bloat (audit P2)
                    if d:
                        print(f"[sweeper] purged {d} old done job(s)", flush=True)
            except Exception as e:
                print(f"[sweeper] error: {e}", flush=True)
            _t.sleep(60)
    threading.Thread(target=_sweep, daemon=True, name="jobsweeper").start()
    return WORKERS
