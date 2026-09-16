"""acp control-plane API — application entrypoint.

Thin assembly layer: creates the FastAPI app, installs the access-gate
middleware, includes the route modules (routes/*.py), and mounts the built SPA.
All shared state and helpers live in core.py; the endpoints live in routes/.

Run:  uvicorn app:app --host 0.0.0.0 --port 8077

Endpoint groups (see routes/):
  system  — /healthz /config /schedule /hub
  rubric  — /rubric /rules
  scans   — /scans* /inventory + per-file remediation
  drive   — /me /sources /folders /drive/upload
  hitl    — /hitl/queue*
  ai      — /ai/explain /ai/status
"""
from __future__ import annotations
import base64
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

import core
import store as _store
from readiness_phase_diagnostics import ReadinessPhaseMiddleware
from routes import ROUTERS

logger = logging.getLogger(__name__)

app = FastAPI(title="acp — accessibility compliance API", version="0.1.0")
app.add_middleware(ReadinessPhaseMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Acp-Auth", "Content-Disposition"])


@app.middleware("http")
async def _reserve_database_capacity_for_mutations(request, call_next):
    """Keep read bursts from consuming every local connection needed by a user decision.

    Only explicitly mutating HTTP methods receive the reserved slot. Background/default callers
    share the ordinary gate, so forgetting to classify work fails safe instead of stealing the
    capacity needed to record a user decision.
    """
    token = _store.DB_MUTATION_REQUEST.set(
        request.method.upper() not in {"GET", "HEAD", "OPTIONS"})
    try:
        return await call_next(request)
    finally:
        _store.DB_MUTATION_REQUEST.reset(token)

# Not every environment has the Postgres driver installed (store.py's SQLite path doesn't need
# it, and some dev boxes never install it) — guard the import so this module still loads there.
# When it IS present (every real deploy: api/requirements.txt pins psycopg2-binary), register a
# handler for the exact failure mode of the 2026-08-30 incident.
try:
    import psycopg2.pool as _pg_pool
except ImportError:  # pragma: no cover — SQLite-only dev environment, no Postgres driver at all
    _pg_pool = None

if _pg_pool is not None:
    @app.exception_handler(_pg_pool.PoolError)
    async def _db_pool_exhausted(request, exc):
        """store.py's _getconn() retries a burst for a few seconds (api/store.py ~line 1156)
        but still raises psycopg2.pool.PoolError once that window elapses — previously an
        unhandled 500 with no distinguishable body, which is what POST /discovery/preflight and
        POST /scans both did during the 2026-08-30 pool-exhaustion incident. _getconn() fails
        BEFORE acquiring a connection, i.e. before any query in THIS cursor() call runs — so for
        the common case (pool exhaustion on the first DB touch of a request) nothing from this
        request was written. A handler that already committed an earlier, separate cursor() call
        before a LATER one hits this is the one case that statement doesn't cover; the response
        is intentionally still framed as advice ("try again"), not a hard guarantee, for exactly
        that reason.

        Response shape is a stable contract the frontend detects to replace the generic
        "scan failed: 500" copy with something legible.

        WHAT THE MESSAGE MAY CLAIM depends on the request, and this is the part that was wrong.
        The body used to say "No changes were made" for every route. That statement is provable
        only when nothing in the request could have written before the pool gave out — true of a
        read, and NOT true of a mutating request, because an earlier cursor() in the same handler
        may already have committed. The docstring above conceded exactly that case in prose while
        the message asserted the opposite in the user's face.

        It is not theoretical. POST /scans commits enqueue_scan (scan_runs + jobs + scan_inputs)
        and then calls scan_event() to record scan.queued — another database write, after the
        scan is durable. A PoolError there returns this 503 with the scan genuinely created, and
        "No changes were made" would be a lie that invites the user to submit a second one.

        So the claim is scoped to what the method can prove:

          * safe method (GET/HEAD/OPTIONS) -> changes="none". Nothing was written; say so.
          * anything else                  -> changes="unknown". Say that plainly and point at
                                              reconciliation rather than at a retry.

        The client's half of this already exists: submitIntent.outcomeIsUncertain() treats a 503
        as uncertain and RETAINS the submit intent's idempotency key, so a retry after this
        response resolves to the job that may already exist instead of creating a duplicate.
        `changes` makes that reasoning explicit in the payload rather than implicit in the status
        code, and `code` is the stable identifier to branch on — `detail` is kept unchanged for
        the callers already reading it."""
        import uuid as _uuid
        from datetime import datetime as _dt, timezone as _tz
        safe = request.method.upper() in {"GET", "HEAD", "OPTIONS"}
        request_id = (request.headers.get("x-request-id")
                      or _uuid.uuid4().hex[:12])
        if safe:
            message = ("ACP's database was temporarily at capacity, so this could not be read. "
                       "No changes were made. Try again shortly.")
        else:
            message = ("ACP's database was temporarily at capacity. We could not confirm whether "
                       "your request completed — check its status before submitting it again.")
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5", "X-Request-Id": request_id},
            content={
                "detail": "database_busy",          # unchanged: existing callers branch on this
                "code": "DB_CAPACITY_BUSY",         # stable identifier for new callers
                "message": message,
                "changes": "none" if safe else "unknown",
                "request_id": request_id,
                "occurred_at": _dt.now(_tz.utc).isoformat(),
            },
        )


