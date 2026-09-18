import { authEpoch } from './apiIdentity.js'
import ReleaseReviewWorkspace from './ReleaseReviewWorkspace.jsx'
import { reviewBadgeTitle } from './remediationCountSummary.js'
import { prepareWorkflowEntry } from './workflowEntry.js'
import { useEffect, useState, useMemo, useCallback, useRef, lazy, Suspense } from 'react'
import HitlBell from './HitlBell.jsx'
import { assessmentLine, outcomeChips } from './assessmentProgress.js'
import { queuedProgress } from './queuedProgress.js'
import { nextFallbackInterval } from './fallbackPollBackoff.js'
import { acceptLiveJobState } from './liveJobStateGuard.js'
import { preflightVerdict } from './discoveryPreflightGate.js'
import { scanPollDecision } from './scanPollDecision.js'
import { scanFailureDetail, hasFallbackInventory } from './scanFailureMessage.js'
import LiveAssessmentLive from './LiveAssessmentLive.jsx'
import RemediationRunCard from './RemediationRunCard.jsx'
import { useRemediationRun } from './useRemediationRun.js'
import useReleaseReadinessRefresh from './useReleaseReadinessRefresh.js'
import WorkflowStageStack from './WorkflowStageStack.jsx'
import { canonicalWorkflowStages, currentCanonicalStage } from './canonicalStageCard.js'
import { useCanonicalStageLineage } from './useCanonicalStageLineage.js'
import { armNotifyOnComplete, notifyScanComplete, notifyScanFailed, notificationsSupported, notifyPermission } from './scanNotify.js'
import { refreshDriveToken } from './driveAuth.js'
import { refreshSPToken } from './spAuth.js'
import PrivateAiBadge from './PrivateAiBadge.jsx'
import { getSources, getRubric, getConfig, getMe, getMyAccess, getMyScope, getCapability, listScans, getScan, getStageLineage, NOT_MODIFIED, getActiveScan, getWorkspaceBootstrap, getActiveWorkflows, startScan, startScanQueued, cancelScan, getJob, setDriveToken, setSPToken, setGoogleToken, setMsToken, clearAllTokens, getDecisions, saveDecisionsBatch, refreshScanDriveToken, refreshScanSPToken, clearScanTokens, getScanLocations, remediateScan, SESSION_EXPIRED, SCAN_UNAVAILABLE, checkHealth, openDiscoverStream, checkDiscoveryPreflight } from './api'
import { beginOrResumeIntent, completeIntent, abandonIntent, outcomeIsUncertain } from './submitIntent'
import { SIM } from './sim.js'
import { setPersona, recommendFor } from './sim.js'
import { ACCOUNT_MENU_DISMISS_MS, useAutoDismissDetails } from './a11y.js'
import { loadDelegations } from './OwnerDelegate.jsx'
import { loadRolePrivileges } from './RolePrivilege.jsx'
import { loadFileTypeConfig, visibleForFileTypes } from './FileTypeConfig.jsx'
import { annotate, loadPublished } from './ontology.js'
import { RuleBreakdown } from './Transparency.jsx'
import Logo from './Logo.jsx'
import ChatWidget from './ChatWidget.jsx'
import VersionToast from './VersionToast.jsx'
import RealtimeShadowPanel from './RealtimeShadowPanel.jsx'
import WorkflowContinuityBanner, { primaryActiveWorkflow } from './WorkflowContinuityBanner.jsx'
import DiscoveryContinuityChoice from './DiscoveryContinuityChoice.jsx'
import { StageStartConflictDialog } from './StageStartConflictDialog.jsx'
// Lazy: KnowledgeGraph statically imports all of d3 (~250 kB min) — the only heavy
// dep not already behind a dynamic import. Loading it on tab entry keeps d3 out of
// the main chunk entirely.
const KnowledgeGraph = lazy(() => import('./KnowledgeGraph.jsx'))
const AdminLiveTraffic = lazy(() => import('./AdminLiveTraffic.jsx'))
const LiveOperationsNotifier = lazy(() => import('./LiveOperationsNotifier.jsx'))
import SignIn from './SignIn.jsx'
import FindingEvidenceViewer from './FindingEvidenceViewer.jsx'
import { captureEvidenceTarget, forgetEvidenceTarget, parseEvidenceHref, withoutEvidenceParams } from './evidenceLink.js'
import Settings from './Settings.jsx'
import MyDataDialog from './MyDataDialog.jsx'
import Monitor from './Monitor.jsx'
import QueuePanel from './QueuePanel.jsx'
import Publish from './Publish.jsx'
import Overview from './Overview.jsx'
import AssessRunner from './AssessRunner.jsx'
import AssessSetup from './AssessSetup.jsx'
import AssessFileFindings from './AssessFileFindings.jsx'
import { inventorySnapshot } from './discoverRunTime.js'
import { scanOptionAt, scanWorkflowContext } from './scanOptionDate.js'
import AssessSummary from './AssessSummary.jsx'
import AssessRunIntegrity, { useScanManifest } from './AssessRunIntegrity.jsx'
import { runIntegrity, integrityCaveat } from './runIntegrity.js'
import AssessWorklist from './AssessWorklist.jsx'
import { documentRows } from './assessMetrics.js'
import RunDetails from './RunDetails.jsx'
import Integrations from './Integrations.jsx'
import Discover from './Discover.jsx'
import { priorStageResults, guardStageAction } from './priorStageResults.js'
import DiscoverRunProgress from './DiscoverRunProgress.jsx'
// Dashboard import removed — component retired from Assess tab (kept on disk)
import { CAPABILITY_FALLBACK, ASSESSMENT_FALLBACK, fmtOf } from './capability.js'
import Remediate from './Remediate.jsx'
import EmptyState, { Loading } from './EmptyState.jsx'
import OverviewPreviewCard from './OverviewPreviewCard.jsx'
import AssessPreviewCard from './AssessPreviewCard.jsx'
import { markLoad, logLoadSummary } from './loadPerf.js'
import MonitorPreviewCard from './MonitorPreviewCard.jsx'
import { isActiveJobStale } from './activeJobStaleness.js'
import ScanReviewModal from './ScanReviewModal.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import { applyScopeConfig } from './activeScope.js'
import A11ySelfCheck from './A11ySelfCheck.jsx'
import { scanPhaseLine, NARRATION_STEPS, activityLine } from './phaseNarration.js'
import { useScanRefetch } from './scanRefetch.js'
import ConfirmDialog from './ConfirmDialog.jsx'
import { AdminInsights } from './AdminInsights.jsx'
import AcrWorkspace from './AcrWorkspace.jsx'
import AccessRestricted from './AccessRestricted.jsx'
import { visibleTabs, isVisible, isAccessPending, canOperate, firstPermittedTab, canOpenSettings, mergeBootstrapIdentity } from './access.js'
import { deliveryAccess } from './deliveryAccess.js'
import { timezoneBadge } from './userTimezone.js'
import { handleWorkflowTabKeyDown } from './workflowTabs.js'
import { isHistoricalScan, narrowScanDefaultContext } from './defaultScan.js'

// Self-scan overlay: on in dev, or on the deployed demo via ?a11y
const SHOW_A11Y = import.meta.env.DEV || (typeof location !== 'undefined' && new URLSearchParams(location.search).has('a11y'))

// step=0 → utility tab (no number); step>0 → workflow step with numbered badge
const TABS = [
  ['overview',      'Overview',      'at a glance',         0],
  ['integrations',  'Sources',       'connect sources',     0],
  ['discover',      'Discover',      'inventory · classify', 1],
  ['assess',        'Assess',        'score vs WCAG',       2],
  ['remediate',     'Remediate',     'fix issues',          3],
  ['publish',       'Release',       'approve & deploy',    4],
  ['monitor',       'Monitor',       'track compliance',    5],
  ['liveops',       'Live Operations', 'Azure traffic',     0],
  ['analytics',     'Scan Analytics', 'compare scans',      0],
  ['graph',         'Knowledge Graph', 'explore findings',   0],
  // ADR 0047 — ACP's OWN conformance against WCAG 2.2 A/AA, in VPAT form. Step 0 (not part of the
  // numbered Discover→Monitor flow) because it is not about a customer's estate at all: the other
  // tabs assess the documents ACP processes, this one assesses ACP.
  ['acr',           'Conformance',   'ACR / VPAT',          0],
]

// TEMPORARY PRODUCT POLICY (2026-09-04): every signed-in user can navigate the complete
// workspace. API authorization remains authoritative for privileged mutations; in particular,
// making these views discoverable must not turn a read-only user into a platform administrator.
const ALL_TAB_KEYS = TABS.map(([key]) => key)


function timeAgo(iso) {
  if (!iso) return null
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 60) return 'just now'
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

function fmtStamp(iso) {
  if (!iso) return null
  // timeZoneName: 'short' stamps the viewer's zone (e.g. "PDT" / "CDT") so cross-timezone
  // viewers (you in PT, a teammate in CT) can tell at a glance it's THEIR local time.
  return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' })
}

function progressText(p) {
  if (!p) return ''
  // The two long, per-file phases now show the OUTCOME-oriented line — how much is done (with saved
  // results), how fast, how long left — instead of "Reading files · 145/250 · one-filename". Multiple
  // files are processed at once, so a single in-flight filename is not the honest signal; the count is
  // (see assessmentProgress.js). The earlier phases keep their plain status label.
  if ((p.phase === 'reading' || p.phase === 'analysing') && p.files_found) {
    return assessmentLine(p)
  }
  const m = {
    queued: 'Queued…', connecting: 'Connecting to source…', discovering: 'Discovering files…',
    reading: `Reading files · ${p.files_done}/${p.files_found}`,
    tagging: 'mova Agent classifying & tagging documents…',
    analysing: `Analysing documents · ${p.files_done}/${p.files_found}`,
    // NB: 'scoring' here is the scan compiling per-document results, NOT the WCAG
    // assessment — that runs on the Assess tab. Label it as such to avoid conflation.
    scoring: 'Compiling results…', done: 'Complete', error: 'Error',
  }
  let s = m[p.phase] ?? p.phase
  // Queued scans don't stream per-file progress, so reassure the user it's alive
  // by showing elapsed time on the long worker-pool phase.
  if (p.elapsed != null) s += ` · still working (${p.elapsed}s)`
  return s
}

// Overall scan progress (0–100) across every phase, so the bar only reaches 100%
// when the scan is actually done — not when the read phase finishes (tagging,
// analysing and scoring still follow). The read phase spans 12→84%, scaled by the
// real per-file count; the post-read phases fill the remainder.
const PHASE_PCT = { queued: 2, connecting: 5, discovering: 9, reading: 12, tagging: 88, analysing: 92, scoring: 97, done: 100, error: 100 }

// Semantic colours for the live outcome chips (passed / need-review / failed / processing). Kept as a
// small style map so the chip row needs no new CSS and reads the same in light contexts.
const OCHIP_STYLE = {
  ok: { color: '#2F7D51', background: '#E6F2EA' },
  warn: { color: '#9A6011', background: '#F7EDDB' },
  bad: { color: '#A5314A', background: '#F9E7EB' },
  muted: { color: '#54636F', background: '#ECEFF4' },
}


// Group landed files by format — returns [{fmt, count}] sorted descending, max 5 formats.
// Used in the format breakdown strip under the outcome chips.
function formatBreakdown(files) {
  if (!files || files.length === 0) return []
  const counts = {}
  for (const f of files) {
    const fmt = (fmtOf(f) || 'other').toUpperCase()
    counts[fmt] = (counts[fmt] || 0) + 1
  }
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)
    .map(([fmt, count]) => ({ fmt, count }))
}

// Failed-by-format breakdown: only files with status==='error', same grouping.
function failedByFormat(files) {
  if (!files || files.length === 0) return []
  return formatBreakdown(files.filter((f) => f.status === 'error'))
}

// Count of landed files that have at least one finding (status === 'uncertain').
function findingsSoFar(files) {
  if (!files) return 0
  return files.filter((f) => f.status === 'uncertain').length
}

function progressPct(p) {
  if (!p) return 0
  if (typeof p.pct === 'number') return p.pct   // explicit (e.g. queued per-file count)
  if (p.phase === 'reading' && p.files_found) {
    const frac = Math.min(1, (p.files_done || 0) / p.files_found)
    return Math.round(12 + frac * (84 - 12))
  }
  return PHASE_PCT[p.phase] ?? 6
}

// Honest progress for a queued/fan-out scan, derived from the scan row's REAL per-file counter
// (scan_runs.files_done / files, bumped as each file's per-file job lands) — never a timer or a
// synthetic decay curve. While files remain, the estate is being analysed; once every file is in,
// the finalize pass (per-document score + estate aggregate) runs. `current` is deliberately left
// unset: the fan-out analyses files in parallel across workers, so no single "file N is in flight
// right now" is knowable — the truthful signal is the count, not a made-up filename.
// Shown on results views (Overview / Dashboard / Monitor) until the user runs Assess —
// scores, trends and dashboards only appear after an explicit assessment.
function AssessGate({ onGo }) {
  return (
    <section className="panel" style={{ textAlign: 'center', padding: '52px 24px' }}>
      <div style={{ fontSize: 30, marginBottom: 10 }}>📋</div>
      <h2 style={{ margin: '0 0 6px' }}>Run the assessment to see results</h2>
      <p className="muted" style={{ maxWidth: 480, margin: '0 auto 20px', lineHeight: 1.55 }}>
        This estate has been discovered but not yet assessed against WCAG 2.1. Running <b>Assess</b>
        opens each file and scores it; compliance scores, trends and dashboards appear here once it
        completes.
      </p>
      <button onClick={onGo}>Go to Assess →</button>
    </section>
  )
}

// Per-user/per-session "fresh start": wipe activity caches so no scan results, published
// files, assess/remediation progress, scores, or upload history from a prior session or
// user survive. Config (roles, file-types, delegations) is intentionally left alone.
const ACTIVITY_LS = ['mova_ontology_v1', 'mova_drive_scores', 'mova_drive_archive', 'mova_upload_history']
function clearActivityStorage() {
  try {
    Object.keys(sessionStorage).filter((k) => k.startsWith('acp-')).forEach((k) => sessionStorage.removeItem(k))
    ACTIVITY_LS.forEach((k) => localStorage.removeItem(k))
  } catch { /* storage unavailable — ignore */ }
}

// Reconnecting an already-connected source must refresh durable workflow credentials now.
// Epoch and effect cleanup fence owner changes, logout, and superseded reconnect attempts.
export function useSharePointWorkflowKeepalive({ hasSPToken, owner, reconnectRevision,
  setActiveWorkflows, setTokenRefreshError }) {
  useEffect(() => {
    if (!hasSPToken || !owner) return
    let alive = true
    const epoch = authEpoch()
    const current = () => alive && authEpoch() === epoch
    const refresh = async () => {
      try {
        const response = await getActiveWorkflows()
        if (!current()) return
        const workflows = response?.active_workflows || []
        setActiveWorkflows(workflows)
        let ids = [...new Set(workflows
          .filter((workflow) => workflow?.source === 'sharepoint' && workflow?.scan_id)
          .map((workflow) => workflow.scan_id))]
        // Rolling-deploy compatibility when the older API omits Release workflows.
        if (!ids.length) {
          const active = await getActiveScan()
          if (active?.id) ids = [active.id]
        }
        if (!ids.length || !current()) return
        const tok = await refreshSPToken({ interactive: false, persist: false })
        if (!current()) return
        sessionStorage.setItem('sp_token', tok)
        setSPToken(tok)
        await Promise.all(ids.map((id) => refreshScanSPToken(id)))
        if (current()) setTokenRefreshError(null)
      } catch {
        if (current()) setTokenRefreshError('SharePoint session may have expired — remaining scan or release files are paused until you re-sign in to SharePoint.')
      }
    }
    refresh()
    const iv = setInterval(refresh, 20 * 60 * 1000)
    return () => { alive = false; clearInterval(iv) }
  }, [hasSPToken, owner, reconnectRevision, setActiveWorkflows, setTokenRefreshError])
}

// A report's finding link (evidenceLink.js): `/?view=evidence&scan=…&file=…&finding=…`. Read ONCE
// per page load, before sign-in, so the target survives the SignIn screen (and, through
// sessionStorage, a redirect sign-in that returns to a bare "/").
const evidenceStorage = () => { try { return window.sessionStorage } catch { return null } }
function initialEvidenceTarget() {
  if (typeof window === 'undefined') return null
  const { target, restoredHref } = captureEvidenceTarget({ search: window.location.search, storage: evidenceStorage() })
  if (restoredHref) {
    try { window.history.replaceState(window.history.state, '', `${restoredHref}${window.location.hash}`) } catch { /* address bar only */ }
  }
  return target
}

