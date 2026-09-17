"""Route modules for the acp control plane.

Each module exposes a `router` (fastapi.APIRouter). app.py includes them all.
Shared state and helpers live in api/core.py.
"""
from . import system, rubric, scans, drive, hitl, ai, disposition, campaigns, capability, sharepoint, control, costs, assess, scope, analytics, public, discovery, openapi_health, workspace, content_workspaces, acr, workspace_roles_admin, lifecycle_archive, realtime, stage_executions, remediation_policy, remediation_waterfall, release_continuation, automatic_release, release_folders, ai_run_approval, report_render, change_review

ROUTERS = [system.router, rubric.router, scans.router, drive.router, hitl.router, ai.router,
          disposition.router, campaigns.router, capability.router, sharepoint.router,
          control.router, costs.router, assess.router, scope.router, analytics.router, public.router,
          discovery.router, openapi_health.router, workspace.router, content_workspaces.router,
          acr.router, workspace_roles_admin.router, lifecycle_archive.router, realtime.router,
          stage_executions.router, remediation_policy.router, remediation_waterfall.router, release_continuation.router, automatic_release.router, release_folders.router, ai_run_approval.router,
          report_render.router, change_review.router]