# ── workspace-role enforcement (PRD §11) ──────────────────────────────────────
#
# REGISTERED BEFORE _access_gate, WHICH IS WHAT MAKES IT RUN AFTER IT, and the inversion is
# worth stating because I got it backwards first and the tests caught it. Starlette's
# add_middleware INSERTS at the head of the list and the head is the OUTERMOST layer, so the
# LAST middleware registered is the FIRST to see a request. Defining this above _access_gate
# therefore puts the access gate outside it: auth runs first, stamps request.state.user_email,
# and only then does this read it.
#
# Registered the other way round, this gate sees no identity at all and answers 403 ("you lack
# permission") to every request including the owner's — where the truth is 401 ("we do not know
# who you are"). PRD §11 asks for exactly that distinction, and getting it backwards tells an
# expired session it has been demoted. The symptom is not subtle once you look, and it is
# invisible if you only test handlers: the middleware is not in that path at all.
#
# ONE ENFORCEMENT POINT, not 236. See api/workspace_capability_map.py for why the mapping is a
# table and how tests/test_capability_map_is_complete.py makes "100% of routes mapped" (PRD §18)
# a checkable claim rather than an assertion.
#
# COST, STATED PLAINLY: from the `observe` rung upward this resolves the caller's role per
# request, which is a settings read (the people record) plus a role read. There is deliberately NO
# cache — PRD §9 requires a changed role to take effect on the user's NEXT request, and any TTL is
# a window in which a revoked permission still works. At `off` the middleware returns before
# touching the store at all, so the default path pays nothing.
#
# Whether that per-request cost is acceptable under real load was left open in slice 4 and is now
# ANSWERABLE rather than answered: `observe` pays exactly the same cost as `enforce` while changing
# nobody's access, so a workspace measures it on its own traffic before anything depends on it.
# That is a second reason to run the rung, beyond the permission evidence it was added for.
@app.middleware("http")
async def _workspace_capability_gate(request, call_next):
    import workspace_capability_map as capmap
    import workspace_rollout as rollout
    import workspace_roles as wr

    route = core.match_registered_route(request.scope.get("path", ""), request.method)
    if route is None:
        return await call_next(request)          # not an API route we know; the gate above owns it
    needed = capmap.required_capabilities(request.method, route.path)
    if not needed:
        return await call_next(request)          # exempt by design — capmap says why

    # Public/exempt routes and static assets do not need a role lookup. For
    # protected routes a durable-mode read failure must refuse the action, while
    # preserving the existing retryable database-capacity response.
    try:
        roles_resolved = rollout.roles_resolved()
    except Exception as exc:
        pool_module = globals().get("_pg_pool")
        if pool_module is not None and isinstance(exc, pool_module.PoolError):
            return await _db_pool_exhausted(request, exc)
        return JSONResponse(status_code=503, content={
            "detail": "role_permissions_unavailable",
            "message": "Role permissions could not be checked. Please try again.",
        }, headers={"Retry-After": "5"})
    if not roles_resolved:
        return await call_next(request)

    email = getattr(request.state, "user_email", None)
    access = wr.access_for_email(core.store, email, owner_email=core.OWNER_EMAIL,
                                 is_suspended=_capability_gate_suspended)

    # BELOW `navigation`, `access` IS TODAY'S ACCESS AND ANSWERS THE WRONG QUESTION. It reports
    # what the caller has now — everything, because OPEN_ACCESS — and `calculated` is what their
    # role would grant. Reading the first would make observe mode measure nothing at all: the
    # check below would always pass, no would-deny row would ever be written, and the rollout
    # would report a clean run because it never asked the question. A monitor that cannot go
    # wrong is not a monitor.
    #
    # Above that rung there is no `calculated` — the decision IS the access — so this collapses
    # to `access` and the enforcing path is unchanged. The owner has no `calculated` at any rung,
    # which is the carve-out doing its job: they cannot be measured into a denial either.
    effective = access.get("calculated") or access
    held = frozenset(effective.get("capabilities") or ())
    if capmap.allows(request.method, route.path, held):
        return await call_next(request)

    # 403, not 404. PRD §11 reserves 404 for "confirming another tenant's object exists would
    # disclose information" — which is a per-OBJECT decision the routes already make via their
    # owner-scoped reads (get_scan(..., owner=...) answers 404 for a foreign scan). This gate is
    # about the CAPABILITY, and a role that lacks it is not being told about anyone else's data
    # by being told it lacks it. Conflating the two would make every permission error look like a
    # missing page, which is unactionable for the user and unloggable for an operator.
    # The role enforcement WOULD use, which below `navigation` is not necessarily the assigned
    # one — an unassigned person resolves to the default Platform User, and naming their assigned
    # role (none) in the row would misreport why they were about to be refused.
    role = (effective.get("role") or access.get("role") or {}).get("name")

    # BELOW `enforce`, THE REQUEST STILL SUCCEEDS — and that is a decision, not a gap. PRD §15
    # step 1: "Calculate roles but preserve current access; log differences." This is the
    # difference, and logging it is the only way the rollout is ever evidence-based rather than
    # hopeful: a capability map that is subtly wrong shows up here as real people hitting real
    # routes, days before it could show up as a 403 during someone's workday.
    #
    # `navigation` records too, even though the SPA is already hiding the tab. What reaches this
    # point at that rung is precisely what the hiding did NOT cover — a direct URL, a stale open
    # tab, a background poll from a page loaded before the role changed — which is the exact list
    # an operator needs before making the server refuse.
    if not rollout.enforcement_active():
        _record_denial(email, needed, role=role, method=request.method, path=route.path,
                       prospective=True)
        return await call_next(request)

    detail = (f"your {role} role does not include this action" if role
              else "you have no workspace role, so this action is not available")
    _record_denial(email, needed, role=role, method=request.method, path=route.path)
    return Response(status_code=403, media_type="application/json",
                    content=json.dumps({"detail": detail,
                                        "required": sorted(needed), "capability_denied": True}))