export default function App() {
  const [me, setMe] = useState(null)
  const [evidenceTarget, setEvidenceTarget] = useState(initialEvidenceTarget)
  // Signed in with the target on screen: the address bar carries it now, so the sign-in copy in
  // storage has done its job and must not outlive it.
  useEffect(() => { if (me && evidenceTarget) forgetEvidenceTarget(evidenceStorage()) }, [me, evidenceTarget])
  const closeEvidence = () => {
    forgetEvidenceTarget(evidenceStorage())
    try { window.history.replaceState(window.history.state, '', withoutEvidenceParams(window.location.href)) } catch { /* address bar only */ }
    setEvidenceTarget(null)
  }
  // From one evidence view to another (a document's list → one of its records, and back): the URL
  // moves with the view, so the address bar always names what is on screen and Back returns to it.
  const openEvidence = (href) => {
    const next = parseEvidenceHref(href)
    if (!next) return
    try { window.history.pushState(window.history.state, '', href) } catch { /* address bar only */ }
    setEvidenceTarget(next)
  }
  useEffect(() => {
    const onPop = () => setEvidenceTarget(parseEvidenceHref(window.location.search || ''))
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  const [userTimezone, setUserTimezone] = useState('America/Chicago')
  // Why the user is looking at the sign-in screen. null on a first visit; set when a 401
  // bounced them out mid-session, so SignIn can say so rather than appear for no reason.
  const [signedOutReason, setSignedOutReason] = useState(null)
  const [rubric, setRubric] = useState(null)
  const [sources, setSources] = useState([])
  const [scan, setScan] = useState(null)
  const [justAssessed, setJustAssessed] = useState(null) // scan id assessed this session (optimistic)
  const [assessmentActivity, setAssessmentActivity] = useState(null)
  const [assessPhase, setAssessPhase] = useState('idle') // idle | starting | running | done

  // The two capability tables `AssessSummary` counts over. Held HERE rather than fetched inside
  // the summary so the component stays pure — its own test forbids it deriving anything, because a
  // component that recomputes is how a fifth denominator arrives.
  //
  // The fallbacks are the synchronous default, exactly as AssessRunner uses them: they mirror the
  // Python tables verbatim and are CI-locked to them, so the numbers are right before the fetch
  // resolves and right after. A failed fetch leaves the fallback in place rather than emptying the
  // map — an empty capability map would read every criterion as "no method", which is the same
  // false verdict as a zero.
  // Run details is a sub-view of Assess, not a tab. It holds the capability reference, the
  // "no method" explanation and the scan traces — reachable from the summary, returned from, and
  // deliberately absent from the primary navigation, which is where engineering diagnostics were
  // not supposed to be.
  const [runDetails, setRunDetails] = useState(false)
  // The worklist ROW a reader opened, for the findings-by-criterion view. It holds the row rather
  // than the file so the view renders the same derivation the list was ordered by.
  //
  // This used to open FileDrawer, with a note saying the dedicated view "replaces this when it
  // lands". It landed in #546 and sat unmounted; that note is now history rather than a plan.
  const [assessFile, setAssessFile] = useState(null)
  // AssessRunner still owns the run; AssessSetup owns the button that starts it. The runner hands
  // its start function here on mount. Stable identity via useCallback, so registering does not
  // re-fire on every render of this very large component.
  const priorResultsRef = useRef({})
  const assessStart = useRef(null)
  const registerAssessStart = useCallback((fn) => { assessStart.current = fn }, [])
  // Hide any prior completed dashboard in the SAME click that starts the new run. AssessRunner
  // also reports `starting` synchronously for its internal re-run paths; this wrapper covers the
  // separate AssessSetup button owned by App.
  const startAssessment = useCallback((decided) => {
    if (priorResultsRef.current.assess) return
    setAssessPhase('starting'); setRunDetails(false); setAssessFile(null)
    assessStart.current?.(decided)
  }, [])
  // A28 · bulk-fix the deterministic findings in a worklist selection. Calls the SAME endpoint
  // (remediateScan with an explicit scope) R3's "Apply N automatic fixes" already uses on the
  // Remediate tab — this is a second entry point into proven, tested infrastructure, not a new
  // code path. Enqueues, then hands off to Remediate to watch progress there rather than
  // duplicating its polling machinery here.
  //
  // Takes scanId as a CALL-TIME argument rather than closing over `run` — `run` is derived at
  // line ~822, after this component's only early return (`if (!me) return <SignIn/>`), so a hook
  // declared here cannot reference it without the same "rendered more hooks than previous render"
  // crash the assessFileNext memo hit (see App.jsx history). The JSX call site, which IS past that
  // return, supplies run.id explicitly instead.
  const [bulkFixBusy, setBulkFixBusy] = useState(false)
  const handleBulkFix = useCallback(async (scanId, rows) => {
    if (!scanId || bulkFixBusy || !rows?.length || priorResultsRef.current.assess || scanId !== priorResultsRef.current.scanId) return
    setBulkFixBusy(true); setErr(null)
    try {
      const r = await remediateScan(scanId, rows.map((row) => row.file))
      if (!r.enqueued) {
        setErr(`Nothing to remediate — the server found no eligible work in the ${rows.length} `
          + `document${rows.length === 1 ? '' : 's'} sent. They may already have been remediated `
          + `elsewhere; re-open Assess to refresh.`)
      } else {
        setView('remediate'); window.scrollTo({ top: 0, behavior: 'smooth' })
      }
    } catch (e) {
      setErr(`Bulk fix failed: ${e?.message ?? e}`)
    } finally {
      setBulkFixBusy(false)
    }
  }, [bulkFixBusy])
  const [cap, setCap] = useState(CAPABILITY_FALLBACK)
  const [assessment, setAssessment] = useState(ASSESSMENT_FALLBACK)
  useEffect(() => {
    let on = true
    getCapability().then((r) => {
      if (!on) return
      if (r?.capability) setCap(r.capability)
      if (r?.assessment) setAssessment(r.assessment)
    }).catch(() => { /* the fallbacks stand */ })
    return () => { on = false }
  }, [])
  // The account card names the same per-user timezone that Release uses for destination folder
  // names. Keep the documented Central default on a failed read; the browser timezone does not
  // control this setting and would be a misleading fallback.
  useEffect(() => {
    if (!me) return
    let alive = true
    getMyScope().then((value) => {
      if (alive) setUserTimezone(value?.release_timezone || 'America/Chicago')
    }).catch(() => {})
    return () => { alive = false }
  }, [me])
  const [scanLoading, setScanLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const isStaging = window.location.hostname.includes('staging')
  const [err, setErr] = useState(null)
  // A submit that never got confirmed. Deliberately NOT `err`: "scan failed" is a claim we cannot
  // make here. The request was bounded and the response lost, so the scan may or may not exist —
  // and the retry is safe either way because the idempotency key is held (submitIntent.js).
  const [submitUncertain, setSubmitUncertain] = useState(null)
  const [discoveryChoice, setDiscoveryChoice] = useState(null)
  // Capacity state from the last preflight check — drives the notice near the scan action.
  // null = no check run yet (first visit); cleared when a new scan starts successfully.
  const [preflightCapacityState, setPreflightCapacityState] = useState(null)
  // A scan the user stopped on purpose. Deliberately NOT `err`: "scan failed: …" over someone's own
  // Stop press is a wrong account of what happened, and the red treatment sends them looking for a
  // fault that does not exist.
  const [stopped, setStopped] = useState(null)
  const [view, setViewState] = useState('overview')
  const setView = (next) => {
    prepareWorkflowEntry(next)
    setViewState(next)
  }
  // Whether the user has CHOSEN the tab they are on. 'overview' is the app's own default, so a
  // role that hides it must move them rather than showing an Access restricted screen for a place
  // they never asked to be — see AccessRestricted.jsx for why an explicit navigation gets the
  // screen instead of a silent bounce. A ref, not state: nothing renders from it, and making it
  // state would re-render the whole workspace on the first tab click.
  const viewWasChosen = useRef(false)
  const goToView = (next) => { viewWasChosen.current = true; setView(next) }
  // The server's answer to "which tabs may this user see, and what may they do inside them".
  // `null` until bootstrap answers, and null means NOT TOLD — everything renders. The refusal
  // that matters is the server's; see the header of access.js for why this direction is right
  // here and the opposite direction is right there.
  const [access, setAccess] = useState(null)
  const [activeWorkflows, setActiveWorkflows] = useState([])
  const primaryWorkflow = useMemo(() => primaryActiveWorkflow(activeWorkflows), [activeWorkflows])

  // PRD §10 — the app's own default view is 'overview'. A role that hides it must move the user
  // on rather than greeting them with Access restricted for a tab they never chose. Only the
  // default: once they have picked a tab, an inaccessible one gets the screen, which explains
  // itself instead of silently bouncing them somewhere they did not ask to go.
  useEffect(() => {
    if (!access?.enforced || viewWasChosen.current) return
    if (isVisible(access, view)) return
    const target = firstPermittedTab(access, TABS)
    if (target && target !== view) setView(target)
  }, [access, view])

  // PRD §9 — a role changed by an administrator must reach an open session without a sign-out.
  // On focus rather than on a timer: the moment somebody comes back to the tab is when they are
  // about to act on it, and a poll would spend the shared API budget on sessions nobody is
  // looking at. A failed refresh leaves the previous answer in place (getMyAccess resolves null
  // on error, and null would mean "not told" — so it is only applied when it is real), because a
  // network blip is not a permission decision.
  useEffect(() => {
    if (!access) return
    const refresh = () => {
      if (document.visibilityState === 'hidden') return
      getMyAccess().then((next) => { if (next) setAccess(next) }).catch(() => {})
    }
    window.addEventListener('focus', refresh)
    window.addEventListener('acp-access-changed', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.removeEventListener('focus', refresh)
      window.removeEventListener('acp-access-changed', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [access])
  // A pending "open this source's history" redirect from Discover's completion card (the "See
  // what's changed since your last scan of this source" link) to Integrations' SourceDrawer —
  // the raw scan-source string (run.source), not a source object; Integrations does its own
  // sourceKeys() matching, the same lookup SourceDrawer's own data already relies on. Cleared by
  // Integrations once it has consumed it, so switching away and back to Discover's card doesn't
  // replay a stale redirect.
  const [pendingSourceOpen, setPendingSourceOpen] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [triage, setTriage] = useState({})              // per-scan triage, lifted from Remediate for time-travel
  const [assignees, setAssignees] = useState({})        // per-file assignee ({file: email}) for the "Assigned to me" inbox filter (#417 backend)
  const savedDecRef = useRef({ scanId: null, decisions: {}, triage: {}, assignees: {} })  // last-persisted snapshot
  const hydratingRef = useRef(false)                    // suppress the save effect during hydration
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [myDataOpen, setMyDataOpen] = useState(false)
  const accountMenuRef = useRef(null)
  useAutoDismissDetails(accountMenuRef, ACCOUNT_MENU_DISMISS_MS)
  const [scanList, setScanList] = useState([])
  // true only when the user explicitly picked an older scan from the time-travel picker —
  // distinguishes "user went back in time" from "a new scan arrived while they were reading".
  const [explicitTimeTravel, setExplicitTimeTravel] = useState(false)
  const [delta, setDelta] = useState(null)
  const [deltaKey, setDeltaKey] = useState(0)
  const [progress, setProgress] = useState(null)
  const [loaded, setLoaded] = useState(false)
  // Which step of the initial-load chain is in flight, so the loading screen can say something
  // more specific than a static "Loading your workspace…" the whole way through — see the
  // effect below and Loading()'s own comment (EmptyState.jsx) for what each value means.
  const [loadStage, setLoadStage] = useState(null)
  // The boot chain refused. Distinct from "no data": a failed /workspace/bootstrap used to be
  // swallowed by an empty .catch, after which .finally flipped `loaded` and the user was shown
  // EmptyState — "No assessment has run yet" — for an estate that might hold a thousand documents.
  // A request that failed and an account with nothing in it are different facts and no longer share
  // a screen. `bootAttempt` re-runs the effect, so the retry is a real re-read, not a page reload.
  const [bootError, setBootError] = useState(null)
  const [bootAttempt, setBootAttempt] = useState(0)
  // GET /workspace/bootstrap's cached Overview snapshot for the default scan, set as soon as
  // that one request resolves — before getScan's full file/finding payload arrives. Only
  // consumed by the loading screen's preview line (Loading preview prop, EmptyState.jsx); every
  // other view still renders from `run`/`files` once those load, unchanged.
  const [overviewPreview, setOverviewPreview] = useState(null)
  // Externally-certified documents, read by Publish. NOTHING WRITES THIS ANY MORE: its only writer
  // was the ad-hoc single-file upload panel on Discover, removed on request — and that was the
  // app's only mount of Upload, so there is no other route to it.
  //
  // Kept rather than deleted. Publish consumes it and handles an empty list (`certified = []` is
  // its default), so the effect is that externally-certified documents simply never appear there —
  // a consequence of removing upload, not a fault. Keeping the state means restoring the panel is
  // one commit; deleting it would widen a removal into a refactor.
  //
  // The SETTER is still bound because the workspace reset at `resetWorkspace` clears it along with
  // everything else. Dropping it from this destructure made that call a ReferenceError — caught
  // here rather than at runtime, which is the whole reason to look at who else touches a symbol
  // before narrowing it.
  const [certifiedDocs, setCertifiedDocs] = useState([])
  const [publishedFiles, setPublishedFiles] = useState([])
  const [hasDriveToken, setHasDriveToken] = useState(() => !!sessionStorage.getItem('gd_token'))

  // ADR 0014 — keep a long-running scan's Drive token fresh. GIS access tokens expire ~1h;
  // while a scan is running we silently re-mint one every 20min and push it to the backend
  // so scans that outlast the token don't 401 on their tail. Best-effort; no-op without GIS.
  useEffect(() => {
    if (!hasDriveToken) return
    let alive = true
    const iv = setInterval(async () => {
      try {
        const response = await getActiveWorkflows()
        let ids = [...new Set((response?.active_workflows || [])
          .filter(workflow => workflow?.source === 'drive' && workflow?.scan_id)
          .map(workflow => workflow.scan_id))]
        if (!ids.length) {
          const active = await getActiveScan()
          if (active?.id) ids = [active.id]
        }
        if (!ids.length || !alive) return
        const token = await refreshDriveToken()
        if (!alive) return
        setDriveToken(token)
        await Promise.all(ids.map(id => refreshScanDriveToken(id)))
        setTokenRefreshError(null)
      } catch { setTokenRefreshError('Google Drive session may have expired — files added since then may be skipped. Reconnect Drive to continue.') }
    }, 20 * 60 * 1000)
    return () => { alive = false; clearInterval(iv) }
  }, [hasDriveToken])
  const [hasSPToken, setHasSPToken] = useState(() => !!sessionStorage.getItem('sp_token'))
  const [tokenRefreshError, setTokenRefreshError] = useState(null)

  const [spReconnectRevision, setSPReconnectRevision] = useState(0)
  useSharePointWorkflowKeepalive({ hasSPToken, owner: me?.email, reconnectRevision: spReconnectRevision,
    setActiveWorkflows, setTokenRefreshError })
  const [delegations, setDelegations] = useState(loadDelegations)
  const [fileTypeConfig, setFileTypeConfig] = useState(loadFileTypeConfig)
  const [rolePrivileges, setRolePrivileges] = useState(loadRolePrivileges)
  const [ontology, setOntology] = useState(loadPublished)
  const [aiEnabled, setAiEnabled] = useState(false)
  const [wcagMode, setWcagMode] = useState(() => {
    try { return localStorage.getItem('acp-wcag-mode') === 'on' } catch { return false }
  })
  useEffect(() => {
    try { localStorage.setItem('acp-wcag-mode', wcagMode ? 'on' : 'off') } catch {}
    document.documentElement.dataset.wcag = wcagMode ? 'on' : ''
  }, [wcagMode])
  const [hitlCount, setHitlCount] = useState(0)  // pending HITL items, reported up from Remediate for the nav badge

  // The remediation run's live state, held at App level so the persistent card survives a tab
  // change — `<Remediate/>` is mounted only on its own tab, so anything it owns dies on a switch.
  //
  // POSITION IS LOAD-BEARING, TWICE. It reads `scan?.run?.id` rather than the `run` const derived
  // further down, because that const is in the temporal dead zone up here. And it must sit ABOVE
  // the `if (!me) return <SignIn/>` early return below: a hook after a conditional return is
  // called on some renders and not others, which is "Rendered more hooks than during the previous
  // render" and takes the whole app down. Both were caught by the full suite rather than by any
  // test of this card.
  // Bootstrap already carries the owner-scoped active workflow before the full scan payload
  // finishes loading. Use that id for active Remediation so its persistent card can connect
  // immediately after sign-in/reload instead of waiting behind the estate-sized getScan.
  const activeRemediationScanId = primaryWorkflow?.stage === 'remediate'
    ? primaryWorkflow.scan_id : null
  const remRun = useRemediationRun(activeRemediationScanId || scan?.run?.id || null)
  useReleaseReadinessRefresh({ runId: scan?.run?.id, surface: view,
    enabled: Boolean(me && ['remediate', 'publish'].includes(view) && !isHistoricalScan(scanList, scan?.run?.id)),
    snapshot: remRun.snapshot, events: remRun.events,
    onScan: next => setScan(current => current?.run?.id === next.run.id ? next : current) })
  const canonicalRun = useCanonicalStageLineage(primaryWorkflow?.scan_id || scan?.run?.id || null,
    getStageLineage)
  const canonicalStage = currentCanonicalStage(canonicalRun.lineage)
  const priorResults = priorStageResults(canonicalRun.lineage, scan?.run?.id, {
    awaitingLineage: Boolean(scan?.run?.completed_at && canonicalRun.lineage?.scan_id !== scan?.run?.id),
  })
  priorResultsRef.current = { ...priorResults, scanId: scan?.run?.id }
  const remediationStage = canonicalWorkflowStages(canonicalRun.lineage).find(stage => stage.stage === 'remediate')
  const remediationProgressHostId = view === 'remediate' && isVisible(access, 'remediate') && remediationStage?.execution_id
    ? `remediation-document-progress-${remediationStage.execution_id}` : null
  const canonicalScanId = canonicalStage?.scan_id || canonicalRun.lineage?.scan_id || null
  // Durable (background queue) is the default (2026-08-21). The session-scoped path runs as a
  // bare in-process thread with no queue behind it — the code's own comment on it has always said
  // "lost if that replica restarts", and this app auto-deploys on every merge to main, so that was
  // not a rare edge case: a scan interrupted mid-crawl by a routine redeploy left phase="queued"
  // forever with no error (fixed to fail loud in #607, but failing loud is still failing — the
  // scan itself is gone either way). The durable path survives the exact same redeploy because the
  // job lives in scan_runs / the worker queue, not in a thread that dies with its replica.
  // Confirmed live via GET /readyz before flipping this: workers.alive=true, can_run_scans=true —
  // this is not pointing the default at unconfirmed infrastructure. The switch that used to expose
  // this choice was intentionally removed from the UI (ScanReviewModal.jsx) — this default IS
  // that "operator through configuration" lever now, not a placeholder for a future removed one.
  const [queuedScan, setQueuedScan] = useState(true)
  const [deepScan, setDeepScan] = useState(false)      // off by default → Fast scan; opt in to PII scan via the switch
  const [excludeRemediated, setExcludeRemediated] = useState(true)  // on by default — skip re-discovering ACP's own Remediated/ output
  // ADR 0011 skips re-analysing byte-identical files already scored under the same rubric. OFF by
  // default from 2026-08-19: every switch in this group now starts off, so the scan a user gets
  // without touching anything is the plainest one — nothing skipped, nothing inferred, and the
  // four toggles read as additions to a known baseline rather than as a mix whose starting state
  // has to be checked. Opting IN to the skip is the deliberate act; it is a speed and cost
  // optimisation, and a stale score kept by a skip is harder to notice than a slow scan.
  //
  // `exclude_remediated` deliberately stays ON and is NOT part of this: off, ACP re-discovers its
  // own Remediated/ output as source documents, which inflates the file count and shows
  // "remediated ✓" on a scan that remediated nothing (provenance.py). That is a wrong number
  // pointing at MORE coverage, which is the direction nobody checks.
  const [incremental, setIncremental] = useState(false)
  // The in-flight durable scan's id — what the banner's Stop button cancels. null when
  // no queued scan is being polled (sync scans finish in-request and can't be stopped).
  const [liveScanId, setLiveScanId] = useState(null)
  // Which scan Monitor should highlight, set by a "View in Monitor →" click from
  // Discover/Assess (stakeholder UX review, 2026-08-30) so the click lands pointed at the run it
  // came from instead of Monitor's unfiltered landing page. null means no focus — the ordinary,
  // unfiltered Monitor-tab-click case. Cleared from QueuePanel's "Show all" via onClearFocus,
  // threaded through <Monitor>.
  const [monitorFocusScanId, setMonitorFocusScanId] = useState(null)
  // The in-flight discover job's id — lets Discover poll GET /jobs/{id} (the durable SQL queue
  // row, with a real locked_at claim timestamp) the same way AssessRunner already polls its own
  // job, to tell "queued, nobody's claimed it" from "a worker claimed it Ns ago and is opening
  // the source" — a real signal that existed already but nothing on Discover ever read.
  const [discoverJobId, setDiscoverJobId] = useState(null)
  // Did the user press Stop for the run currently being polled? A ref, not state, for the same
  // reason notifyArmedRef is: the poll loop below runs inside a closure captured at scan start and
  // would never see a state update. Reset at the top of every run, so a stop never leaks into the
  // next scan. See scanPollDecision.js for why the loop cannot infer this from the scan itself —
  // a queued scan that is cancelled simply stops existing, and absence is what it looked like all
  // along.
  const scanCancelledRef = useRef(false)
  // Set once, on unmount, and read by the job poll loop below.
  //
  // WHY THIS IS NEEDED AND WHAT IT IS NOT. `_pollScanJobPolling` is a `do…while (!job.done)` over
  // `getJob` with a 350ms sleep and NO other stop condition — not even scanCancelledRef, which
  // only the queued-scan loop consults. So an App that unmounts while a job is still running kept
  // issuing a request every 350ms until that job finished on the server, for a component that no
  // longer exists.
  //
  // This is about the REQUESTS, not about setState. React 18 removed the "state update on an
  // unmounted component" warning because such an update is a documented no-op; guarding the
  // setters would buy nothing. The network traffic and the timer are the real cost, and they are
  // what this stops.
  const unmountedRef = useRef(false)
  useEffect(() => () => { unmountedRef.current = true }, [])
  // Live job state pushed by the Discover SSE stream (openDiscoverStream), read by the queued-
  // scan poll loop instead of it separately fetching getJob() every tick — a ref, not state,
  // because it updates far more often (~every backend seq bump) than the loop itself renders
  // (once per 1s tick) and does not need its own re-render. sseFailedRef flips once the stream
  // errors out (proxy strips SSE, 404, network) so the loop falls back to the old per-tick
  // getJob() poll for the rest of that scan rather than trusting a connection known to be dead.
  const liveJobStateRef = useRef(null)
  const sseFailedRef = useRef(false)
  // "Notify me when complete" (slice 3c): the button drives `notifyArmed` for its label; the ref is what
  // the async scan-completion code reads, since it fires long after this render's closure was captured.
  const [notifyArmed, setNotifyArmed] = useState(false)
  const notifyArmedRef = useRef(false)
  const [tick, setTick] = useState(0)                  // bumped every minute to keep timeAgo labels fresh
  const [platformVersion, setPlatformVersion] = useState(null)  // full git-derived CalVer from /config (with the daily .N)
  const [realtimeShadowEnabled, setRealtimeShadowEnabled] = useState(false)
  // Bumped once if /config reports a scope different from activeScope.js's fallback. React cannot
  // observe a module-level binding, so this is what makes the server-driven scope actually reach
  // the rendered denominators instead of sitting in a variable nothing re-reads.
  const [, setScopeTick] = useState(0)
  // Set when the scan this tab is holding cannot be loaded for this account (per-scan 404 —
  // see api.SCAN_UNAVAILABLE). Shape: { scanId, reason, recoveredTo|null, recovered:boolean }.
  // Rendered as an alert, because the alternative — which is what shipped — is an empty score.
  const [scanUnavailable, setScanUnavailable] = useState(null)
  const recoveringRef = useRef(false)      // one recovery at a time; getScan() below can 404 too
  // Ownership of the platform `scan_scope` setting (PUT /settings is owner-only). null = unknown
  // until /config reports `is_scope_owner`. Fail-open: unknown/absent leaves scope editing enabled
  // (current behavior); only an explicit `false` gates a non-owner to a read-only scope wizard.
  const [scopeOwner, setScopeOwner] = useState(null)
  // Universal scan gate: `{ source, folder }` while the app-level review modal is open. Declared
  // with the other hooks (above the `!me` early return) so the hook order never changes.
  const [pendingScan, setPendingScan] = useState(null)
  // Non-blocking: the reasons discovery/preflight returned 'degraded' for the scan currently
  // running, or null. Set once at scan start, shown on the run's own progress card for its
  // duration (see DiscoverRunProgress) — the degraded condition (e.g. a queue backlog) caused
  // the scan to queue behind other work, so it stays relevant for as long as the run does,
  // unlike the ambient pre-scan readyz banner it complements rather than replaces.
  const [preflightDegraded, setPreflightDegraded] = useState(null)
  // null = unknown; true = down; false = reachable.
  const [backendDown, setBackendDown] = useState(null)
  const [backendRetries, setBackendRetries] = useState(0)
  const [backendLastChecked, setBackendLastChecked] = useState(null)
  const [backendRetrying, setBackendRetrying] = useState(false)
  const [backendRestored, setBackendRestored] = useState(false)
  const backendWasDown = useRef(false)

  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 60_000)
    return () => clearInterval(t)
  }, [])

  // Backend health banner: probe /healthz every 30 s. Tracks retry count for severity tiers.
  useEffect(() => {
    let cancelled = false
    const probe = () => checkHealth().then((ok) => {
      if (cancelled) return
      setBackendLastChecked(Date.now())
      setBackendDown(!ok)
      if (ok) {
        if (backendWasDown.current) { setBackendRestored(true); setTimeout(() => setBackendRestored(false), 4000) }
        backendWasDown.current = false
        setBackendRetries(0)
      } else {
        backendWasDown.current = true
        setBackendRetries((n) => n + 1)
      }
    })
    probe()
    const t = setInterval(probe, 30_000)
    return () => { cancelled = true; clearInterval(t) }
  }, [])

  // Adopt the scope the SERVER is gating on. activeScope.js ships a fallback preset so the first
  // paint is never blank, but the bundle's copy is a guess until this lands — the backend's
  // `scan_scope` setting is the only authority. `scopeTick` exists solely to re-render after the
  // module's live bindings change: React cannot observe a module variable, so without it the UI
  // would keep rendering the fallback's arithmetic and the fetch would be pointless. Bumped only
  // when something actually changed.
  //
  // Boot adopts the default scope; a successful scan-scoped save adopts that exact selection.
  // Re-fetching global defaults after a save would overwrite the user's selected criteria.
  const adoptScopeConfig = (c) => { if (applyScopeConfig(c)) setScopeTick((n) => n + 1) }

  // Pull the authoritative CalVer once (works pre-auth — /config is public) so the build
  // stamp + header show the full version with the daily counter, not the date-only bundle tag.
  useEffect(() => {
    getConfig().then((c) => {
      if (c?.version) setPlatformVersion(c.version)
      setRealtimeShadowEnabled(c?.realtime_shadow_enabled === true)
      adoptScopeConfig(c)
      // Ownership of the owner-only `scan_scope` setting. Only trust an explicit boolean; a build
      // whose backend predates this field leaves `scopeOwner` null → scope editing stays enabled.
      if (typeof c?.is_scope_owner === 'boolean') setScopeOwner(c.is_scope_owner)
    }).catch(() => { /* keep the build-time fallback */ })
  }, [])

  useEffect(() => {
    const onExpired = (e) => {
      clearAllTokens()
      sessionStorage.removeItem('gd_token')
      sessionStorage.removeItem('sp_token')
      setHasDriveToken(false)
      setHasSPToken(false)
      // Carry the reason to the sign-in screen. Dropping the user back to sign-in with no
      // explanation, mid-review, reads as the app having lost their work.
      setSignedOutReason(e?.detail?.reason || SESSION_EXPIRED)
      setMe(null)
    }
    window.addEventListener('acp:session-expired', onExpired)
    return () => window.removeEventListener('acp:session-expired', onExpired)
  }, [])

  // Deep-link into Settings from anywhere in the app (P1: the review card's empty-state honesty hint
  // points an admin at Settings → AI Providers). Reuses the same custom-event pattern as
  // acp:session-expired / acp:scan-unavailable rather than threading a callback through the tree.
  // Gated on the settings permission exactly as the ⚙ button and the modal render already are.
  useEffect(() => {
    const onOpenSettings = () => { if (canOpenSettings(me, access)) setSettingsOpen(true) }
    window.addEventListener('acp:open-settings', onOpenSettings)
    return () => window.removeEventListener('acp:open-settings', onOpenSettings)
    // `access` belongs here as well as `me`: it arrives from /workspace/bootstrap AFTER the first
    // render, so a listener registered with only [me] would close over the null payload forever
    // and keep answering from access.js's fail-open. That direction is the safe one, which is
    // exactly why it would not have been noticed.
  }, [me, access])

  // Refetch the scan when a remediation or a deferred assessment announces that the server's
  // file_records changed — see scanRefetch.js for which events and why.
  useScanRefetch(scan?.run?.id, setScan)

  // Background scan-list refresh so a scan that finishes in another tab (or via a scheduled
  // run) surfaces here without a page reload. Only runs while idle — no scan in flight from
  // this tab — and at a slow interval to avoid burning API quota across concurrent sessions.
  // When the list updates, isTimeTravel reacts automatically: if the user was viewing the
  // "latest" scan and a newer one appeared, the banner offers a one-click switch; if they
  // explicitly time-traveled, the existing time-travel banner is already showing.
  useEffect(() => {
    if (!me || busy) return
    const id = setInterval(() => {
      listScans().then(setScanList).catch(() => {})
    }, 60_000)
    return () => clearInterval(id)
  }, [me, busy])

  // Session storage is intentionally cleared on sign-out. Rejoin comes from owner-scoped
  // server state instead, refreshed through a small endpoint so completion removes the banner
  // without repeatedly downloading the whole workspace bootstrap.
  useEffect(() => {
    if (!me) return
    let alive = true
    const refresh = () => {
      if (document.hidden) return
      getActiveWorkflows().then((r) => {
        if (alive) setActiveWorkflows(r?.active_workflows || [])
      }).catch(() => {})
    }
    const id = setInterval(refresh, 15_000)
    // Another browser tab can start, stop, or finish a stage while this one is in the
    // background. Refresh as soon as the user returns so the durable compact card reconciles
    // immediately instead of displaying the previous state for up to one polling interval.
    // The server remains authoritative; this is the same owner-scoped read used by the timer.
    window.addEventListener('focus', refresh)
    window.addEventListener('acp-access-changed', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      alive = false
      clearInterval(id)
      window.removeEventListener('focus', refresh)
      window.removeEventListener('acp-access-changed', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [me])

  // Publish writes back per file; refetching once per click would fire dozens of
  // times on "Publish all", so debounce — one getScan after the burst settles
  // makes published_at the durable source of the checkmarks (they survive tab
  // switches instead of living only in Publish's local state).
  const pubRefetchTimer = useRef(null)
  const schedulePublishRefetch = () => {
    clearTimeout(pubRefetchTimer.current)
    pubRefetchTimer.current = setTimeout(() => {
      const sid = scan?.run?.id
      if (sid) getScan(sid).then(setScan).catch(() => {})
    }, 800)
  }

  useEffect(() => {
    if (!me) return
    setBootError(null)
    getRubric().then(setRubric).catch(() => {})
    getSources().then(setSources).catch(() => {})
    setLoadStage('bootstrap')
    // Real timing for this chain (loadPerf.js) — see that module's header for why. hadPreviewForPerf
    // /hadScanForPerf are set inside the .then() below, where `b`/`scanId` are in scope, and read
    // back out in .finally(), which is not.
    markLoad('load-start')
    let hadPreviewForPerf = false
    let hadScanForPerf = false
    getWorkspaceBootstrap()
      .then(async (b) => {
        markLoad('bootstrap-resolved')
        // Which scan is "the" default (newest NON-collapsed — a degenerate small scan on top
        // would otherwise make every view show a shrunken estate) is now decided server-side,
        // the SAME rule pickDefaultScan/defaultScan.js applies, pinned against the same test
        // cases (api/routes/workspace.py pick_default_scan) — so this one request already
        // carries the pick, the scan-picker list, the active-job summary, and (Phase 1a) the
        // picked scan's cached Overview snapshot, in place of listScans + getActiveScan.
        setScanList(b.scans || [])
        // Workspace access (PRD §13) rides this same request rather than a second one, so the
        // navigation can draw with the right tabs on its FIRST render. Fetched separately it
        // would render every tab and then remove some — a visible flicker that also briefly
        // advertises surfaces the user may not have.
        setAccess(b.me?.access || null)
        // Bootstrap is the cross-provider identity authority. GET /me reads a Google Drive
        // profile and may be unavailable in a Microsoft-only session; retain these server-made
        // authorization flags so an administrator does not lose Settings controls in the SPA.
        if (b.me) setMe((current) => mergeBootstrapIdentity(current, b.me))
        setActiveWorkflows(b.active_workflows || [])
        const active = primaryActiveWorkflow(b.active_workflows || [])
        if (active && !viewWasChosen.current && isVisible(b.me?.access || null, active.stage)) {
          setView(active.stage)
        }
        setOverviewPreview(b.overview || null)
        hadPreviewForPerf = !!b.overview
        // The default historical scan and the active execution are allowed to differ. When work
        // is running, load THAT scan before opening its stage; otherwise the restored Assess or
        // Remediate screen would accurately rejoin the wrong scan.
        const scanId = active?.scan_id || b.scan_id || null
        hadScanForPerf = !!scanId
        // If a scan is still running (e.g. user reloaded mid-scan), resume tracking it. The
        // default-path job is checked FIRST: it can be mid-crawl with no scan_runs row at all
        // (see ACTIVE_JOB_KEY above), a window bootstrap's active_job cannot see into, so a
        // pending job and an active scan_runs row are never both real at once — no double-
        // reconnect risk.
        let pendingJobId = sessionStorage.getItem(ACTIVE_JOB_KEY)
        // A job nobody ever claimed sits here forever otherwise — see activeJobStaleness.js.
        // Past the stale window, stop treating it as pending and let the normal bootstrap-picked
        // scan (already fetched above via scanId) show instead of an indefinite reconnect.
        if (pendingJobId && isActiveJobStale(Number(sessionStorage.getItem(ACTIVE_JOB_AT_KEY)))) {
          sessionStorage.removeItem(ACTIVE_JOB_KEY)
          sessionStorage.removeItem(ACTIVE_JOB_AT_KEY)
          pendingJobId = null
        }
        // Only a stage that gates something worth waiting on is worth naming: with active_job
        // already resolved as part of bootstrap, a missing scanId has nothing left to await in
        // either branch below, so there is no meaningful "checking…" period left to narrate —
        // unlike the pre-bootstrap chain, where getActiveScan() was still a real network call.
        if (scanId) setLoadStage('scan')
        if (pendingJobId) {
          // reconnectJob owns its OWN busy/progress UI (setBusy/setProgress) and can run for as
          // long as the reconnected job takes to settle — it must stay fire-and-forget, exactly
          // as before, never awaited into the chain that flips `loaded`. active_job is ignored
          // in this branch on purpose (see the comment above): a pending job and an active
          // scan_runs row are never both real at once, so there is nothing in it to act on here
          // — only getScan is left to actually wait on.
          reconnectJob(pendingJobId)
          if (scanId) { await getScan(scanId).then(setScan); markLoad('scan-resolved') }
        } else {
          // reconnectScan, like reconnectJob above, stays fire-and-forget — bootstrap already
          // resolved active_job in the SAME request that gave us scanId, so unlike the old
          // listScans+getScan+getActiveScan chain there is nothing left to race here: only
          // getScan(scanId) — a genuinely heavy query on a large estate (file_records joined
          // against scan_inventory, shadow-file reconciliation, counter aggregation) — still
          // gates `loaded`.
          if (b.active_job && b.active_job.id) reconnectScan(b.active_job.id, b.active_job.job_id)
          if (scanId) { await getScan(scanId).then(setScan); markLoad('scan-resolved') }
        }
      })
      // A 404 on the scan has its own recovery (acp:scan-unavailable, handled above) and must keep
      // it — that path knows how to pick another scan, which this screen does not.
      .catch((e) => {
        if (String(e?.message || '') === SCAN_UNAVAILABLE) return
        setBootError(e?.name === 'TimeoutError'
          ? 'The server did not respond in time.'
          : (e?.message || 'The server could not be reached.'))
      })
      .finally(() => {
        setLoaded(true); setLoadStage(null)
        markLoad('load-complete')
        logLoadSummary({ hadPreview: hadPreviewForPerf, hadScan: hadScanForPerf })
      })
  }, [!!me, bootAttempt]) // identity details hydrate inside this effect; only sign-in/out reruns it

  // Annotate the corpus with the published business ontology (adds `.ont`: label,
  // priority, matched rule, weighted score) so the live workflow is ontology-aware.
  // Kept above the early return below to satisfy the rules of hooks.
  // Attach the per-file remediation recommendation. In SIM the sim builder already sets it;
  // for a REAL backend scan the files arrive without it, so compute it here — otherwise
  // `remediable` is empty and server-side remediation finds nothing to do.
  const allFiles = useMemo(() => annotate(scan?.files ?? [], ontology).map((f) => (f.rec ? f : { ...f, rec: recommendFor(f) })), [scan, ontology])
  // Run health, derived ONCE and read by both places that report it: the exception chip in the
  // header actions, and the "✓ Verified" affordance in the context bar below it. They are two ends
  // of one ordering (worker error > unreadable > healthy), so deriving them separately is how the
  // header came to show "3 unreadable" and "✓ Verified" side by side.
  // Off `scan?.run`, not the `run` const — that one is bound much further down, and reading it
  // here is a temporal-dead-zone crash rather than a stale value. Same object either way.
  const unreadableFiles = useMemo(
    () => allFiles.filter((f) => f.status === 'error').length, [allFiles])
  const workerError = scan?.run?.status === 'failed'

  // The file-type filter applies to EVERY tab, not just Discover.
  //
  // It used to be filtered inside Discover alone (`visibleFiles`), so an operator who scoped the
  // scan to .docx saw a docx-only inventory and then a full estate everywhere after it: Assess
  // scored the PDFs, Remediate queued them, Overview counted them, Publish certified against
  // them. The filter looked like it worked and then silently stopped applying one tab later,
  // which is worse than not having one — every number downstream described a different
  // population than the screen the operator set it on.
  //
  // Filtered once, here, so every tab inherits the same population by construction rather than
  // by each component remembering to. `scan_scope` on the server is the other half and gates
  // which CRITERIA are evaluated; this gates which FILES are shown. Both are needed: the server
  // returns the whole estate because a scan inventories everything it can see.
  //
  // An empty config means no restriction, matching Discover's original `!== false` test — a
  // type absent from the map has never been excluded, only ones explicitly set false.
  const files = useMemo(() => visibleForFileTypes(allFiles, fileTypeConfig), [allFiles, fileTypeConfig])

  // A13 · document-to-document navigation from inside a file's findings. It walks the SAME order the
  // worklist prints — documentRows, unopened last — so "next" never disagrees with the list it came
  // from. Only opened documents are reachable: an unopened file has no findings view to move to.
  // Declared here, with the other hooks and above every early return, so its hook order is stable.
  const assessNavRows = useMemo(
    () => documentRows(files, { cap, assessment }).filter((r) => r.opened),
    [files, cap, assessment])
  const assessFileNext = useMemo(() => {
    if (!assessFile) return null
    const i = assessNavRows.findIndex((r) => r.file === assessFile.file)
    return i >= 0 && i + 1 < assessNavRows.length ? assessNavRows[i + 1] : null
  }, [assessNavRows, assessFile])
  // A5/A13 · the RAW file record behind the worklist row. `assessFile` is a documentRow — the
  // derived shape the worklist and this navigation use — which carries no size, no Drive id and no
  // timestamp. Those live only on the file record `files` itself hands out, so this looks it up by
  // the one key both shapes share.
  const assessFileRaw = useMemo(
    () => (assessFile ? files.find((f) => f.file === assessFile.file) : null),
    [assessFile, files])

  // Real accounts that get elevated privileges on source connect (never shown in demo list)
  const PRIV_PROFILE = {
    id: 'jeremy-yu', name: 'Jeremy Yu', role: 'Compliance Officer & Admin',
    scope: { label: 'Full estate · all departments', departments: 'all' },
    allow: ['overview', 'integrations', 'discover', 'assess', 'remediate', 'publish', 'monitor', 'settings', 'liveops', 'analytics', 'acr'],
  }
  const PRIVILEGED = { 'jeremyyu.movate@gmail.com': PRIV_PROFILE }

  // Everything that belongs to ONE scan, cleared in one place.
  //
  // Three call sites used to reset overlapping-but-different subsets of this, and each missed
  // something the others cleared: sign-in reset all of it, time-travel reset `decisions` and
  // `certifiedDocs` but not `triage`, and a NEW SCAN reset none of it — it relied entirely on
  // the hydration effect, which only covers decisions/triage and only once the fetch lands.
  //
  // What that cost, concretely: `publishedFiles` gates the Publish and Monitor steps of the
  // progress rail (`publish: publishedFiles.length > 0`). Publish a document, then re-scan, and
  // the NEW scan opened with Publish and Monitor already ticked — on the strength of files
  // published against the previous one.
  //
  // `triage` matters more than it used to. Marking a single file in-scope excludes every
  // unmarked file from remediation, so showing another scan's scope marks — even for the
  // hydration round-trip — is now materially misleading, not just untidy.
  //
  // NOT reset here, deliberately: `assessPhase` and `justAssessed`. Both already self-correct
  // and clearing them would flash the Overview's gated panels off and back on. AssessRunner is
  // keyed on `run.id`, so it remounts per scan and re-emits `onPhase` from that run's own saved
  // state; and `justAssessed` is compared by id (`justAssessed === run?.id`), so a value left
  // over from another scan can never match. Add to this function only what does NOT self-correct.
  const resetScanScopedState = () => {
    setHitlCount(0)
    setDecisions({}); setTriage({}); setAssignees({})
    setCertifiedDocs([]); setPublishedFiles([])
  }

  const signIn = (p) => {
    // Fresh per user: if a DIFFERENT user signs in (or first sign-in), wipe activity caches
    // so nothing — published files, assess/remediation results, scores — carries over.
    try {
      if (localStorage.getItem('mova_last_user') !== p.email) {
        clearActivityStorage()
        localStorage.setItem('mova_last_user', p.email || '')
      }
    } catch { /* ignore */ }
    setSignedOutReason(null)    // the sign-in worked; the expiry notice must not outlive it
    setScanUnavailable(null)    // ditto: a new session's scan list is about to be re-read
    if (p.token && p.sso === 'Microsoft') {
      // Microsoft: the Entra token is the API bearer (backend verifies it via Graph /me). It is
      // ALSO the SharePoint token, wired below from sp_token — no Drive scopes on this one.
      setMsToken(p.token)
    } else if (p.token) {
      setGoogleToken(p.token)   // API Bearer auth
      setDriveToken(p.token)    // Same token has Drive scopes — no separate connect needed
      setHasDriveToken(true)
    } else {
      const gdToken = sessionStorage.getItem('gd_token')
      if (gdToken) { setDriveToken(gdToken); setHasDriveToken(true) }
    }
    const sp = sessionStorage.getItem('sp_token')
    if (sp) { setSPToken(sp); setHasSPToken(true) }
    // Reset ALL downstream state so the new session starts blank — including the pieces the
    // old reset missed (triage, the assess-completion phase, the optimistic-assess flag, and
    // the localStorage-backed published/ontology), so no tab shows legacy data on first view.
    setPersona(p); setScan(null); setScanList([]); setExplicitTimeTravel(false); setLoaded(false)
    setOverviewPreview(null)
    resetScanScopedState()
    // Sign-in additionally resets what a scan change deliberately leaves alone: a new session
    // has no assessment in flight to report a phase for.
    setAssessPhase('idle'); setJustAssessed(null)
    setOntology(loadPublished())
    setSettingsOpen(false); setMyDataOpen(false); setView((p.allow || ['overview'])[0])
    setMe({ email: p.email, name: p.name, role: p.role, scope: p.scope?.label,
      allow: [...new Set([...(p.allow || []), ...ALL_TAB_KEYS])] })
    // Scope editing is owner-only (PUT /settings = _require_admin). GET /me returns the
    // authoritative per-user `is_scope_owner` post-auth (the sign-in payload doesn't carry it,
    // and /config is fetched pre-auth so its copy is null). A non-owner → read-only scope in the
    // review modal instead of a silently-dropped edit; fail-open (null) keeps the owner editable.
    getMe().then((m2) => {
      if (typeof m2?.is_scope_owner === 'boolean') setScopeOwner(m2.is_scope_owner)
      // Backend-enforced: grant admin-only operational views from /me, so an admin who is not in
      // PRIV_PROFILE still sees them and a non-admin cannot reach them through the UI.
      if (m2?.is_admin) setMe((m) => {
        const allow = m.allow || []
        const adminViews = ['analytics', 'liveops'].filter((view) => !allow.includes(view))
        return { ...m, is_admin: true,
          allow: adminViews.length ? [...allow, ...adminViews] : allow }
      })
    }).catch(() => {})
  }

  // Called from Integrations when a source OAuth succeeds
  const handleConnect = (provider, email, token) => {
    const priv = PRIVILEGED[email?.toLowerCase()]
    if (provider === 'google') {
      sessionStorage.setItem('gd_token', token)
      setDriveToken(token); setHasDriveToken(true)
      if (priv) setMe((m) => ({ ...m, ...priv, email, scope: priv.scope?.label }))
      getSources().then(setSources).catch(() => {})
    } else if (provider === 'microsoft') {
      sessionStorage.setItem('sp_token', token)
      setSPToken(token); setHasSPToken(true)
      setSPReconnectRevision((revision) => revision + 1)
      if (priv) setMe((m) => ({ ...m, ...priv, email, sso: 'Microsoft', scope: priv.scope?.label }))
    }
  }

  // Time-travel: when the active scan changes, hydrate THAT scan's saved decisions.
  // These hooks MUST sit above the `!me` early return — React requires every hook to
  // run unconditionally on every render, or the hook count changes and the tree crashes.
  useEffect(() => {
    const sid = scan?.run?.id
    if (!sid || sid === savedDecRef.current.scanId) return
    let cancelled = false
    getDecisions(sid).then((d) => {
      if (cancelled) return
      const dec = {}, tri = {}, asg = {}
      Object.entries(d || {}).forEach(([file, m]) => {
        if (m.action) { try { dec[file] = JSON.parse(m.action) } catch { /* ignore */ } }
        if (m.triage) tri[file] = m.triage
        if (m.assignee) asg[file] = m.assignee
      })
      hydratingRef.current = true
      savedDecRef.current = { scanId: sid, decisions: dec, triage: tri, assignees: asg }
      setDecisions(dec); setTriage(tri); setAssignees(asg)
    }).catch(() => {
      hydratingRef.current = true
      savedDecRef.current = { scanId: sid, decisions: {}, triage: {}, assignees: {} }
      setDecisions({}); setTriage({}); setAssignees({})
    })
    return () => { cancelled = true }
  }, [scan?.run?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Persist decision/triage changes to the active scan (skips hydration writes).
  useEffect(() => {
    if (hydratingRef.current) { hydratingRef.current = false; return }
    const sid = savedDecRef.current.scanId
    if (!sid || sid !== scan?.run?.id) return
    const prev = savedDecRef.current
    const items = []
    new Set([...Object.keys(prev.decisions), ...Object.keys(decisions)]).forEach((f) => {
      if (JSON.stringify(prev.decisions[f]) !== JSON.stringify(decisions[f])) items.push({ file: f, kind: 'action', value: decisions[f] ?? null })
    })
    new Set([...Object.keys(prev.triage), ...Object.keys(triage)]).forEach((f) => {
      if (prev.triage[f] !== triage[f]) items.push({ file: f, kind: 'triage', value: triage[f] ?? null })
    })
    new Set([...Object.keys(prev.assignees || {}), ...Object.keys(assignees)]).forEach((f) => {
      if ((prev.assignees || {})[f] !== assignees[f]) items.push({ file: f, kind: 'assignee', value: assignees[f] ?? null })
    })
    if (items.length) saveDecisionsBatch(sid, items).catch(() => {})
    savedDecRef.current = { scanId: sid, decisions: { ...decisions }, triage: { ...triage }, assignees: { ...assignees } }
  }, [decisions, triage, assignees]) // eslint-disable-line react-hooks/exhaustive-deps

  // A per-scan request 404'd (api.js dispatches this before any caller's .catch can eat it).
  // Say so, then move the user to a scan they CAN load — an unexplained empty score is the
  // failure this replaces, and retrying an id that will never resolve only prolongs it.
  useEffect(() => {
    const onUnavailable = async (e) => {
      const badId = e?.detail?.scanId || null
      // Ignore a 404 for some OTHER scan than the one on screen: a stale poll from a tab the
      // user has already navigated away from must not evict the scan they are reading.
      const current = scan?.run?.id || null
      if (badId && current && badId !== current) return
      if (recoveringRef.current) return
      recoveringRef.current = true
      setScanUnavailable({ scanId: badId, reason: e?.detail?.reason || '', recoveredTo: null, recovered: false })
      try {
        let list = []
        try { list = await listScans() } catch { list = [] }
        setScanList(list)
        const next = list.find((s) => s.id !== badId) || null
        if (next) {
          // getScan can itself 404 (a scan deleted between the list and this read). The
          // recoveringRef guard above stops that from re-entering; the catch leaves the alert
          // standing with recovered:false, which is the honest outcome.
          const loaded = await getScan(next.id)
          setScan(loaded); resetScanScopedState(); setView('overview')
          setScanUnavailable({ scanId: badId, reason: e?.detail?.reason || '', recoveredTo: next.id, recovered: true })
        } else {
          // Nothing of their own to fall back to. Clear the dead scan and send them to the one
          // action that can produce one, rather than leaving a scored-looking empty dashboard.
          // overviewPreview must go too: it now renders directly (OverviewPreviewCard) whenever
          // `run` is null, and this scan's aggregate numbers are exactly as dead as the scan.
          setScan(null); setOverviewPreview(null); resetScanScopedState()
          setScanUnavailable({ scanId: badId, reason: e?.detail?.reason || '', recoveredTo: null, recovered: true })
          setView((v) => (me?.allow && !me.allow.includes('discover') ? v : 'discover'))
        }
      } finally {
        recoveringRef.current = false
      }
    }
    window.addEventListener('acp:scan-unavailable', onUnavailable)
    return () => window.removeEventListener('acp:scan-unavailable', onUnavailable)
  }, [scan?.run?.id, me?.allow]) // eslint-disable-line react-hooks/exhaustive-deps

  // The run's coverage record, read ONCE for the screen and shared. The Run integrity panel
  // renders it in full and the summary beside it carries one sentence from it, and those two have
  // to agree — a summary reading "No findings" under a panel reading "coverage unknown" is exactly
  // the contradiction the gate exists to remove, and two independent fetches is how it appears.
  //
  // ABOVE the `if (!me)` return below, and that placement is load-bearing rather than tidy. Every
  // hook after that line runs only when signed in, so a hook there changes React's hook count
  // between a signed-out and a signed-in render: "Rendered more hooks than during the previous
  // render", which took out five App-mounting test files at once when this was first written
  // further down. Nothing else in this component calls a hook after line ~928 — this was the first,
  // and the error names the symptom rather than the rule, so it is written down here.
  //
  // Keyed on assessed_at rather than on `assessed && resultsReady` (both derived far below, and
  // unavailable this early). The cost of the looser condition is one cheap indexed GET for a scan
  // whose results are not on screen; the verdict itself is computed where those flags exist.
  const runManifest = useScanManifest(scan?.run?.id, { skip: !scan?.run?.assessed_at })

  if (!me) return <SignIn onSignedIn={signIn} notice={signedOutReason} />   // SignIn's own BuildStamp shows the full CalVer
  // After sign-in, never before: the viewer's facts request is owner-scoped, and there are no
  // hooks below this line, so this early return cannot change the hook count.
  if (evidenceTarget) return <FindingEvidenceViewer target={evidenceTarget} onClose={closeEvidence} onOpen={openEvidence} />

  const switchScan = async (id) => {
    if (id === scan?.run?.id) return
    // History is chronological. The preferred workspace default can intentionally be an older
    // verified/full-size scan; choosing the starred newest entry must still leave replay mode.
    setExplicitTimeTravel(isHistoricalScan(scanList, id))
    setScanLoading(true)
    try {
      setScan(await getScan(id))
      // Cleared BEFORE the hydration effect refills decisions/triage for the scan being opened,
      // so the gap between the two never shows the previous scan's marks as if they were this
      // one's. The prior scan keeps its own copy — decisions and triage are persisted per scan
      // server-side and re-fetched on every switch, which is what makes time-travel lossless
      // even for a run nobody finished.
      resetScanScopedState()
    } catch { /* leave current scan */ } finally { setScanLoading(false) }
  }

  // ── Universal scan gate ──────────────────────────────────────────────────────
  // Every scan entry point calls `requestScan` (wired as their `onScan` prop), which OPENS the
  // app-level review modal instead of scanning. The only path that actually dispatches a scan is
  // the modal's "Start scan" confirm → `doScan`. This is what makes the scope/behavior review
  // unbypassable from Discover, Overview, EmptyState/ScanSetup, the Sources tab, and the
  // Drive/SharePoint browse panels alike — before this, the modal lived inside Integrations and
  // only the Sources tab reached it. (`pendingScan` state is declared with the other hooks above.)
  // `folderFirst` unifies the two "pick a folder" entry points that used to run independently:
  // Discover's own "Choose folder to scan…" button opened a standalone FolderPicker modal, and on
  // a selection immediately opened THIS SAME wizard again at its default "Entire connected
  // source" step — the folder chosen a moment ago request re-asked from scratch, which read as a
  // regression, not a review step. FolderPicker's `layout="inline"` is already what the wizard's
  // own step 1 embeds for "Specific folders" (see FolderPicker.jsx's own header), so the fix is
  // routing straight there instead of opening a second, separate instance of the same picker.
  // `folders` is the multi-root form, carried through the review modal to the run. It exists for
  // the SharePoint site picker: several sites travel as roots (scanner._sp_locations splits bare
  // site ids out of the same `folders` list it reads folder pairs from), and the wizard has no
  // surface that could hold them — its folder tree browses ONE drive, so a multi-site selection
  // would have been silently dropped at the review step and the scan would have run against the
  // whole of OneDrive instead. Dropping a boundary is the one failure this whole path is careful
  // about, so the selection is carried rather than re-asked.
  const requestScan = (source, folder = null,
                       { folderFirst = false, allFolders = false, folders = null } = {}) =>
    setPendingScan({ source, folder, folderFirst, allFolders, folders })

  // The DEFAULT (session-scoped, non-durable) scan path has no scan_runs row until AFTER its
  // crawl finishes — _scan_discover creates it partway through its own function body, well past
  // the point _list() has already spent however long the source's estate takes to walk. So
  // getActiveScan() (below) cannot see a scan in that window at all: reload the tab mid-crawl and
  // the screen shows nothing running, no error, nothing — the same invisibility #607 fixed for a
  // job whose replica died, still present for the ordinary case of a plain reload.
  //
  // ACTIVE_JOB_KEY is what survives the reload instead: sessionStorage (not state, which a reload
  // wipes), holding the one thing the poll actually needs — job_id — set the moment a job starts
  // and cleared the moment it resolves, success or failure alike, so a finished job is never
  // "reconnected" to on a later reload. ACTIVE_JOB_AT_KEY is its wall-clock companion — see
  // activeJobStaleness.js for why a bare job_id with no timestamp isn't enough on its own.
  const ACTIVE_JOB_KEY = 'active_job_id'
  const ACTIVE_JOB_AT_KEY = 'active_job_id_at'

  const _pollScanJobPolling = async (job_id) => {
    let job
    do {
      job = await getJob(job_id)
      // Checked straight after the await, before the sleep: an unmounted App must not schedule
      // another tick, and must not spend a further getScan below either.
      if (unmountedRef.current) return null
      setProgress(job)
      if (!job.done) await new Promise((r) => setTimeout(r, 350))
    } while (!job.done && !unmountedRef.current)
    if (unmountedRef.current) return null
    if (job.error) throw new Error(job.error)
    return getScan(job.scan_id)
  }

  const pollScanJob = (job_id) => {
    sessionStorage.setItem(ACTIVE_JOB_KEY, job_id)
    sessionStorage.setItem(ACTIVE_JOB_AT_KEY, String(Date.now()))
    const run =
      typeof ReadableStream !== 'undefined' && typeof getJob.openStream === 'function'
        ? new Promise((resolve, reject) => {
            let settled = false
            let lastJob = null
            let stream = null
            const finish = (job) => {
              if (settled) return
              settled = true
              stream?.close()
              if (job?.error) reject(new Error(job.error))
              else getScan(job?.scan_id).then(resolve, reject)
            }
            stream = getJob.openStream(job_id, {
              onMessage: (job) => {
                // Same rule as the polling fallback: once the App is gone, close the stream
                // rather than holding a server-side generator open for nobody.
                if (unmountedRef.current) { settled = true; stream?.close(); resolve(null); return }
                lastJob = job
                setProgress(job)
                if (job.done) finish(job)
              },
              onDone: () => { if (lastJob?.done) finish(lastJob) },
              onError: () => {
                if (settled) return
                settled = true
                stream?.close()
                _pollScanJobPolling(job_id).then(resolve, reject)
              },
            })
          })
        : _pollScanJobPolling(job_id)
    return run.finally(() => {
      // ONLY when the job actually reached a terminal state. Abandoning the poll because the App
      // unmounted is not the same as the job finishing: these two keys are what a fresh load
      // reconnects THROUGH, so clearing them on unmount would mean a reload during a running job
      // silently forgot it — defeating the reconnect this function exists to serve.
      if (unmountedRef.current) return
      sessionStorage.removeItem(ACTIVE_JOB_KEY)
      sessionStorage.removeItem(ACTIVE_JOB_AT_KEY)
    })
  }

  // Reconnect to an in-flight DEFAULT-path scan after a page reload — the mirror of reconnectScan
  // just below, for the path that has no durable scan_runs row to reconnect through until the end.
  // A job that died silently before this reload (#607's staleness detection, read fresh on the
  // very next poll) surfaces here exactly as it would have in a tab that stayed open — the reload
  // does not cost the error, only the seconds spent waiting for it.
  const reconnectJob = async (job_id) => {
    setBusy(true); setErr(null); setProgress({ phase: 'connecting' })
    try {
      const fresh = await pollScanJob(job_id)
      setScan(fresh); setExplicitTimeTravel(false)
      // A fresh, successfully-reconnected scan supersedes any earlier "scan not available"
      // banner — found live 2026-08-21: a stale banner from an EARLIER failed reconnect (a
      // sessionStorage active_job_id surviving a reload past the scan it pointed at) kept
      // rendering "you don't have a scan of your own yet" directly above this scan's own real,
      // correct results, because nothing here ever cleared it once new data arrived.
      setScanUnavailable(null)
      resetScanScopedState()
      setScanList(await listScans())
    } catch (e) {
      setErr(`scan failed: ${scanFailureDetail(e?.message ?? e)}`)
    } finally {
      setBusy(false); setProgress(null)
    }
  }

  // `runScope` is the wizard's per-run folder choice ({folders, exclude}). Given, it wins over the
  // connection default below — that is what "this run only" means. Absent (a scan started without
  // the wizard), the saved connection scope is used, so a scheduled or card-launched scan still
  // honours what the source is configured to cover.
  // Stop the run currently being polled. Two things happen and BOTH are needed:
  //
  //  1. the flag, set FIRST and unconditionally — it is what ends the poll loop. A cancelled QUEUED
  //     scan is invisible to the poll (no scan_runs row is ever created for it), so nothing the
  //     server returns can end the loop on its own. Set before the await so the very next tick
  //     stops, rather than one round-trip later.
  //  2. the request, whose failure is now SHOWN. It used to be `.catch(() => {})`, so a cancel the
  //     server refused — a 409, an expired session, an offline tab — looked exactly like a cancel
  //     that worked. That silence is half of why this button was reported as doing nothing.
  //
  // The flag stands even if the request fails: the user asked this tab to stop polling, and it
  // stops. The message says plainly that the server may still be running the work, so "I stopped
  // watching" is never mistaken for "the work is definitely stopped".
  const stopScan = (scanId) => {
    scanCancelledRef.current = true
    if (!scanId) return Promise.resolve()
    return Promise.resolve(cancelScan(scanId)).catch((e) => {
      setStopped(`Stopped watching this scan, but the server did not confirm the cancel `
        + `(${e?.message || 'request failed'}) — it may still be running. Check Monitor.`)
    })
  }

  const doScan = async (source, folder = null, runScope = null, replaceActive = false) => {
    if (busy) return                              // a scan/assessment is already running — don't launch another
    setBusy(true); setErr(null); setSubmitUncertain(null); setPreflightCapacityState(null); setProgress({ phase: 'preparing' })
    // A stop belongs to the run that was stopped. Clearing both here is what stops the previous
    // run's notice hanging over this one, and stops a stale flag ending this scan the moment it
    // starts.
    setStopped(null); scanCancelledRef.current = false
    setPreflightDegraded(null)   // belongs to the run that's ending; a new run gets its own verdict
    const prevAvg = scan?.run?.avg_score
    // SIM: sim functions handle any source string synthetically.
    // Real: map to a backend-valid source based on what tokens are present.
    const wantDrive = source === 'drive' || source === 'all'
    const wantSP = source === 'sharepoint'
    const apiSource = SIM ? source : (
      wantSP && hasSPToken ? 'sharepoint' :
      wantDrive && hasDriveToken ? 'drive' :
      'local'
    )
    // THE SAVED SCOPE HAS TO REACH THE SCAN. The Sources card persists which folders a
    // connection covers, and until this it was displayed and never sent: the card said
    // "Scans: HR" and every scan still read the whole Drive. A scope that is shown but not
    // applied is worse than none — it is a boundary the user believes in.
    //
    // Read here rather than held in state so it is never stale: the card can be edited in
    // another tab between mount and scan, and one extra GET is cheaper than a scan that
    // covered something else.
    let picked = null
    let excluded = null
    const includeSubfolders = runScope?.includeSubfolders !== false
    if (runScope && Array.isArray(runScope.folders)) {
      picked = runScope.folders
      excluded = runScope.exclude || []
    } else if (!SIM && (apiSource === 'drive' || apiSource === 'sharepoint') && !folder) {
      try {
        const { locations } = await getScanLocations()
        picked = (locations || {})[apiSource] || []
        excluded = ((locations || {})._exclude || {})[apiSource] || []
      } catch {
        // A failed lookup must not silently widen the scan to the whole source, and must not
        // block it either. Null means "not narrowed", which is what the server already does
        // without the parameters — and the scope line on the result will say so honestly.
        picked = null; excluded = null
      }
    }

    let streamHandle = null
    let submissionUncertain = false
    try {
      let fresh
      if (queuedScan) {
        // Read-only check on the SPECIFIC source + folders about to be scanned — catches a bad
        // credential, a deleted folder, or a dead worker tier before a scan row exists, instead
        // of the scan silently returning "0 documents" after the fact. Only a 'blocked' verdict
        // stops the start; 'degraded' (e.g. a queue backlog) is allowed through — the readyz
        // banner already covers ambient warnings, this only stops what would actually fail.
        // Best-effort: a failed preflight CALL (network hiccup, endpoint down) must never itself
        // block starting a scan — startScanQueued's own worker_tier_alive check below still
        // catches the one failure mode that actually matters if this check couldn't run at all.
        if (!SIM) {
          let pre = null
          try { pre = await checkDiscoveryPreflight(apiSource, folder, picked) } catch { pre = null }
          const { blocked, reason, capacityState, degradedReasons } = preflightVerdict(pre)
          if (blocked) throw new Error(`can't start this scan — ${reason}`)
          if (capacityState && capacityState !== 'ready') setPreflightCapacityState(capacityState)
          if (degradedReasons.length) setPreflightDegraded(degradedReasons)
        }
        // Durable path: enqueue a scan job, then poll until the scan is persisted.
        //
        // One idempotency key per submit intent (submitIntent.js). A retry after an UNCERTAIN
        // failure — a timeout, a dropped connection, the pool-exhaustion 503 — reuses the key, so
        // if the first request committed before its response was lost the server hands back that
        // same job instead of enqueuing a second scan. The key is only released once the outcome
        // is known: accepted here, or provably rejected in the catch below.
        setProgress({ phase: 'submitting' })
        const submitKey = beginOrResumeIntent('scan')
        let accepted
        try {
          accepted = await startScanQueued(apiSource, folder, aiEnabled, deepScan, excludeRemediated, incremental, picked, excluded, submitKey, replaceActive, true, includeSubfolders)
        } catch (err) {
          // Hold the key when we cannot tell whether the scan was created; drop it when the
          // server proved it was not, so the user's next, corrected attempt is a fresh intent
          // rather than one that resolves to nothing.
          if (!outcomeIsUncertain(err?.status)) abandonIntent('scan')
          // …and say so on screen. Before this, a lost response produced "scan failed", which
          // overclaims in the one direction that matters: the enqueue may well have committed.
          // A bounded request that timed out is reported as unconfirmed, with the retry that
          // reconciles it, rather than as a failure the user might respond to by starting a
          // second scan by hand.
          else {
            submissionUncertain = true
            setSubmitUncertain({ source, folder, runScope, timedOut: err?.name === 'TimeoutError' })
          }
          throw err
        }
        completeIntent('scan')
        const { scan_id, job_id, workers, worker_tier_alive } = accepted
        // Split topology (#113): the API's local pool is 0 by design — the standalone worker
        // container's heartbeat is what proves the queue is manned.
        if (!SIM && !workers && !worker_tier_alive) throw new Error('no workers available — the worker service looks down; check Monitor')
        // Sources launches discovery; Discover reports the accepted run's live progress.
        // A rejected request stays on Sources with its inputs and error intact.
        if (view === 'integrations') setView(me?.allow && !me.allow.includes('discover') ? 'overview' : 'discover')
        setLiveScanId(scan_id)
        setDiscoverJobId(job_id)
        setProgress({ phase: accepted.inline ? 'connecting' : 'queued' })
        // Live job state arrives by push instead of the loop below fetching getJob() every
        // tick — a strict reduction in request volume, not just lower latency. onError flips
        // sseFailedRef so the loop degrades to the old per-tick poll for the rest of this scan
        // (proxy stripping SSE, a network hiccup) rather than trusting a connection known dead.
        liveJobStateRef.current = null
        sseFailedRef.current = false
        streamHandle = openDiscoverStream(scan_id, {
          onMessage: (state) => { if (acceptLiveJobState(liveJobStateRef.current, state)) liveJobStateRef.current = state },
          onError: () => { sseFailedRef.current = true },
          // Deliberately NOT flipping sseFailedRef here: onDone means the job finished, not that
          // the connection is broken — liveJobStateRef.current already holds the final state
          // (done: true), which is exactly what the loop should keep reading for its last tick
          // or two until scanPollDecision independently detects settlement via getScan().
        })
        const t0 = Date.now()
        let misses = 0
        let foundOnce = false
        // Adaptive backoff for the fallback getJob() poll ONLY — see fallbackPollBackoff.js.
        // The loop's own 1000ms tick (settlement detection via scanPollDecision) is untouched.
        let fallbackInterval = 1
        let fallbackTicksLeft = 0
        let lastFallbackJob = null
        for (let i = 0; i < 600 && !fresh; i++) {        // up to ~10 min for large estates
          await new Promise((r) => setTimeout(r, 1000))
          // Fan-out scans create the row early with status 'running' and bump files_done as each
          // per-file job lands, so we can show the REAL count ("Analysing documents · 3/5") off
          // the scan row itself — no fabricated phase, no timer-driven bar.
          const elapsed = Math.round((Date.now() - t0) / 1000)
          let g = null
          try { g = await getScan(scan_id); misses = 0; foundOnce = true } catch { g = null; misses++ }
          // Best-effort, read-only, and never allowed to affect the poll's exit decision below —
          // scanPollDecision reads `g`/`scan`, not this. Prefer the SSE-pushed state (no extra
          // request); only fall back to polling getJob() once the stream has proven itself dead,
          // not on its first miss — a single dropped tick must not fall back permanently. A miss
          // here (job TTL'd out of Redis, transient error) just means this tick renders with the
          // coarser scan_runs-derived phase instead of the live one; it must never be mistaken
          // for the scan itself failing.
          let job = liveJobStateRef.current
          if (!job && sseFailedRef.current && job_id) {
            if (fallbackTicksLeft > 0) {
              fallbackTicksLeft--
              job = lastFallbackJob
            } else {
              try { job = await getJob(job_id) } catch { job = null }
              const changed = JSON.stringify(job) !== JSON.stringify(lastFallbackJob)
              fallbackInterval = nextFallbackInterval(changed, fallbackInterval)
              fallbackTicksLeft = fallbackInterval - 1
              lastFallbackJob = job
            }
          }
          // The exit ladder lives in scanPollDecision.js — pure, ordered, and tested. It was four
          // inline branches with no test reachable from anywhere, which is how the fifth (the user
          // pressing Stop) stayed missing: a QUEUED scan that is cancelled has no scan_runs row and
          // never will, so the poll sees exactly what it saw before the cancel — nothing.
          const decision = scanPollDecision({
            cancelled: scanCancelledRef.current, scan: g, foundOnce, misses,
          })
          // A deploy mid-scan drops this tab's identity; the owner-scoped lookup then 404s
          // FOREVER (found live 2026-07-11: silent console spam, banner wedged on
          // "Connecting…"). Persistent misses → say what happened instead of spinning.
          if (decision.action === 'session-lost') {
            window.dispatchEvent(new CustomEvent('acp:session-expired', { detail: { reason:
              'The app was updated and this tab’s session ended. Sign in again — your scan kept running server-side and will be here when you return.' } }))
            return
          }
          // Stopped by the person watching it. A clean return, NOT a throw: the catch below
          // prefixes "scan failed:", and a scan the user deliberately stopped did not fail.
          if (decision.action === 'stopped') {
            setStopped('Scan stopped. Documents already analysed were kept.')
            return
          }
          // Never once claimed after ~45s of trying: say that plainly instead of either the wrong
          // session-expiry message above or spinning silently for the full 10-minute cap.
          if (decision.action === 'never-started') {
            throw new Error('this scan never started — the queue may be stuck. Try again, or check Monitor.')
          }
          // sseFailedRef read live here, not via a state flip — a ref because it changes far more
          // often than the loop renders (see its declaration comment). The fallback getJob() poll
          // above already compensates for the dead stream; this just tells the person watching
          // that the *live push* is down, not just that the number happens to be old (that's what
          // g.run.freshness's own 'checkpoint'/'stale' already cover). A dead SSE stream means we
          // cannot trust anything about currency until it either recovers or the poll settles.
          const freshness = sseFailedRef.current ? 'reconnecting' : (g?.run?.freshness ?? null)
          setProgress(g ? { ...queuedProgress(g, elapsed, job), freshness }
                         : { phase: foundOnce ? 'connecting' : 'queued', elapsed, freshness })
          if (decision.action === 'settled') fresh = decision.scan
        }
        if (!fresh) throw new Error('scan still processing — watch it finish in the Monitor queue')
      } else {
        const { job_id } = await startScan(apiSource, folder, aiEnabled, deepScan, excludeRemediated, incremental, picked, excluded, includeSubfolders)
        if (view === 'integrations') setView(me?.allow && !me.allow.includes('discover') ? 'overview' : 'discover')
        fresh = await pollScanJob(job_id)
      }
      setScan(fresh); setExplicitTimeTravel(false)
      // See reconnectJob's identical line: a fresh successful scan supersedes any stale
      // "scan not available" banner left over from an earlier failed reconnect attempt.
      setScanUnavailable(null)
      // A re-scan is a new run, so nothing from the last one carries into it. The previous scan
      // is NOT lost — it stays in scan_runs and stays selectable in Time-travel, with its own
      // decisions and triage, however far through the workflow it got.
      resetScanScopedState()
      // "Notify me when complete" (slice 3c): if the user armed it and walked away, ping them with the
      // outcome. Best-effort — notifyScanComplete never throws, so a missing notification can't fail the
      // scan it describes.
      if (notifyArmedRef.current) {
        const r = fresh.run || {}
        notifyScanComplete({ assessed: r.files_done || 0, total: r.files || 0, review: r.uncertain || 0 })
      }
      setScanList(await listScans())
      const newAvg = fresh.run.avg_score
      if (prevAvg != null && newAvg != null && newAvg !== prevAvg) { setDelta(newAvg - prevAvg); setDeltaKey((k) => k + 1) }
      // Guide the user into the workflow: land on Discover (step 1) after a scan.
      setView(me?.allow && !me.allow.includes('discover') ? 'overview' : 'discover')
    } catch (e) {
      // A stale 'starting'/'busy' capacity notice from the preflight probe earlier in this same
      // attempt must not keep telling the user "you can scan now" underneath a banner that just
      // said the scan failed for lack of workers — found live 2026-08-28: the two are set at
      // different points (preflight vs. this catch) and nothing cleared the first on failure, so
      // both rendered at once and directly contradicted each other. The failure is the newer,
      // harder signal; it wins.
      setPreflightCapacityState(null)
      if (e?.status === 409 && ['discovery_workflow_active', 'workflow_stage_active', 'recent_compatible_workflow'].includes(e?.detail?.code)) {
        setDiscoveryChoice({
          scanId: e.detail.active_scan_id,
          activeStage: e.detail.active_stage,
          workflowRevision: e.detail.workflow_revision,
          recentCompatible: e.detail.code === 'recent_compatible_workflow',
          source,
          folder,
          runScope,
        })
        setErr(null)
        return
      }
      // An unconfirmed submit already has its own, more accurate surface; a red "scan failed"
      // beside it would contradict it, which is exactly the two-banners-disagreeing bug the
      // comment above was written about.
      // A plain Error from preflight is a known rejection, not a lost submit response.
      if (!submissionUncertain) setErr(`scan failed: ${scanFailureDetail(e?.message ?? e)}`)
      // Same "notify me" arming as the completion path below — a user who opted to walk away
      // wants to know the scan failed just as much as that it finished (see scanNotify.js).
      // A second scanFailureDetail() call, not a shared variable, deliberately keeps the line
      // above byte-for-byte identical to reconnectScan's own catch site — pinned by
      // discoverFailedVsCompleteContradiction.test.js as an exact source-level match.
      if (notifyArmedRef.current) notifyScanFailed(scanFailureDetail(e?.message ?? e))
    } finally {
      // Always close, whatever the loop's own state — the server's generator loop otherwise
      // keeps polling Redis every 250ms for a client that has already stopped listening.
      streamHandle?.close()
      setBusy(false); setProgress(null); setLiveScanId(null); setDiscoverJobId(null)
      setNotifyArmed(false); notifyArmedRef.current = false      // one arming per run
    }
  }

  // Reconnect to an in-flight scan after a page reload — the durable fan-out keeps
  // running server-side, so we just resume polling until it finishes.
  const reconnectScan = async (scan_id, job_id = null) => {
    setBusy(true); setProgress({ phase: 'connecting', elapsed: 0 }); setLiveScanId(scan_id)
    setDiscoverJobId(job_id)
    // Same live-push mechanism doScan uses (see its comment) — scan-ID-anchored, so it survives
    // the worker retrying under a new job_id mid-scan, unlike the old per-tick getJob(job_id)
    // poll below, which pins the job_id captured at reconnect time and 404s forever once the
    // worker actually retries (found live 2026-08-26: hundreds of 404s on a stale job_id while
    // the scan itself kept progressing normally — the "hanging" was the UI going dark, not the
    // backend stalling).
    liveJobStateRef.current = null
    sseFailedRef.current = false
    const streamHandle = openDiscoverStream(scan_id, {
      onMessage: (state) => { if (acceptLiveJobState(liveJobStateRef.current, state)) liveJobStateRef.current = state },
      onError: () => { sseFailedRef.current = true },
    })
    const t0 = Date.now()
    let fresh
    // Same adaptive backoff as doScan — see fallbackPollBackoff.js. Only the fallback
    // getJob() call backs off; this loop's own 1500ms tick is untouched.
    let fallbackInterval = 1
    let fallbackTicksLeft = 0
    let lastFallbackJob = null
    try {
      let misses = 0
      for (let i = 0; i < 600 && !fresh; i++) {
        await new Promise((r) => setTimeout(r, 1500))
        const elapsed = Math.round((Date.now() - t0) / 1000)
        let g = null
        try { g = await getScan(scan_id); misses = 0 } catch { g = null; misses++ }
        // Same fallback rule as doScan: prefer the SSE-pushed state; only poll getJob() once the
        // stream has proven itself dead, not on its first miss. Never affects this loop's exit
        // decision, only which phase this tick renders with.
        let job = liveJobStateRef.current
        if (!job && sseFailedRef.current && job_id) {
          if (fallbackTicksLeft > 0) {
            fallbackTicksLeft--
            job = lastFallbackJob
          } else {
            try { job = await getJob(job_id) } catch { job = null }
            const changed = JSON.stringify(job) !== JSON.stringify(lastFallbackJob)
            fallbackInterval = nextFallbackInterval(changed, fallbackInterval)
            fallbackTicksLeft = fallbackInterval - 1
            lastFallbackJob = job
          }
        }
        // A reconnected scan HAS a scan_runs row, so a server-side cancel already settles this
        // loop through the status check below — this is not the queued-scan gap. It is here so
        // Stop ends the poll on the same tick on both paths instead of after another round-trip,
        // and so a cancel the server refused still stops this tab polling. The miss thresholds
        // here are deliberately left alone: reconnect has no foundOnce notion (it only ever runs
        // for a scan that already existed), so scanPollDecision's gating would not fit.
        if (scanCancelledRef.current) {
          setStopped('Scan stopped. Documents already analysed were kept.')
          return
        }
        // Same deploy-dropped-identity guard as doScan: persistent owner-scoped 404s mean
        // this tab can no longer see its scan — say so instead of spinning forever.
        if (misses >= 8) {
          window.dispatchEvent(new CustomEvent('acp:session-expired', { detail: { reason:
            'The app was updated and this tab’s session ended. Sign in again — your scan kept running server-side and will be here when you return.' } }))
          return
        }
        // Same reasoning as doScan's identical line: sseFailedRef read live, ref not state.
        const freshness = sseFailedRef.current ? 'reconnecting' : (g?.run?.freshness ?? null)
        setProgress(g ? { ...queuedProgress(g, elapsed, job), freshness }
                       : { phase: 'connecting', elapsed, freshness })
        if (g && g.run && g.run.status !== 'running') fresh = g
      }
      // Same reset as doScan. Usually a no-op — a reconnect follows a page reload, where React
      // state started empty anyway — but this runs from a startup effect that can land while a
      // different scan is already on screen, and a fourth path that resets a different subset is
      // exactly how the three before it drifted apart.
      // See reconnectJob's identical line: a fresh successful scan supersedes any stale
      // "scan not available" banner left over from an earlier failed reconnect attempt.
      if (fresh) { setScan(fresh); setExplicitTimeTravel(false); setScanUnavailable(null); resetScanScopedState(); setScanList(await listScans()); setView('overview') }
    } catch { /* best-effort reconnect */ }
    finally { streamHandle.close(); setBusy(false); setProgress(null); setLiveScanId(null); setDiscoverJobId(null) }
  }

  const run = scan?.run

  const trendData = [...scanList].reverse().filter((s) => s.avg_score != null)
    .map((s) => ({ score: s.avg_score, label: s.completed_at ? new Date(s.completed_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '' }))
  const trend = trendData.map((d) => d.score)
  const trendDates = trendData.map((d) => d.label)
  // Decisions ratified on the Discover action plan, rolled up by effective action
  // so they can flow into Remediate (queue) and Report (evidence).
  const ratified = files.reduce((acc, f) => {
    const d = decisions[f.file]; if (!d || d.state === 'rejected') return acc
    const action = d.state === 'override' ? d.action : f.rec?.action
    if (!action) return acc
    acc[action] = (acc[action] || 0) + 1; acc.total += 1
    return acc
  }, { auto: 0, assisted: 0, review: 0, archive: 0, keep: 0, manual: 0, total: 0 })
  // OV-02: one action, no numbers. EmptyState no longer configures anything — the criteria
  // and file-type pickers it used to render belong after an inventory exists, in Assess,
  // where the eligible-file count can be shown against them.
  const placeholder = bootError
    ? (
      <section className="empty" role="alert">
        <div className="emptyicon" aria-hidden="true">⚠</div>
        <h3 className="emptytitle">Couldn’t load your workspace</h3>
        <p className="muted emptysub">
          {bootError} Nothing was lost — this is usually brief. Your documents and decisions are unaffected.
        </p>
        <div className="emptyactions">
          <button onClick={() => setBootAttempt((n) => n + 1)}>Try again</button>
        </div>
      </section>
    )
    : loaded
    ? <EmptyState onGoToSource={() => { setView('integrations'); window.scrollTo({ top: 0, behavior: 'smooth' }) }} />
    : <Loading stage={loadStage} preview={overviewPreview} />
  // The scan panel renders inside whichever view is open, so scope its narration to that view
  // when the view is a pipeline step that owns scan phases. The view ids ARE the step names in
  // PHASE_STEP ('discover', 'assess'); anything else is a non-step view and narrates the job.
  const narrationStep = NARRATION_STEPS.has(view) ? view : null
  // Presentation decouple: results views stay blank until the user runs Assess. The flag
  // is persisted on the scan (assessed_at); justAssessed gives an immediate optimistic flip.
  const assessed = !!run?.assessed_at || justAssessed === run?.id
  // When the Assess results (summary / worklist / file / run-details) may render. Two ways to be
  // ready, and the distinction is the bug this guards: (1) the runner finished a run THIS session
  // (assessPhase 'done'); (2) the scan was assessed in a PRIOR session — persisted `assessed_at`,
  // and NOT this session's optimistic `justAssessed` flip. Case 2 is the reload: assessPhase is
  // 'idle' because AssessRunner's per-session cache is gone, so gating purely on 'done' left an
  // already-assessed scan showing an EMPTY panel (AssessSetup hidden by `assessed`, results hidden
  // by the 'done' gate). Excluding `justAssessed === run.id` keeps results hidden during the
  // click→running gap the 'done' gate was added to cover, so a fresh run still waits for the bar.
  // A persisted assessed_at belongs to the LAST completed assessment. It is safe to show only
  // while the runner is idle. Once a new run starts, leaving this fallback active keeps the old
  // dashboard on screen until the first live poll lands — a few seconds in production, long
  // enough to make the new run look as though it returned stale results. `starting` closes the
  // click-to-child-effect gap; `running` keeps the old snapshot hidden for the whole new run.
  const resultsReady = assessPhase === 'done' || (priorResults.assess && assessed)
    || (assessPhase === 'idle' && !!run?.assessed_at && justAssessed !== run?.id)
  // runIntegrity is a plain function, not a hook, so it is safe here — below the `if (!me)`
  // early return. The FETCH is not, and lives above it; see useScanManifest's call site.
  //
  // `currentScanId` is the load-bearing staleness check, not `runInFlight`. resultsReady already
  // hides the old snapshot for the whole of a new run (see the comment above), so the reachable
  // staleness is the quieter kind: a manifest fetched for the previous scan still in state when
  // the screen has moved to another one, which survives a reload where a "something is running"
  // flag does not.
  const runVerdict = runIntegrity(runManifest.manifest, {
    error: runManifest.error,
    loading: runManifest.loading,
    runInFlight: assessPhase === 'starting' || assessPhase === 'running',
    manifestScanId: runManifest.manifest?.scan_id ?? null,
    currentScanId: run?.id ?? null,
  })
  const assessGate = <AssessGate onGo={() => { setView('assess'); window.scrollTo({ top: 0, behavior: 'smooth' }) }} />
  // Time-travel = viewing a PAST scan. "Not the newest entry in the picker" is not the same
  // thing as "not the preferred workspace default": that default may be an older verified or
  // full-size run. The picker and this mode both use chronological newest-first order.
  // listScans() returns only runs with a completed_at,
  // so a run that is still in flight — or an ADR 0020 Discover-only run, whose status leaves
  // 'running' the moment discovery ends — is absent from scanList entirely and satisfied the
  // old `scanList[0]?.id !== run.id` test. The banner then announced a replay of the NEWEST
  // scan and rendered "viewing the scan from ." with no date, because there was no
  // completed_at to format. Worse than cosmetic: isTimeTravel also drives readOnly on
  // Remediate/Publish/Monitor, so a freshly-discovered estate came up locked.
  // Requiring membership in scanList is what makes this mean "a past scan" — completed_at is
  // then guaranteed present, since that is exactly what listScans() filters on.
  const isTimeTravel = isHistoricalScan(scanList, run?.id)
  const narrowDefault = !explicitTimeTravel ? narrowScanDefaultContext(scanList, run?.id) : null
  // A background scan can finish while another stage is active. The persistent stage card is
  // the useful foreground truth in that moment; stacking "New scan available" above it makes
  // the completed inventory sound like a second live run. Keep deliberate replay and the narrow-
  // scan safeguard visible, but defer the passive new-scan notice until active work is finished.
  // A narrow run remains available in Scan History but no longer gets a separate status banner.
  // The durable Discover / Assess / Remediate card directly below the tabs owns workflow status;
  // stacking a scope-policy explanation above it made the newest run look like a second alert.
  const showScanHistoryBanner = isTimeTravel && !narrowDefault
    && (explicitTimeTravel || !primaryWorkflow)
  // The workflow tablist's roving tab stop. Normally the selected tab (`view`). But `view` can be
  // a tab the role no longer shows — an administrator hid it mid-session and the panel now reads
  // Access restricted — and then no rendered tab matched, the tablist had no tab stop at all, and
  // the panel was labelled by the id of a button that no longer exists. So: fall back to the first
  // rendered tab that is not locked (the arrow-key handler skips disabled tabs, and a disabled
  // button cannot take focus). This only decides which tab Tab lands on; it selects nothing and
  // does not navigate, so the Access restricted screen stays. If every rendered tab is locked, or
  // none is rendered, no tab claims the stop — there is nothing focusable to claim it.
  const workflowTabs = visibleTabs(access, TABS)
  const workflowTabLocked = (k, step) => busy && step > 0 && k !== 'discover' && view !== k
  const viewTabRendered = workflowTabs.some(([k]) => k === view)
  const workflowTabStop = viewTabRendered ? view
    : (workflowTabs.find(([k, , , step]) => !workflowTabLocked(k, step)) || [])[0] ?? null
  // Name for the tabpanel when its tab is not rendered, instead of aria-labelledby pointing at an
  // id that is not in the document. Mirrors the heading AccessRestricted shows in that case.
  const viewLabel = (TABS.find(([k]) => k === view) || [])[1] || view
  const workflowPanelLabel = viewTabRendered ? undefined
    : isVisible(access, view) ? viewLabel
      : isAccessPending(access) ? 'Access pending'
        : `Access restricted: ${viewLabel}`


  return (
    <>
    {isStaging && (
      <div role="status" style={{
        background: '#B45309', borderBottom: '2px solid #92400E',
        padding: '6px 16px', fontSize: 12, fontWeight: 700, color: '#fff',
        letterSpacing: '0.08em', textTransform: 'uppercase', textAlign: 'center', userSelect: 'none'
      }}>
        Staging — not production
      </div>
    )}
    <div className={`app${isTimeTravel ? ' replaymode' : ''}`}>
      <a className="skiplink" href="#main-content">Skip to main content</a>
      <header>
        <div className="brand"><Logo /><h1 className="sub">Accessibility Platform</h1>
        </div>
        <div className="header-actions">
          {/* Process-health chip — visible only once a discovery run has completed. Shows
              exceptions only: worker failure > unreadable files. Healthy is the quiet default.
              Uses allFiles (not files) so the type-filter never hides error-status rows from
              the count, and the indicator describes the full run rather than the current view. */}
          {run?.completed_at && (() => {
            const unreadable = unreadableFiles
            if (!workerError && unreadable === 0) return null
            const [chipColor, chipBg, chipLabel, chipTip] = workerError
              ? ['#7A271A', '#FEF3F2', 'Worker error', 'Assessment stopped due to a processing failure. Some files were not scored.']
              : ['#6B3A00', '#FFF7E6', `${unreadable} unreadable`, `${unreadable} file${unreadable !== 1 ? 's' : ''} could not be opened and were skipped.`]
            return (
              <span title={chipTip} style={{
                fontSize: 11, fontWeight: 700, letterSpacing: '0.04em',
                padding: '2px 8px', borderRadius: 20,
                background: chipBg, color: chipColor,
                border: `1px solid ${chipColor}22`,
                cursor: 'default', userSelect: 'none'
              }}>
                {chipLabel}
              </span>
            )
          })()}
          <HitlBell />
          <details className="header-menu accessibility-menu">
            <summary aria-label="Accessibility and AI preferences">
              <span aria-hidden="true">◉</span> Accessibility
            </summary>
            <div className="header-menu-panel" role="group" aria-label="Accessibility and AI preferences">
              <div className="header-menu-heading">Display and assistance</div>
              <button className={`menu-setting${wcagMode ? ' menu-setting--on' : ''}`}
                onClick={() => setWcagMode(v => !v)} aria-pressed={wcagMode}>
                <span><b>High-contrast palette</b><small>Apply accessible colours across every tab</small></span>
                <span aria-hidden="true">{wcagMode ? 'On' : 'Off'}</span>
              </button>
              <button className={`menu-setting${aiEnabled ? ' menu-setting--on' : ''}`}
                onClick={() => setAiEnabled(v => !v)} aria-pressed={aiEnabled}>
                <span><b>AI assistance</b><small>{aiEnabled ? 'Explanations and drafting enabled' : 'Deterministic rules only'}</small></span>
                <span aria-hidden="true">{aiEnabled ? 'On' : 'Off'}</span>
              </button>
              <PrivateAiBadge aiEnabled={aiEnabled} />
            </div>
          </details>
          <details className="header-menu account-menu" ref={accountMenuRef}>
            <summary aria-label={`Account menu for ${me.email}`}>
              <span className="account-avatar" aria-hidden="true">{(me.name || me.email || '?').split(/\s|@/).filter(Boolean).slice(0, 2).map(s => s[0]).join('').toUpperCase()}</span>
              <span className="account-chevron" aria-hidden="true">⌄</span>
            </summary>
            <div className="header-menu-panel account-panel">
              <div className="account-identity">
                <b>{me.name || me.email}{' '}
                  <span aria-label={`Timezone: ${timezoneBadge(userTimezone).name}`}
                    title={`Release folder timezone: ${userTimezone}`}
                    style={{ display: 'inline-block', padding: '1px 6px', borderRadius: 999,
                      background: 'var(--bg)', border: '1px solid var(--line)', color: 'var(--muted)',
                      fontSize: 10.5, lineHeight: 1.4, verticalAlign: '1px' }}>
                    {timezoneBadge(userTimezone).short}
                  </span>
                </b>
                <span>{me.email}</span>
              </div>
              {me.role && <div className="account-meta"><span>Role</span><b>{me.role}</b></div>}
              {rubric && <div className="account-meta"><span>Rubric</span><b>{rubric.target} · {rubric.hash.slice(0, 8)}</b></div>}
              <div className="account-meta"><span>Build</span><b title={`Built ${fmtStamp(__BUILD_TIME__)} (your local time)`}>
                {void tick}v{platformVersion || __BUILD_VERSION__} PT · {timeAgo(__BUILD_TIME__)}
              </b></div>
              <div className="menu-separator" />
              <button className="menu-action" onClick={() => setMyDataOpen(true)}>Reset my data</button>
              {canOpenSettings(me, access) && <button className="menu-action" aria-label="Platform settings" onClick={() => setSettingsOpen(true)}>⚙ <span>Settings</span></button>}
          {/* SWITCH ACCOUNT — a full teardown, then the sign-in screen, which now asks Google
              and Microsoft for an account chooser rather than reusing the browser's single
              signed-in session.

              It cannot be a lighter-touch "just re-pick the Drive": the ACP identity is the
              Authorization bearer (api.js sends Google's when present, else Microsoft's) and
              api/app.py stamps request.state.user_email from it, which is what per-user data
              isolation keys on. Re-minting only the Drive token would leave you signed in as one
              account while reading another's Drive, with the scans owned by the first — the
              wrong half of a switch, and invisible. So this does the same complete teardown as
              sign out and differs only in saying what it is for. */}
              <button className="menu-action" title="Sign out and choose a different Google or Microsoft account"
                  onClick={async () => {
            // Best-effort: clear the running scan's backend token store so the worker
            // doesn't keep credentials that are about to become invalid.
            try { const a = await getActiveScan(); if (a?.id) await clearScanTokens(a.id) } catch { /* ignore */ }
            clearAllTokens()
            clearActivityStorage()
            try { sessionStorage.clear() } catch { /* ignore */ }
            window.location.reload()
              }}>⇄ <span className="menu-action-label">switch account</span></button>
              <button className="menu-action menu-action--danger" onClick={async () => {
            // Best-effort: clear the running scan's backend token store so the worker
            // doesn't keep credentials that are about to become invalid.
            try { const a = await getActiveScan(); if (a?.id) await clearScanTokens(a.id) } catch { /* ignore */ }
            clearAllTokens()
            clearActivityStorage()
            // Hard reload guarantees a 100% fresh in-memory state for whoever signs in next
            // on this browser — no scan, decisions, assess phase, or files survive.
            try { sessionStorage.clear() } catch { /* ignore */ }
            window.location.reload()
              }}>↪ <span className="menu-action-label">sign out</span></button>
            </div>
          </details>
        </div>
      </header>
      {backendDown && (() => {
        const secSince = backendLastChecked ? Math.round((Date.now() - backendLastChecked) / 1000) : null
        const checkedLabel = secSince === null ? '' : secSince < 5 ? 'just now' : secSince < 60 ? `${secSince}s ago` : `${Math.floor(secSince / 60)}m ago`
        const extended = backendRetries > 3
        return (
          <div role="alert" aria-label="Service temporarily unavailable" style={{
            background: '#fffdf5', borderLeft: '4px solid #f59e0b', borderBottom: '1px solid #fde68a',
            padding: '9px 20px', display: 'flex', alignItems: 'center', gap: 10,
            fontSize: 13.5, color: '#1c1917',
          }}>
            <span aria-hidden="true" style={{ fontSize: 15, lineHeight: 1, flexShrink: 0, color: '#d97706' }}>⚠</span>
            <div style={{ flex: 1 }}>
              <span style={{ fontWeight: 600 }}>{extended ? 'ACP remains unavailable' : 'Reconnecting to ACP'}</span>
              {' — '}
              <span style={{ color: '#57534e' }}>
                Scans and data updates are temporarily paused.
                {checkedLabel && <span style={{ marginLeft: 6, color: '#78716c' }}>Last checked {checkedLabel}.</span>}
              </span>
            </div>
            <button
              onClick={() => {
                setBackendRetrying(true)
                checkHealth().then((ok) => {
                  setBackendLastChecked(Date.now())
                  setBackendDown(!ok)
                  if (ok) {
                    if (backendWasDown.current) { setBackendRestored(true); setTimeout(() => setBackendRestored(false), 4000) }
                    backendWasDown.current = false
                    setBackendRetries(0)
                  } else {
                    backendWasDown.current = true
                    setBackendRetries((n) => n + 1)
                  }
                  setBackendRetrying(false)
                })
              }}
              disabled={backendRetrying}
              style={{
                flexShrink: 0, border: '1px solid #d97706', borderRadius: 5,
                background: 'white', padding: '4px 12px', cursor: backendRetrying ? 'default' : 'pointer',
                fontSize: 13, color: '#92400e', fontWeight: 500, opacity: backendRetrying ? 0.6 : 1,
              }}
            >
              {backendRetrying ? 'Retrying…' : 'Retry now'}
            </button>
          </div>
        )
      })()}
      {backendRestored && (
        <div role="status" style={{
          background: '#f0fdf4', borderLeft: '4px solid #22c55e', borderBottom: '1px solid #bbf7d0',
          padding: '9px 20px', display: 'flex', alignItems: 'center', gap: 10,
          fontSize: 13.5, color: '#14532d',
        }}>
          <span aria-hidden="true" style={{ fontSize: 15, color: '#16a34a' }}>✓</span>
          <span><strong>Connection restored</strong> — Scans and data updates are available again.</span>
        </div>
      )}
      {tokenRefreshError && (
        <div role="alert" style={{
          background: '#fffbeb', borderBottom: '2px solid #f59e0b',
          padding: '10px 20px', display: 'flex', alignItems: 'center', gap: 10,
          fontSize: 14, color: '#78350f',
        }}>
          <span aria-hidden="true">⚠️</span>
          <span style={{ flex: 1 }}>{tokenRefreshError}</span>
          <button onClick={() => setTokenRefreshError(null)} aria-label="Dismiss session warning"
            style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 18, color: '#92400e', lineHeight: 1, padding: '0 4px' }}>
            ✕
          </button>
        </div>
      )}
      <div className="header-context" aria-label="Current workspace context">
        {me.scope && <span><i className="scopedot" /><b>{me.scope}</b></span>}
        {rubric && <span><b>{rubric.target}</b></span>}
        {allFiles.length > 0 && <span><b>{allFiles.length.toLocaleString()}</b> documents</span>}
        {/* ✓ Verified is the healthy state, which this header now reports by SILENCE in the chip
            row above — so it has to mean the same thing the chip's absence means, or the header
            contradicts itself. Gated on `run.status !== 'failed'` alone it did not: a run with
            unreadable files showed the amber "N unreadable" chip AND "✓ Verified" at the same
            time, one saying documents were skipped and the other that everything checked out.
            The chip was always a single highest-severity signal (worker error > unreadable >
            healthy); moving the healthy end of it down here has to preserve that ordering. */}
        {run?.completed_at && !workerError && unreadableFiles === 0 && (
          <span className="context-verified">✓ Verified</span>
        )}
      </div>

      <nav aria-label="Compliance workflow">
        <div className="tabs" role="tablist" aria-label="Compliance workflow">
          {/* ONE MECHANISM, NOT TWO. #1287 opened every tab to every signed-in user by deleting
              the legacy `me.allow` persona filter from this line; the owner's 2026-09-04 decision
              replaces that blanket opening with a REASON — every signed-in user holds the default
              Platform User role, which grants every current tab (api/workspace_rbac.py).

              So the filter stays gone and this reads only the server's answer. The difference is
              that the openness is now something a role says and an administrator can narrow,
              rather than a property of the navigation nobody can change; and it is enforced
              server-side, which a tab filter never was. With RBAC off, `access` is null and
              everything renders — the same as today. */}
          {visibleTabs(access, TABS).map(([k, label, rg, step]) => {
            const stageDone = {
              // A scan record existing is not the same as discovery having FINISHED — `run` is
              // truthy the instant a scan starts (even mid-listing, `status='running'`), so `!!run`
              // put a checkmark on Discover while a scan was still in flight. The backend's own
              // definition of "a completed discovery run" (list_scans: `completed_at IS NOT NULL`,
              // the same gate /assess/eligibility reads) is completed_at — match it here, so the
              // nav tab and the Assess tab's "no discovery run yet" message can never disagree.
              discover: !!run?.completed_at,
              assess: assessed,
              remediate: files.some((f) => f.remediated_at || f.drive_write_url),
              publish: (publishedFiles?.length || 0) > 0,
              monitor: (publishedFiles?.length || 0) > 0,
            }
            const done = !!stageDone[k] && view !== k
            // While a scan/assessment is running, lock the OTHER numbered workflow steps: jumping
            // to Assess mid-scan would show the previous scan's data, not the one in flight. The
            // current view + the utility tabs (step 0) stay reachable.
            //
            // `k !== 'discover'`: `busy` is exclusively the Discover-scan flag (never set by
            // AssessRunner — see the note on the runinfo section below), so Discover is never
            // "other" relative to the thing making busy true. Without this, navigating away from
            // Discover during a scan (to Overview, a step-0 tab that stays reachable) locked
            // Discover itself out until the scan finished — the one tab a user watching a running
            // scan would most want to click back into became the one tab they couldn't reach.
            // Found live 2026-08-28 while adding the live-scan nav badge just below: the badge
            // would have pointed at a tab nothing could open.
            const locked = workflowTabLocked(k, step)
            return (
              <button key={k} id={`workflow-tab-${k}`} role="tab" aria-selected={view === k}
                      aria-controls="workflow-panel"
                      aria-current={view === k ? 'step' : undefined}
                      tabIndex={workflowTabStop === k ? 0 : -1}
                      disabled={locked}
                      title={locked ? 'A scan or assessment is running — this step opens when it finishes' : rg}
                      className={`tab${view === k ? ' on' : ''}${done ? ' done' : ''}${step ? ' stepTab' : ''}${locked ? ' locked' : ''}`}
                      onKeyDown={handleWorkflowTabKeyDown}
                      onClick={() => goToView(k)}>
                {step > 0 && <span className="stepnum" aria-hidden="true">{done ? '✓' : step}</span>}
                <span className="tablbl">{done && <span className="vh">completed: </span>}{label}</span>
                <span className="rg">{rg}</span>
                {k === 'remediate' && hitlCount > 0 && <span title={reviewBadgeTitle(hitlCount)} style={{ marginLeft: 6, fontSize: 10.5, fontWeight: 700, minWidth: 16, height: 16, lineHeight: '16px', textAlign: 'center', padding: '0 5px', borderRadius: 9, background: '#B4690E', color: '#fff', display: 'inline-block' }}>{hitlCount}</span>}
                {/* Every fix so far for "does the user know their scan is still running" lived
                    entirely inside the Discover tab body — a user who navigates to Overview or
                    Assess while a scan is queued/running saw nothing anywhere telling them so,
                    and (until the `locked` fix just above) could not even click back into
                    Discover to check. `.pulsedot` is the same green live-indicator already used
                    for a running job elsewhere (QueuePanel, Monitor's audit trail, and the
                    Discover progress card itself per #916) — reused here, not reinvented, so it
                    reads as the same signal wherever it appears. Shown only while NOT already on
                    Discover: the tab body's own cards already say this far more richly there. */}
                {k === 'discover' && busy && view !== 'discover' && (
                  <span title="A scan is running — open Discover to see progress" role="status"
                        style={{ marginLeft: 6, display: 'inline-flex', alignItems: 'center' }}>
                    <span className="pulsedot" aria-hidden="true" />
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </nav>
      <div className="runinfo">
        {scanList.length > 0 && (
          <div className="runinfo-stamp">
            <span className="runinfo-ago" title={fmtStamp(run?.completed_at)}>
              {void tick /* re-render every minute */}
              {timeAgo(run?.completed_at) ?? '—'}
            </span>
            <span className="runinfo-abs">{fmtStamp(run?.completed_at) ?? '—'}</span>
            {run?.source && <span className="runinfo-source">{run.source}</span>}
            {/* NOT the same population as the Discover completion card's "N files inventoried" —
                by design, not staleness. run.files (scan_runs.files) is set from `_list()`'s
                RETURN value (scanner.py's _search_drive/_search_folder), which is already
                filtered to scannable MIME types; scope.inventory.discovered counts the whole
                estate, every file type, via a SEPARATE `inventory_out` accumulator inside the
                same call. A Drive full of photos/videos alongside a smaller set of real
                documents legitimately produces two correct, differently-scoped numbers here —
                found live 2026-08-29 as an apparent contradiction (1,033 here vs. 6,922 on the
                completion card, same scan) that traced to exactly this, not a bug in either
                count. Labelled "scannable" — the same word scope.scannable already uses
                internally (scanner.py) — so the difference reads as two facts, not one wrong
                one. */}
            {run?.files != null && <span className="muted">{run.files.toLocaleString()} scannable documents</span>}
            {run?.published_at && (
              <span style={{ fontSize: 11.5, color: 'var(--green,#1a7f45)', whiteSpace: 'nowrap' }}
                    title={`Enumeration verified complete — ${fmtStamp(run.published_at)}`}>
                ✓ Verified
              </span>
            )}
          </div>
        )}
        {scanList.length > 1 && (
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
              <span className="muted" title="Scan History — pick any past scan and every tab, dashboard and score reflects that point in time">🕐 Scan History:</span>
              <select
                value={scan?.run?.id || ''}
                onChange={(e) => switchScan(e.target.value)}
                disabled={scanLoading || busy}
                aria-label="Select scan run"
                style={{ fontSize: 12, padding: '2px 6px', borderRadius: 4, border: '1px solid var(--line)', background: 'var(--surface)', color: 'inherit', cursor: 'pointer' }}
              >
                {scanList.map((s, i) => {
                  // scanOptionDate.js: `at` is completed_at when assessed, discovered_at (ADR
                  // 0020) when only discovered, null when neither is set. Falls back to "not yet
                  // dated" rather than `new Date(null)`, which silently renders as the Unix
                  // epoch — "Dec 31, 4:00 PM" in Pacific time — found live 2026-08-29 in an
                  // unassessed scan's own picker entry, from this label reading completed_at
                  // alone.
                  const at = scanOptionAt(s)
                  const workflowContext = scanWorkflowContext(s)
                  return (
                    <option key={s.id} value={s.id}>
                      {i === 0 ? '★ ' : ''}
                      {at ? new Date(at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'not yet dated'}
                      {s.avg_score != null ? ` · ${s.avg_score}/100` : ''}
                      {' · '}{at ? timeAgo(at) : ''}{workflowContext ? ` · ${workflowContext}` : ''}{i === 0 ? ' · latest' : ''}{s.published_at ? ' · verified' : ''}
                    </option>
                  )
                })}
              </select>
              {scanLoading && <span className="spinner" />}
            </label>
          )}
          {run && (Object.keys(decisions).length + Object.keys(triage).length) > 0 && (
            <span className="muted" style={{ marginLeft: 12, fontSize: 12, color: 'var(--success-fg)', whiteSpace: 'nowrap' }}
                  title="Your triage + remediation decisions are saved to this scan and restored when you switch to it in Scan History">
              ✓ {Object.keys(decisions).length + Object.keys(triage).length} decision{(Object.keys(decisions).length + Object.keys(triage).length) !== 1 ? 's' : ''} saved
            </span>
          )}
      </div>

      {showScanHistoryBanner && (
        <div className="ttbanner" role="status">
          {explicitTimeTravel ? (
            <>
              {/* fmtStamp returns null for a missing stamp; the guard on isTimeTravel means that
                  can no longer happen here, but the fallback stays so a null can never again
                  render as a bold empty span followed by a bare period. */}
              <span style={{ fontSize: 13.5 }}>🕐 <b>Viewing an earlier scan</b> — {scanWorkflowContext(run) ? <><b>{scanWorkflowContext(run)}</b> from </> : 'results from '}<b>{fmtStamp(run.completed_at) ?? 'an earlier date'}</b>{run.avg_score != null ? ` · ${run.avg_score}/100` : ''}. Changes are unavailable until you return to the latest scan.</span>
            </>
          ) : (
            <span style={{ fontSize: 13.5 }}>✨ <b>New scan available</b> from <b>{fmtStamp(scanList[0]?.completed_at) ?? 'just now'}</b> — a more recent scan finished while you were reviewing this one.</span>
          )}
          <button className="ttexit" onClick={() => switchScan(scanList[0].id)}>
            ↩ Return to latest scan
          </button>
        </div>
      )}

      {/* A failed scan attempt never clears `scan` — setScan(fresh) only runs on success — so the
          previous run's own completed results routinely stay on screen directly under this
          banner. Found live 2026-08-29: "scan failed: 500" over a "Discovery complete" card reads
          as a flat contradiction with nothing explaining that the two describe different
          attempts. Naming the earlier run's timestamp says explicitly what "unaffected" means,
          rather than asking the reader to infer it from a card that never changed. */}
      {err && (
        <div className="err" role="alert">
          <div>{err}</div>
          {hasFallbackInventory(run?.discovered_at, run?.completed_at) && (
            <div style={{ fontWeight: 400, fontSize: 12.5, marginTop: 3 }}>
              Your previous inventory from {fmtStamp(run.discovered_at || run.completed_at)} is unaffected and still shown below.
            </div>
          )}
          {/* The scan_id of THIS failed attempt, for correlating with Monitor/support — never
              run?.id (a different, unrelated scan: the one whose results are still on screen,
              per the reassurance line above). liveScanId is only set once startScanQueued
              returns (App.jsx's doScan), so a failure before that point — a blocked preflight
              check, "no workers available" — has no attempt-specific id to show and this is
              correctly omitted rather than showing an id that isn't the failed attempt's own. */}
          {liveScanId && (
            <div style={{ fontWeight: 400, fontSize: 11.5, marginTop: 3, fontFamily: 'var(--font-mono)' }}>
              Scan ID: {liveScanId}
            </div>
          )}
        </div>
      )}
      {/* A submit whose response was lost. Reported as STATUS, not as an alert: we do not know
          that anything failed, and the red "scan failed" treatment would push the user toward
          starting a second scan by hand — the one action that could actually duplicate work.
          Found in production 2026-09-01: a Discovery submitted during a deploy never reached a
          replica, and with no timeout on the call the page sat in "Submitting Discovery" forever,
          with no console error and nothing to retry. */}
      {submitUncertain && (
        <div className="panel scanstopped" role="status" style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>
            {submitUncertain.timedOut
              ? 'The server did not confirm your scan in time'
              : 'The server did not confirm your scan'}
          </div>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 3 }}>
            It may or may not have started — the request was sent but no answer came back.
            Trying again is safe: it reconciles to the same scan rather than starting a second one.
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
            <button className="ghost small"
                    onClick={() => { const a = submitUncertain; setSubmitUncertain(null)
                                     doScan(a.source, a.folder, a.runScope) }}>
              Try again
            </button>
            <button className="ghost small" onClick={() => setView('monitor')}>Check Monitor</button>
            <button className="ghost small" onClick={() => setSubmitUncertain(null)}>Dismiss</button>
          </div>
        </div>
      )}
      {/* A deliberate stop is not a fault, so it is reported as status rather than as an alert —
          role="status" and the muted panel treatment, not role="alert" and the red one. */}
      {stopped && (
        <div className="panel scanstopped" role="status" style={{ marginBottom: 12 }}>
          <span style={{ fontSize: 13 }}>■ {stopped}</span>
          <button className="ghost small" style={{ marginLeft: 10 }}
                  onClick={() => setStopped(null)}>Dismiss</button>
        </div>
      )}
      <DiscoveryContinuityChoice
        choice={discoveryChoice && (discoveryChoice.recentCompatible
          || (discoveryChoice.activeStage || 'discover') === 'discover') ? discoveryChoice : null}
        onContinue={() => {
          const scanId = discoveryChoice?.scanId
          const activeStage = discoveryChoice?.activeStage || 'discover'
          setDiscoveryChoice(null)
          if (scanId) switchScan(scanId)
          goToView(activeStage)
          window.scrollTo({ top: 0, behavior: 'smooth' })
        }}
        onReplace={() => {
          const pending = discoveryChoice
          setDiscoveryChoice(null)
          if (pending) doScan(pending.source, pending.folder, pending.runScope, true)
        }}
        onDismiss={() => setDiscoveryChoice(null)}
      />
      <StageStartConflictDialog
        choice={discoveryChoice && !discoveryChoice.recentCompatible
          && (discoveryChoice.activeStage || 'discover') !== 'discover' ? discoveryChoice : null}
        onContinue={() => {
          const scanId = discoveryChoice?.scanId
          const activeStage = discoveryChoice?.activeStage || 'discover'
          setDiscoveryChoice(null)
          if (scanId) switchScan(scanId)
          goToView(activeStage)
          window.scrollTo({ top: 0, behavior: 'smooth' })
        }}
        onRestart={() => {
          const pending = discoveryChoice
          setDiscoveryChoice(null)
          if (pending) doScan(pending.source, pending.folder, pending.runScope, true)
        }}
        onCancel={() => setDiscoveryChoice(null)}
      />
      {/* Assessment has a real live card immediately below this fallback. Do not stack a
          generic “still running” banner above the richer card for the same work. */}
      <WorkflowContinuityBanner
        workflow={primaryWorkflow?.stage === 'assess'
          ? null : primaryWorkflow}
        canonicalAvailable={canonicalStage?.stage === 'release'}
        currentView={view}
        onReturn={(stage) => { goToView(stage); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
        onViewPrevious={(scanId) => { switchScan(scanId); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
        onLiveOps={() => { goToView('liveops'); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
      />
      {view !== 'overview' && <WorkflowStageStack activeTab={view} defaultCollapsed={false} releaseReviewWorkspace={<ReleaseReviewWorkspace scanId={canonicalRun.lineage?.scan_id} remediationRunId={remediationStage?.execution_id} refreshRevision={remRun.snapshot?.revision}
        onOpenReview={() => { const url=new URL(window.location.href);url.searchParams.set('tab','remediate');url.searchParams.set('mode','review');history.replaceState({},'',url);goToView('remediate') }} />}
        lineage={canonicalRun.lineage} receivedAt={canonicalRun.receivedAt} progressHostId={remediationProgressHostId}
        assessmentActivity={assessmentActivity}
        discoveryScope={{scanId: run?.id, scope: run?.scope}}
        assessmentFindings={assessed && resultsReady ? { scanId: run?.id, rows: assessNavRows, total: assessNavRows.reduce((sum, row) => sum + row.totalFindings, 0) } : null}
        activeStage={view === 'publish' ? 'release'
          : ['discover', 'assess', 'remediate'].includes(view) ? view : null}
        stageDetails={{
          discover: canonicalStage?.stage === 'discover' && busy && progress
            && (!canonicalScanId || liveScanId === canonicalScanId) ? (
            <DiscoverRunProgress progress={progress} busy={busy} sources={sources}
              source={progress?.source ?? null} scope={progress?.scope ?? null}
              inv={inventorySnapshot({ run: liveScanId === run?.id ? run : null,
                inventory: progress?.inventory ?? progress?.scope?.inventory ?? null })}
              onStop={canonicalScanId ? () => stopScan(canonicalScanId) : undefined}
              onReview={() => { setView('discover'); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
              onContinue={() => { setView('assess'); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
              preflightDegraded={preflightDegraded} freshness={progress?.freshness ?? null}
              runStartedAt={progress?.started_at ?? null} />
          ) : null,
          assess: canonicalStage?.stage === 'assess' ? (
            <LiveAssessmentLive scanId={canonicalScanId}
              active onStop={canonicalScanId ? () => stopScan(canonicalScanId) : undefined} />
          ) : null,
          remediate: canonicalStage?.stage === 'remediate'
            && (!remRun.snapshot?.scan_id || remRun.snapshot.scan_id === canonicalScanId) ? (
            <RemediationRunCard snapshot={remRun.snapshot} receivedAt={remRun.receivedAt}
              connected={remRun.connected} events={remRun.events}
              onOpen={view === 'remediate' ? null : () => {
                setView('remediate'); window.scrollTo({ top: 0, behavior: 'smooth' })
              }} />
          ) : null,
        }}
        onNavigate={(next) => {
          setView(next)
          window.scrollTo({ top: 0, behavior: 'smooth' })
        }} />}

      <main id="main-content" tabIndex={-1}>
      <div id="workflow-panel" role="tabpanel"
           aria-labelledby={viewTabRendered ? `workflow-tab-${view}` : undefined}
           aria-label={workflowPanelLabel}>
      <ErrorBoundary key={view}>
      {/* PRD §10 — a tab the role does not include renders an explanation instead of its body.
          Wrapping the whole panel rather than gating each `view === 'x'` branch is deliberate:
          a per-branch check is one edit away from being forgotten on the next tab somebody adds,
          and the branch that gets forgotten renders its real contents. This is presentation, not
          protection — every route behind these tabs enforces its own capability server-side. */}
      {!isVisible(access, view) ? (
        <AccessRestricted access={access} tabKey={view} tabs={TABS} onGo={goToView}
                          label={(TABS.find(([k]) => k === view) || [])[1]} />
      ) : (<>
        {/* onScan/busy/tokens are threaded so Overview can offer the scan-scope editor after a
            scan exists. Before one, `placeholder` (EmptyState → ScanSetup) is the whole screen;
            without these the editor would still be reachable exactly once per workspace. */}
        {/* The Overview grows organically across the funnel: it renders once an estate is DISCOVERED,
            not only once it is assessed, and its own sections reveal as each stage completes (the
            discovery numbers first, the assessment KPIs once Assess has run). This reverses the older
            OV-01/OV-04 gate — "Overview stays blank until assessed" — which the reveal-as-completed
            structure makes unnecessary: there is no empty-findings page to guard against any more. */}
        {/* Stale-while-revalidate: before `run` (GET /scans/{id}'s full file/finding payload)
            has ever arrived, bootstrap's cached aggregate snapshot is already enough for a real
            (if reduced) Overview — OverviewPreviewCard — rather than leaving the tab on a
            spinner for the heavier call's whole duration. Once `run` is set, this branch never
            applies again for the life of the workspace (a later scan switch already has data to
            show, via `run` from the previous scan, while the new one loads). */}
        {view === 'overview' && (run ? <Overview run={run} files={files} trend={trend} trendDates={trendDates} onGo={setView} scanList={scanList} onPickScan={switchScan} me={me} onScan={requestScan} busy={busy} hasDriveToken={hasDriveToken} hasSPToken={hasSPToken} onFileTypeChange={setFileTypeConfig} cap={cap} assessment={assessment} /> : (overviewPreview ? <OverviewPreviewCard preview={overviewPreview} /> : placeholder))}

        {view === 'integrations' && <Integrations sources={sources} files={files} scans={scanList} onScan={requestScan} busy={busy} hasDriveToken={hasDriveToken} hasSPToken={hasSPToken} onConnect={handleConnect}
          scanId={run?.id}
          openSourceKey={pendingSourceOpen} onOpenSourceHandled={() => setPendingSourceOpen(null)}
          onOpenAssess={() => { setView('assess'); window.scrollTo({ top: 0, behavior: 'smooth' }) }} />}

        {view === 'discover' && <Discover sources={sources} files={files} rawFiles={scan?.files ?? []} busy={busy} resultsOnly={priorResults.discover || isTimeTravel} onScan={guardStageAction(priorResultsRef, 'discover', requestScan)} hasDriveToken={hasDriveToken} hasSPToken={hasSPToken} delegations={delegations} onAdvance={guardStageAction(priorResultsRef, 'discover', () => { setView('assess'); window.scrollTo({ top: 0, behavior: 'smooth' }) })} progress={progress} preflightDegraded={preflightDegraded} preflightCapacityState={preflightCapacityState} scanPct={busy ? progressPct(progress) : 0} scanId={run?.id} activeScanId={liveScanId} jobId={discoverJobId} scope={run?.scope || null} run={run} scanList={scanList} runAt={inventorySnapshot({ run, inventory: run?.scope?.inventory || null })} decisions={decisions} setDecisions={setDecisions} showRunProgress={false}
          // Bootstrap already confirmed a scan exists (its cached snapshot arrived) but the full
          // getScan() payload hasn't yet — the same `run`-is-null window Overview/Assess show a
          // preview card for. Discover's own `files`/`scope` fall back to `[]`/`null` in exactly
          // this window (never `null` for "not asked yet" vs. "asked, found none"), which reads
          // to every count on this screen as a genuinely empty, just-created workspace.
          pendingScanLoad={!run && !!overviewPreview}
          /* Upload lost its top-level tab in the v2 simplification, but not its capability:
             it is a secondary action inside Discover now, which is where "get files in front
             of ACP" already lives. Dropping it outright would have removed the only way to try
             a single ad-hoc file without wiring a whole source. */
          onStop={() => stopScan(liveScanId)} me={me}
          onViewMonitor={() => { setMonitorFocusScanId(liveScanId || run?.id); setView('monitor') }}
          onViewLiveOps={() => { setView('liveops'); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
          onOpenSource={(sourceKey) => { setPendingSourceOpen(sourceKey); setView('integrations'); window.scrollTo({ top: 0, behavior: 'smooth' }) }} />}

        {view === 'assess' && (run ? (
          <>
            {/* Gate: discovery is still running — show a holding message instead of Assess content.
                `busy` is only set by doScan/reconnectScan (discovery path), never by AssessRunner,
                so this is safe to check. `run.completed_at` flips once the backend marks the scan
                done; while it is null the Assess tab has nothing deterministic to show. */}
            {busy && !run?.completed_at && (
              <div style={{ padding: '48px 24px', textAlign: 'center', color: 'var(--muted)' }}>
                <div style={{ fontSize: 22, marginBottom: 12 }}>🔍</div>
                <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 8, color: 'var(--ink)' }}>
                  Available after discovery completes
                </div>
                <div style={{ fontSize: 13.5, marginBottom: 20, maxWidth: 380, margin: '0 auto 20px' }}>
                  Discovery is still running. Assess will be ready once all documents have been catalogued.
                </div>
                <button className="ghost" onClick={() => setView('discover')}>
                  Go to Discover →
                </button>
              </div>
            )}
            {/* The assessment scope lives here now (Discover/Assess PRD §4.4): document types +
                the Core-17 WCAG picker, with a live eligible-file count, written to scan_scope as
                the single authority for the format axis. Collapsed by default so it does not
                displace the run button, and left one click away for when the scope needs changing. */}
            {/* THE PRE-RUN SCREEN (approved board 2). It replaces the two collapsed scope panels
                that used to sit here. The board's footer removes the WCAG scope-rules panel
                outright, and AssessScope's document-type and criterion pickers are rows on this
                screen now - so keeping either would be the same question asked twice, in two
                places, with two answers.

                Rendered only before a run. Afterwards the results answer a different question and
                the re-run control lives with them, inside AssessRunner - which is why `controlled`
                hides the runner's pre-run band but not its results. */}
            {/* discoveredAt takes fmtStamp, NOT the raw column. AssessSetup interpolates the value
                straight into "From discovery run {discoveredAt}" and formats nothing, so passing
                run.completed_at printed an ISO timestamp across the top of the screen. fmtStamp
                also returns null for a missing value, which is exactly the prop's "omit rather
                than invent" contract — so the `|| null` this used to carry is redundant. */}
            {!busy && assessPhase === 'idle' && !assessed && !priorResults.assess && (
              <AssessSetup scanId={run.id} discoveredAt={fmtStamp(run?.completed_at)} busy={busy}
                           approvalPolicy={run?.scope?.fix_approval_policy || null} capability={cap}
                           onSaved={(scope) => adoptScopeConfig({ scope: { name: 'Selected criteria', criteria: scope } })}
                           onRun={startAssessment} />
            )}
            {!priorResults.assess && assessPhase === 'starting' && (
              <section className="panel" role="status" aria-live="polite"
                       style={{ textAlign: 'center', padding: '52px 24px' }}>
                <div className="spinner" aria-hidden="true" style={{ margin: '0 auto 14px' }} />
                <h2 style={{ margin: '0 0 7px' }}>Preparing your assessment</h2>
                <p className="muted" style={{ margin: 0 }}>
                  Loading the new run and assigning documents to Assess workers…
                </p>
              </section>
            )}
            {!priorResults.assess && !(busy && !run?.completed_at) && (
              <AssessRunner key={run.id} files={files} runId={run.id} scanBusy={busy}
                            controlled onReady={registerAssessStart}
                            onAssessed={() => setJustAssessed(run.id)} onPhase={setAssessPhase}
                            onActivity={setAssessmentActivity}
                            onViewMonitor={() => { setMonitorFocusScanId(run.id); setView('monitor') }}
                            me={me} />
            )}
            {/* Gated on assessPhase === 'done', not just `assessed` — `assessed` flips true the
                instant Assess is clicked (before AssessRunner's own progress animation even
                starts), so the results below were popping in fully-populated while the bar
                above still pretended to be working. assessPhase tracks AssessRunner's actual
                idle/running/done state (via onPhase), so results now appear exactly when the
                animation finishes — same instant a real assessment would land. */}
            {/* ONE summary, where four panels used to lead with four counts of "problems" over
                four unstated denominators. `AccessibilityStatus` (13 needing remediation),
                `ConfidenceDashboard` (189 unresolved — which is 176 + 13 restated),
                `CoverageScorecard` (5/20, a capability fact that does not change when you run
                anything) and `RiskScore` (the score) came out. The components still exist:
                AccessibilityStatus is the per-file hero inside FileDrawer, where its denominator
                is one document and therefore unambiguous.

                RiskScore left with the score it renders. `100 − Σ severity_weight`, floored at 0,
                averaged across documents: unresolved review items and criteria with no method
                both weigh ZERO, so a document with forty findings awaiting a person scores 100
                and reads "compliant". That is the score being structurally unable to tell
                "checked and passed" from "not checked" — the one distinction this product
                exists to make. It returns when a written, versioned weighting exists that cannot
                report "good" while a critical finding is unresolved.

                `RuleBreakdown` and `Dashboard` stay: they are the by-criterion detail beneath the
                summary, not a second scoreboard above it. */}
            {/* Run details replaces the results rather than sitting under them: it answers a
                different question, and stacking it below would put a capability reference back on
                the screen whose whole problem was too many panels answering different questions
                at once. */}
            {/* ONE FILE, BY CRITERION (approved board assess-05 / A13-A18). Replaces the results
                 while it is open, exactly as RunDetails does — it carries its own Back and
                 next-document controls, which only make sense for a view that owns the screen.

                 It takes the worklist's ROW rather than the raw file, so it renders the same
                 derivation the list was ordered by; recomputing one here could disagree with the
                 list about which document needs a person first.

                 FileDrawer is not deleted — Overview still uses it. This is only about which view
                 the Assess tab opens for a document. */}
            {assessed && resultsReady && assessFile && (
              <AssessFileFindings row={assessFile} file={assessFileRaw} cap={cap} assessment={assessment}
                                  assessedAt={fmtStamp(run?.assessed_at)}
                                  onBack={() => { setAssessFile(null); window.scrollTo({ top: 0, behavior: 'smooth' }) }}
                                  onNext={assessFileNext ? () => { setAssessFile(assessFileNext); window.scrollTo({ top: 0, behavior: 'smooth' }) } : undefined}
                                  nextName={assessFileNext?.name}
                                  onRemediate={priorResults.assess ? undefined : guardStageAction(priorResultsRef, 'assess', () => { setView('remediate'); window.scrollTo({ top: 0, behavior: 'smooth' }) })} />
            )}
            {assessed && resultsReady && runDetails && (
              <RunDetails scanId={run.id} files={files} cap={cap} assessment={assessment}
                          onBack={() => { setRunDetails(false); window.scrollTo({ top: 0, behavior: 'smooth' }) }} />
            )}
            {assessed && resultsReady && !runDetails && !assessFile && <><AssessSummary files={files} cap={cap} assessment={assessment} assessedAt={fmtStamp(run?.assessed_at)} run={run} notStarted={run?.not_assessed?.count} integrityCaveat={integrityCaveat(runVerdict)} onRemediate={priorResults.assess ? undefined : guardStageAction(priorResultsRef, 'assess', () => { setView('remediate'); window.scrollTo({ top: 0, behavior: 'smooth' }) })} onRunDetails={() => { setRunDetails(true); window.scrollTo({ top: 0, behavior: 'smooth' }) }} onChangeScope={priorResults.assess ? undefined : guardStageAction(priorResultsRef, 'assess', () => { setView('discover'); window.scrollTo({ top: 0, behavior: 'smooth' }) })} /><AssessRunIntegrity key={run.id} verdict={runVerdict} manifest={runManifest.manifest} /><AssessWorklist files={files} cap={cap} assessment={assessment} onOpenFile={(row) => setAssessFile(row)} onBulkFix={priorResults.assess ? undefined : (rows) => handleBulkFix(run.id, rows)} /><RuleBreakdown scanId={run.id} files={files} /></>}
          </>
        ) : (overviewPreview ? <AssessPreviewCard preview={overviewPreview} /> : placeholder))}

        {view === 'remediate' && (run ? <Remediate run={run} files={files} decisions={decisions} setDecisions={setDecisions} triage={triage} setTriage={setTriage} assignees={assignees} setAssignees={setAssignees} myEmail={me?.email} aiEnabled={aiEnabled} resultsOnly={priorResults.remediate} readOnly={isTimeTravel} onRefresh={() => getScan(run.id, run?.revision).then((r) => { if (r !== NOT_MODIFIED) setScan(r) }).catch(() => {})} onHitlCount={setHitlCount} runStream={remRun} remediationStage={remediationStage} progressHostId={remediationProgressHostId} cap={cap} assessment={assessment} assessedAt={fmtStamp(run?.assessed_at)} onNavigate={(v) => { setView(v); window.scrollTo({ top: 0, behavior: 'smooth' }) }} delivery={deliveryAccess(access, isTimeTravel).visible ? <Publish embedded run={run} files={files} cap={cap} assessment={assessment} remediationSnapshot={remRun?.snapshot} certified={certifiedDocs} readOnly={deliveryAccess(access, isTimeTravel || priorResults.remediate).readOnly} triage={triage} onPublish={(file) => { setPublishedFiles((s) => [...s, file]); schedulePublishRefetch() }} me={me} onOpenDetails={() => setView('publish')} /> : null} /> : placeholder)}

        {view === 'publish' && (run ? <Publish run={run} files={files} cap={cap} assessment={assessment} remediationSnapshot={remRun?.snapshot} certified={certifiedDocs} readOnly={isTimeTravel} triage={triage} onPublish={(file) => { setPublishedFiles((s) => [...s, file]); schedulePublishRefetch() }} me={me} /> : placeholder)}

        {/* QueuePanel needs no assessment data at all — it's job-queue/worker/Azure-capacity
            status, not a compliance view — so it renders even behind the assessGate below.
            Found live 2026-08-30: Discover's queued-scan card links "View in Monitor →" for
            exactly the moment a user wants to check queue/worker state, and every click landed
            on a bare "Run the assessment to see results" screen instead — Monitor's WHOLE
            content, QueuePanel included, was nested inside `assessed ?`, gating information
            that has nothing to do with assessment behind a requirement Discovery-stage users
            can't have met yet. QueuePanel is deliberately still not rendered once `<Monitor>`
            itself mounts (assessed) — Monitor already includes it once, at line ~535 of
            Monitor.jsx; rendering it here too on top of `<Monitor>` would just show two. */}
        {view === 'monitor' && (run ? (assessed ? <Monitor me={me} run={run} scanList={scanList} sources={sources} files={files} ratified={ratified} decisions={decisions} publishedFiles={publishedFiles} readOnly={isTimeTravel} aiEnabled={aiEnabled} onAiToggle={setAiEnabled} busy={busy} progress={progress} scanPct={busy ? progressPct(progress) : 0} focusScanId={monitorFocusScanId} onClearFocus={() => setMonitorFocusScanId(null)} trend={trend} trendDates={trendDates} /> : <><QueuePanel focusScanId={monitorFocusScanId} onClearFocus={() => setMonitorFocusScanId(null)} />{assessGate}</>) : (overviewPreview ? <MonitorPreviewCard preview={overviewPreview} /> : placeholder))}


        {/* Standalone Knowledge Graph — was nested inside Assess (findable only after
            scrolling past the score/dashboard); now its own tab so it's directly
            reachable for open-ended exploration, same as Upload. Still needs an
            assessed scan (the graph visualizes WCAG findings), so it shares Monitor's
            gate: assessGate when a scan exists but hasn't been assessed yet. */}
        {view === 'graph' && (run ? (assessed ? <Suspense fallback={<Loading />}><KnowledgeGraph files={files} scanId={run.id} readOnly={isTimeTravel} /></Suspense> : assessGate) : placeholder)}

        {/* Visible to every signed-in user under the temporary open-tab policy. The analytics
            API remains the authority for the underlying estate-wide data. */}
        {view === 'analytics' && <AdminInsights me={me} run={run} files={files} cap={cap} assessment={assessment} scanList={scanList} onPickScan={switchScan} />}

        {/* Live Azure traffic is read-only and payload-sanitized. Its API and SSE endpoints still
            require an authenticated user, and the stream starts only when this tab is opened. */}
        {view === 'liveops' &&
          <Suspense fallback={<Loading />}><AdminLiveTraffic me={me} currentScanId={run?.id || null}
            onNavigateRecovery={(stage) => setView({ assess: 'assess', remediate: 'remediate',
              release: 'publish' }[stage] || 'overview')} /></Suspense>}

        {/* ACP's own Accessibility Conformance Report (ADR 0047). No `run` gate: it is not about a
            scan, and requiring one would make the tab unreachable on a fresh deploy. Every write
            behind it is role-gated server-side (acr_authz) — a read-only visitor sees the report
            and cannot change it, which is the same shape the backend enforces. */}
        {view === 'acr' && <AcrWorkspace readOnly={!canOperate(access, 'acr')} />}

        {/* Guided workflow: a "next step" CTA on each workflow tab once a scan exists.
            'discover' is excluded — it owns a sub-step CTA (Inventory → Classify → Actions → Assess). */}
        {run && !priorResults[view === 'publish' ? 'release' : view] && ['assess', 'remediate', 'publish'].includes(view) && (() => {
          const flow = ['integrations', 'discover', 'assess', 'remediate', 'publish', 'monitor']
          const label = { discover: '1 · Discover — classify the estate', assess: '2 · Assess — score vs WCAG',
                          remediate: '3 · Remediate — fix the issues', publish: '4 · Publish — certify what passes',
                          monitor: '5 · Monitor — keep it compliant' }
          // PRD §10 — "Workflow calls to action must respect destination access." A hidden
          // destination is skipped entirely rather than offered and refused on arrival, so
          // "Continue to Remediate" cannot appear for someone who has no Remediate.
          let nxt = null
          for (let j = flow.indexOf(view) + 1; j < flow.length; j++) {
            if ((!me.allow || me.allow.includes(flow[j])) && isVisible(access, flow[j])) {
              nxt = flow[j]; break
            }
          }
          // Same "is this tab's task done" signal the tab stepper uses — the CTA can't
          // advance until the current tab's own work is actually finished.
          const taskDone = {
            integrations: !!run,
            assess: assessed,
            remediate: files.some((f) => f.remediated_at || f.drive_write_url),
            publish: (publishedFiles?.length || 0) > 0,
          }[view]
          return nxt ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 12,
                          margin: '20px 0 4px', paddingTop: 14, borderTop: '1px solid var(--line)' }}>
              <span className="muted" style={{ fontSize: 13 }}>
                {taskDone ? 'Done here? Continue →' : 'Finish this step to continue →'}
              </span>
              {/* §10 again: "If a destination is view-only, wording changes from Start to View."
                  The label promising work the user cannot do there is worse than no label — the
                  button works, and it was wrong about what it does. */}
              <button disabled={!taskDone}
                      title={taskDone ? undefined : "Complete this tab's task before moving on"}
                      onClick={() => { goToView(nxt); window.scrollTo({ top: 0, behavior: 'smooth' }) }}>
                {canOperate(access, nxt) ? label[nxt] : `View ${label[nxt].split(' — ')[0]}`} →
              </button>
            </div>
          ) : null
        })()}
      </>)}
      </ErrorBoundary>
      </div>
      </main>

      <ChatWidget files={files} run={run} trend={trend} trendDates={trendDates} me={me} />
      {SHOW_A11Y && <A11ySelfCheck />}
      {/* onOntologyChange / onPrivilegeChange are gone with the Business ontology and Permissions
          panels. The ontology DATA path below is untouched — App still annotates the corpus from
          whatever was last published; only its editor left Settings. */}
      {myDataOpen && <MyDataDialog onClose={() => setMyDataOpen(false)} />}
      {settingsOpen && canOpenSettings(me, access) && <Settings files={files} onClose={() => {
        setSettingsOpen(false)
        getMyScope().then((value) => setUserTimezone(value?.release_timezone || 'America/Chicago')).catch(() => {})
      }} onRubricSaved={() => getRubric().then(setRubric)} onDelegationChange={setDelegations} onFileTypeChange={(cfg) => setFileTypeConfig(cfg)} me={me} />}

      {/* The universal scan gate. Opened by `requestScan` from every entry point; the wizard's
          "Start scan" confirm is the only thing that dispatches `doScan`. The behavior toggles are
          bound to the App-level state so a choice here carries into the scan that follows. */}
      {pendingScan && (
        <ScanReviewModal
          source={pendingScan.source} folder={pendingScan.folder}
          startInFolderMode={pendingScan.folderFirst}
          startInAllMode={pendingScan.allFolders}
          deepScan={deepScan} setDeepScan={setDeepScan}
          queuedScan={queuedScan} setQueuedScan={setQueuedScan}
          excludeRemediated={excludeRemediated} setExcludeRemediated={setExcludeRemediated}
          incremental={incremental} setIncremental={setIncremental}
          estCount={sources.filter((s) => (pendingScan.source === 'sharepoint'
            ? (s.type === 'onedrive' || s.type === 'sharepoint')
            : (pendingScan.source === 'all' || s.type === 'google_drive')))
            .reduce((a, s) => a + (s.files || 0), 0)}
          hasDrive={hasDriveToken} hasSP={hasSPToken} canEditScope={scopeOwner !== false}
          scans={scanList}
          onConfirm={(runScope) => {
            const { source, folder, folders: preset } = pendingScan
            setPendingScan(null)
            // The wizard's own answer WINS when it has one: an operator who went on to pick
            // specific folders gave a tighter boundary than the sites they started from, and
            // overriding it with the preset would scan more than they asked for. `folders: []`
            // is the wizard's "entire connected source" and is not an answer about sites, so the
            // preset stands there — that is the ordinary multi-site path.
            const chose = Array.isArray(runScope?.folders) && runScope.folders.length > 0
            const rs = (preset && preset.length && !chose)
              ? { ...(runScope || {}), folders: preset }
              : runScope
            doScan(source, folder, rs)
          }}
          onCancel={() => setPendingScan(null)} />
      )}
      <ConfirmDialog />
      {view !== 'liveops' && <Suspense fallback={null}>
        <LiveOperationsNotifier onOpen={() => { setView('liveops'); window.scrollTo({ top: 0, behavior: 'smooth' }) }} />
      </Suspense>}
      <VersionToast currentVersion={platformVersion} />
      <RealtimeShadowPanel enabled={realtimeShadowEnabled} currentSnapshot={{
        scan_id: run?.id ?? null,
        scan_status: run?.status ?? null,
        progress_phase: progress?.phase ?? null,
        active_workflows: activeWorkflows.length,
      }} />
    </div>
    </>
  )
}