def _record_denial(email, needed, *, role, method, path, prospective: bool = False) -> None:
    """PRD §12's `role.access_denied`, coalesced — see api/workspace_denials.py for why writing
    one row per refusal is the wrong shape.

    `prospective` writes `role.access_would_deny` instead: observe/navigation mode, where the call
    was ALLOWED and this records that enforcement would have stopped it. Two action names rather
    than one with a flag inside the text, because an operator counting refusals must not have to
    parse prose to find out which rows were real — and because the count of would-denies is the
    number the decision to advance a rung actually turns on.

    BEST-EFFORT, AND THAT IS THE POINT OF THE try/except. The refusal has already been decided;
    this only records it. If the audit write fails — the database is busy, the pool is exhausted —
    the user must still get their 403. An exception escaping here would turn a correct denial into
    a 500, which reads to the client as "the server broke" rather than "you may not do this", and
    would make the gate itself the outage.
    """
    try:
        import workspace_denials as denials
        kind = "would_deny" if prospective else "denied"
        record, suppressed = denials.should_record(email, needed, kind=kind)
        if not record:
            return
        core.store.log_decision(
            (email or "anonymous"),
            "role.access_would_deny" if prospective else "role.access_denied",
            detail=denials.detail(email, needed, role=role, method=method, path=path,
                                  suppressed=suppressed, prospective=prospective))
    except Exception:
        from swallowed import swallowed
        swallowed("app._record_denial: recording a capability denial failed", path)


def _capability_gate_suspended(email: str) -> bool:
    """PRD §14 — a suspended user has no effective permissions. Read from the managed-person
    record, the same source routes/system.py uses, so the two cannot disagree about who is
    suspended."""
    return core.suspended_in_store(email)


@app.middleware("http")
async def _access_gate(request, call_next):
    """Public-deploy access gate. With ACP_ACCESS_CODE set, every non-public request
    needs HTTP Basic auth matching it; with ACP_GOOGLE_CLIENT_ID set, every non-public
    request needs a valid GIS Bearer token for an allowed domain. No-op when neither
    is set (local dev). X-E2E-Key bypasses the gate for smoke tests when ACP_E2E_KEY
    is configured."""
    if core.E2E_KEY and request.headers.get("x-e2e-key") == core.E2E_KEY:
        return await call_next(request)
    if core.is_public(request.url.path):
        return await call_next(request)
    if core.ACCESS_CODE:
        ok = False
        hdr = request.headers.get("authorization", "")
        if hdr.startswith("Basic "):
            try:
                ok = base64.b64decode(hdr[6:]).decode().split(":", 1)[1] == core.ACCESS_CODE
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="acp"'})
    elif core.GOOGLE_CLIENT_ID:
        # X-Acp-Auth: session marks a GATE 401 — the access gate itself rejected the request
        # because the session's bearer is absent or invalid. The SPA signs the user out only on
        # this; a route that returns 401 for its own reason (an integration not connected) must
        # NOT eject an authenticated user. Found 2026-08-11: /sources 401'ing a Microsoft user who
        # has no Google Drive bounced the whole session and cleared the bearer.
        # This middleware is outside Starlette's CORS wrapper and can return before that wrapper
        # adds headers. Include the configured wildcard response headers here so a frontend on a
        # separate origin can both receive the 401 and read its session-expiry marker.
        _GATE_401 = {"X-Acp-Auth": "session", "Access-Control-Allow-Origin": "*",
                     "Access-Control-Expose-Headers": "X-Acp-Auth"}
        hdr = request.headers.get("authorization", "")
        if not hdr.startswith("Bearer "):
            return Response(status_code=401, media_type="application/json", headers=_GATE_401,
                            content='{"detail":"Sign in required"}')
        # The SPA tags Microsoft sign-ins with X-Auth-Provider so we verify against the right
        # issuer (Graph) rather than trying Google first and paying a failed round-trip on every
        # Microsoft request. Absent the header we default to Google — the original behaviour.
        provider = request.headers.get("x-auth-provider", "").lower()
        if provider == "microsoft":
            email = core.verify_ms_token(hdr[7:])
        else:
            email = core.verify_gis_token(hdr[7:])
        if not email:
            return Response(status_code=401, media_type="application/json", headers=_GATE_401,
                            content='{"detail":"Session expired — sign in again"}')
        if not core.email_allowed(email):
            # TWO DIFFERENT REFUSALS, and telling them apart costs nothing and saves a support
            # ticket. "Access restricted to authorized accounts" is true of somebody who was never
            # admitted; said to a colleague an administrator suspended this morning it is simply
            # wrong, and it reads as a bug in the product rather than as the deliberate act it is.
            #
            # No enumeration risk in the difference: this branch is reached only AFTER the bearer
            # verified, so the caller has already proved they own the address. The most anyone can
            # learn here is the state of their own account, which is the thing we want them to
            # learn. The second store read is on the refusal path only.
            detail = ("Your access has been suspended — contact your administrator."
                      if core.is_suspended(email) else
                      "Access restricted to authorized accounts")
            return Response(status_code=403, media_type="application/json",
                            content=json.dumps({"detail": detail}))
        request.state.user_email = email   # so routes can attribute the scan (Langfuse user)
        # JUST-IN-TIME ROSTER (owner decision, 2026-09-05). This is the only point in the codebase
        # where "somebody authenticated successfully" is known, so it is where a person first
        # becomes visible to an administrator. Everything expensive is behind a process-local memo
        # in core; the steady-state cost here is one set lookup.
        #
        # SWALLOWED ON PURPOSE, and this is the one judgement in the whole feature worth arguing
        # about. Roster bookkeeping must never be able to 500 a request that authentication has
        # already approved — an unwritable store would otherwise take the whole product down for
        # everyone at the domain, to enforce a record nobody is reading yet. The next request
        # tries again.
        try:
            core.note_signed_in(email, provider=provider or "google")
        except Exception:
            core.swallowed("app: just-in-time roster creation failed")
    return await call_next(request)


for _router in ROUTERS:
    app.include_router(_router)

from routes.activity_event_evidence import router as _activity_evidence_router
app.include_router(_activity_evidence_router)

# Feeds the fail-closed access gate the real, registered route table (core.py cannot import
# `app` itself without a cycle — app.py imports core, not the reverse). Placed at module level,
# immediately after every router is included, so there is no window where a real request could
# be served before this runs: Python finishes importing app.py (and only then does uvicorn start
# accepting connections) before any request reaches the middleware below. See core.py's
# register_protected_routes/is_public docstrings for why this replaced a manually-maintained
# prefix allowlist that silently missed five route groups over five weeks.
core.register_protected_routes(core.enumerate_api_routes(app))

_embedded_worker_reporter = None


@app.on_event("startup")
def _start_job_workers():
    """Open the database, arm and start the scheduler, then start the workers — in that order.

    Nothing here happens at import any more. core.store is lazy (built on first use), so the
    database is opened and the schema/allowlist bootstrap runs here rather than being paid for
    by the first HTTP request — and a boot-time failure surfaces at startup instead of as a 500
    on some route. reload_scheduler() reads the schedule out of that store, so it must follow
    it; the scheduler thread then starts with its jobs already pending."""
    core.get_store()
    _announce_isolation_mode()
    # Tracing first, so the scheduler and worker spans below are captured from the first tick
    # rather than from whenever the first HTTP request happened to arrive. A no-op without
    # APPLICATIONINSIGHTS_CONNECTION_STRING — see api/telemetry.py.
    try:
        import telemetry as _telemetry  # noqa: PLC0415
        from datetime import datetime as _now_dt, timezone as _now_tz  # noqa: PLC0415
        _state = _telemetry.configure(now_iso=_now_dt.now(_now_tz.utc).isoformat())
        if _state.get("enabled"):
            print(f"[telemetry] Application Insights on · sampling {_state['sampling_ratio']} "
                  f"· correlation {_state['correlation']}", flush=True)
    except Exception:  # noqa: BLE001 — never take startup down for telemetry.
        swallowed("app.startup: configuring Application Insights tracing failed")
    # PRD §15/§20.12: the deployment result belongs in the audit trail. Without it a fresh
    # installation's audit log is EMPTY, and "no events" cannot be told apart from "audit logging
    # does not work" — the one ambiguity an auditor cannot afford. Deduped on (version, commit)
    # inside the store, so replicas and a crash loop do not bury the deployments they record.
    #
    # Fail-open, like every other startup side effect here: an installation that cannot write its
    # own audit row must still serve traffic, and the alternative is a boot loop over bookkeeping.
    try:
        from routes.system import _build_info as _bi  # noqa: PLC0415
        _info = _bi()
        _recorded = core.store.record_deployment_audit(
            version=str(_info.get("version") or "dev"),
            commit=_info.get("commit"),
            # Passed through as provenance, never compared: ADR 0048 forbids BRANCHING on the
            # platform, and assigning before the `if` keeps this call out of a condition.
            platform=os.environ.get("ACP_PLATFORM", "unknown"),
            profile=os.environ.get("ACP_DEPLOY_PROFILE", "unknown"))
        if _recorded:
            print(f"[audit] deployment recorded: {_info.get('version')}", flush=True)
    except Exception:  # noqa: BLE001 — never take startup down for an audit row.
        swallowed("app.startup: recording the deployment audit event failed")
    core.reload_scheduler()
    core.start_scheduler()
    # Overrides are Azure policy changes, not merely labels in Settings. The reconciler is
    # deliberately armed only with the same fail-closed gateway used by the admin apply route.
    import capacity_reconcile as _capacity_reconcile
    from routes import control as _capacity_control
    _capacity_reconcile.start(core.store, _capacity_control._capacity_apply_gateway)
    n = core.start_workers()
    if n:
        global _embedded_worker_reporter
        from worker_telemetry import WorkerInstanceReporter
        _embedded_worker_reporter = WorkerInstanceReporter(core)
        _embedded_worker_reporter.start()
        print(f"[acp] started {n} async job worker(s)", flush=True)


def _announce_isolation_mode() -> None:
    """Say out loud whether per-user data isolation is ON. It is the right behaviour in a demo
    and a serious misconfiguration for a tenant, and until now the two were indistinguishable.

    `_owner()` reads request.state.user_email, which only the `elif GOOGLE_CLIENT_ID` branch of
    the access gate sets. So with ACCESS_CODE set — or with neither configured — every user
    resolves to the single owner 'demo' and shares one estate: one person's remediated documents
    are readable by anyone else who can sign in. For a hospital that is patient-record
    disclosure, not a preference.

    Two ways to arrive here without meaning to, both silent before this:
      * ACCESS_CODE and GOOGLE_CLIENT_ID are an if/ELIF. Setting an access code on a deployment
        that HAS Google configured does not add a second factor — it takes the first branch and
        stops user_email being stamped at all, turning isolation off as a side effect.
      * deploy/compose/docker-compose.yml defaults ACP_GOOGLE_CLIENT_ID to empty, so a
        customer-VPC install from that file runs shared-estate unless someone supplies it.

    Printed, not raised. Refusing to boot would be the fail-closed choice for production and is
    worth considering, but it can lock out a running deployment, so that stays a deliberate
    decision rather than a side effect of this change.
    """
    isolated = bool(core.GOOGLE_CLIENT_ID) and not core.ACCESS_CODE
    if isolated:
        print("[acp] per-user data isolation: ON (owner = verified sign-in email)", flush=True)
        return
    why = ("ACP_ACCESS_CODE is set, so the gate never stamps a user email"
           if core.ACCESS_CODE else "no ACP_GOOGLE_CLIENT_ID is configured")
    print(f"[acp] *** per-user data isolation is OFF — {why}. Every user shares the 'demo' "
          f"estate and can read every other user's scans and remediated files. Correct for a "
          f"demo; NOT safe for multi-user or PHI data.{' PRODUCTION.' if core.IS_PROD else ''} "
          f"***", flush=True)


@app.on_event("shutdown")
def _drain_job_workers():
    # Azure Container Apps sends SIGTERM then waits (~30s) before SIGKILL on every
    # deploy/scale event; uvicorn runs this hook in that window. Draining here means
    # in-flight jobs aren't stranded ~31min waiting for the lease sweeper on the new
    # container (audit P1).
    try:
        if _embedded_worker_reporter is not None:
            _embedded_worker_reporter.draining()
        core.stop_workers()
        if _embedded_worker_reporter is not None:
            _embedded_worker_reporter.stop()
            _embedded_worker_reporter.offline()
        print("[acp] drained job workers for shutdown", flush=True)
    except Exception as e:
        print(f"[acp] shutdown drain error: {e}", flush=True)
    # The scheduler owns a thread of its own; stop it in the same window rather than leaving
    # uvicorn to be killed with it still running. Independent of the drain above: a failed
    # drain must not leave the scheduler thread alive.
    try:
        import capacity_reconcile as _capacity_reconcile
        _capacity_reconcile.stop()
        core.stop_scheduler()
    except Exception as e:
        print(f"[acp] scheduler shutdown error: {e}", flush=True)


def _ollama_keepalive_ping() -> None:
    """Best-effort Ollama keep-alive request with an operator-friendly timeout log.

    A timeout is expected while the scale-to-zero service wakes.  Report that bounded
    condition without a traceback; retain the traceback-bearing ``swallowed`` path for
    unexpected failures so real defects remain diagnosable.
    """
    import httpx

    import ai as _ai
    try:
        httpx.post(f"{_ai.OLLAMA_BASE_URL}/api/generate",
                   json={"model": _ai.OLLAMA_MODEL, "prompt": " ",
                         "keep_alive": "30m", "options": {"num_predict": 1}},
                   headers=_ai._OLLAMA_HEADERS,
                   timeout=60)
    except httpx.TimeoutException:
        logger.warning("event=ollama.keepalive_timeout timeout_seconds=60 action=ignored")
    except Exception:
        swallowed("app._ollama_keepalive_ping: the Ollama keep-alive ping failed")


@app.on_event("startup")
def _ollama_prewarm():
    """Keep the Ollama model loaded so 'Why?' explanations don't eat a cold start
    (the model load alone is ~15s on CPU). Best-effort daemon: pings every 10 min
    with keep_alive=30m; a missing/unreachable Ollama just makes each ping a no-op.
    Opt out with ACP_OLLAMA_KEEPALIVE=0."""
    if os.environ.get("ACP_OLLAMA_KEEPALIVE", "1") == "0":
        return
    import threading
    import time

    def _loop():
        while True:
            _ollama_keepalive_ping()
            time.sleep(600)

    threading.Thread(target=_loop, daemon=True, name="ollama-prewarm").start()


from fastapi.staticfiles import StaticFiles
from swallowed import swallowed


class SpaStaticFiles(StaticFiles):
    """StaticFiles that sets the Cache-Control the SPA actually needs.

    Plain StaticFiles ships an ETag but NO Cache-Control, which lets a browser serve a stale
    index.html without revalidating — so after a deploy a client keeps loading the OLD, hashed JS
    bundle named in that cached HTML. That is not hypothetical: on 2026-08-10 it stranded a
    signed-in Microsoft user on pre-fix JS that never sent the X-Auth-Provider header, so every
    request 401'd ("session expired") against a backend that was, by then, perfectly able to
    authenticate them. Only a manual hard-refresh cleared it — untenable for a rollout.

    The entry HTML must be re-checked every load (it is tiny, and its ETag makes an unchanged fetch
    a cheap 304); the content-hashed /assets/* can be cached forever, because a new build gives a
    new filename rather than mutating an old one.
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache"          # revalidate via ETag every load
        elif path.startswith("assets/") or "/assets/" in path:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


# Serve the built React SPA same-origin in the deploy container (ACP_STATIC_DIR
# points at the vite `dist`). Registered last so all /api routes take precedence;
# unset locally (the SPA runs on the vite dev server instead).
_static = os.environ.get("ACP_STATIC_DIR")
if _static and Path(_static).is_dir():
    app.mount("/", SpaStaticFiles(directory=_static, html=True), name="spa")
