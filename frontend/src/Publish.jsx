import { releaseBatchDomain, releaseBatchProgress } from './releaseBatchProgress.js'
import AutomaticPublicationStatus from './AutomaticPublicationStatus.jsx'
import { authEpoch } from './apiIdentity.js'
import AutomaticReleasePackage from './AutomaticReleasePackage.jsx'
import { useState, useEffect, useRef } from 'react'
import ScopeBanner from './ScopeBanner.jsx'
import DriveReleaseReconnect from './DriveReleaseReconnect.jsx'
import ReleaseQuickActions from './ReleaseQuickActions.jsx'
import ReleaseCompletionDocuments from './ReleaseCompletionDocuments.jsx'
import ReleaseOutcomeSummary from './ReleaseOutcomeSummary.jsx'
import { PROGRESS_STATES } from './RemediationProgressSummary.jsx'
import ProgressQueueDrawer from './ProgressQueueDrawer.jsx'
import { releaseProgressState } from './remediationLiveDocumentState.js'
import ReleaseCopyDestination from './ReleaseCopyDestination.jsx'
import ReleaseReports from './ReleaseReports.jsx'
import ReleaseCorrectionNotice from './ReleaseCorrectionNotice.jsx'
import ReleaseDeliveryCard from './ReleaseDeliveryCard.jsx'
import { documentSelection, documentScopeSentence, documentsInSelection } from './remediableScope.js'
import { openReport, publishFile, publishAllFiles, getReleaseStatus, getAutomaticRelease, resumeAutomaticRelease, getReleaseManifest, previewReleaseDestination, previewReleasePackage, listHitlQueue, getSettings, getSourceStatus, rescoreFile, downloadReleasePackage, prepareReleasePackage, downloadPreparedReleasePackage, getQueueJob, putMyReleaseTemplates } from './api.js'
import { releaseDestinationPhrase, releaseConfirmLines } from './releasePolicy.js'
import { SET_STATUS, releaseSetStatus } from './graduation.js'
import { mirrorState, MIRROR } from './deliveryPolicy.js'
import ReleaseHistory from './ReleaseHistory.jsx'
import ReleaseModelProvenance from './ReleaseModelProvenance.jsx'
import ReleaseFileSelection, { releaseFileSize } from './ReleaseFileSelection.jsx'
import ReleasePlanSummary, { formatReleaseBytes } from './ReleasePlanSummary.jsx'
import ReleaseStepPanel from './ReleaseStepPanel.jsx'
import LiveCounter from './LiveCounter.jsx'
import ReleaseDestinationPicker from './ReleaseDestinationPicker.jsx'
import ReleaseTemplates from './ReleaseTemplates.jsx'
import './release-plan-summary.css'
import './release-clarity.css'
import { hasCorrectedCopy, hasSavedCorrectedCopy, deliveryIsCurrent, releaseReadiness, canSelectRelease, releaseSourceState } from './releaseClarityModel.js'
import ReleaseRecoveryActions from './ReleaseRecoveryActions.jsx'
import { releaseErrorCopy } from './releaseErrorCopy.js'

// Step 9 · Publish. Marks re-validated documents as published: the conformance status
// is recorded in the audit trail and the fixed copy (already in Blob + the Drive
// mirror) becomes the document of record. Source replace-in-place and owner
// notification are roadmap — the UI must not claim them until they're real.
// publish() persists via POST /scans/{sid}/publish.
// readOnly: time-travel replay — publishing must act on the live estate, not a snapshot.
export default function Publish({ run, files = [], certified = [], readOnly = false, onPublish, me,
  triage = {}, cap, assessment, remediationSnapshot, embedded = false, onOpenDetails }) {
  // Release operates on the exact document cohort chosen in Remediate. The banner below explains
  // the restriction; this filter enforces it for selection, delivery, packaging and set status.
  const releaseFiles = documentsInSelection(files, triage)
  const [automaticAuthorization, setAutomaticAuthorization] = useState(null)
  const automaticBinding = useRef(null)
  const [automaticStatusPending, setAutomaticStatusPending] = useState(true)
  const [automaticStatusError, setAutomaticStatusError] = useState('')
  const [automaticStatusRefresh, setAutomaticStatusRefresh] = useState(0)
  const [outcomeFilter, setOutcomeFilter] = useState('all')
  const [progressQueue, setProgressQueue] = useState(null)
  const [releaseTab, setReleaseTab] = useState('manage')
  useEffect(() => setReleaseTab('manage'), [run?.id])
  useEffect(() => { setOutcomeFilter('all'); setProgressQueue(null) }, [run?.id])
  const [allowRemainingIssues, setAllowRemainingIssues] = useState(false)
  const releaseScopeKey = JSON.stringify([run?.id, [...new Set(releaseFiles.map(file => file.file))].sort()])
  const partialChoice = useRef(null)
  const ready = releaseFiles.filter(file => allowRemainingIssues ? hasSavedCorrectedCopy(file) : hasCorrectedCopy(file))
  const [sessionDone, setDone] = useState({})
  const [pubUrls, setPubUrls] = useState({})   // file -> published Drive URL, from POST /publish
  const [releaseFolder, setReleaseFolder] = useState(null)
  const [releaseId, setReleaseId] = useState(null)
  const [releaseFolders, setReleaseFolders] = useState([])
  const [releaseResults, setReleaseResults] = useState({})
  // Server-authored publication currency (GET release `publication`): a correction saved after
  // publication makes the delivered copy and reports out of date by exact artifact identity.
  const [publication, setPublication] = useState(null)
  const [republishing, setRepublishing] = useState(false)
  const [reportsRefresh, setReportsRefresh] = useState(0)
  const done = Object.fromEntries(releaseFiles.filter((file) => deliveryIsCurrent(file, releaseResults[file.file], sessionDone)).map((file) => [file.file, true]))
  const [releaseAnnouncement, setReleaseAnnouncement] = useState('')
  const [releaseError, setReleaseError] = useState(null)
  const [manifestError, setManifestError] = useState('')
  const [publishing, setPublishing] = useState(false)
  const publishLock = useRef(false)
  const automaticAdmission = useRef({pending:true,error:true,covered:[]})
  const [downloading, setDownloading] = useState(false)
  const [builderStep, setBuilderStep] = useState(1)
  const [deliveryMethod, setDeliveryMethod] = useState('publish')
  const [packageName, setPackageName] = useState('')
  const [releaseFolderName, setReleaseFolderName] = useState('')
  const [releaseDestination, setReleaseDestination] = useState(null)
  const [destinationLocked, setDestinationLocked] = useState(false)
  const [destinationPending, setDestinationPending] = useState(true)
  const [settingsPending, setSettingsPending] = useState(true)
  const frozenDestination = useRef(undefined)
  const currentRunId = useRef(run?.id)
  currentRunId.current = run?.id
  const releaseOwner = me?.email || run?.owner_email || ''
  const releaseContext = useRef(null)
  const contextKey = JSON.stringify([run?.id, releaseOwner])
  if (releaseContext.current?.key !== contextKey) releaseContext.current = { key: contextKey, live: true }
  useEffect(() => {
    const context = releaseContext.current
    context.live = true
    return () => { context.live = false; context.cancelWait?.() }
  }, [contextKey])
  const ownsRelease = context => context.live && releaseContext.current === context
  const [preserveHierarchy, setPreserveHierarchy] = useState(true)
  const [includeManifest, setIncludeManifest] = useState(true)
  const [includeVerificationReport, setIncludeVerificationReport] = useState(false)
  const [downloadFormat, setDownloadFormat] = useState('zip')
  const [releaseTemplates, setReleaseTemplates] = useState([])
  const [templateSaving, setTemplateSaving] = useState(false)
  const [packagePreview, setPackagePreview] = useState(null)
  const [packageJob, setPackageJob] = useState(null)
  const [keptInAcp, setKeptInAcp] = useState(false)
  const [releasePreview, setReleasePreview] = useState(null)
  const [reviewedPlanKey, setReviewedPlanKey] = useState(null)
  const [previewingRelease, setPreviewingRelease] = useState(false)
  const [selectedFiles, setSelectedFiles] = useState(() => new Set())
  const builderRef = useRef(null)
  const selectionInitialized = useRef(false)
  const confirmDialogRef = useRef(null)
  const confirmCancelRef = useRef(null)
  const releaseHadPendingRef = useRef(false)
  const continuationReported = useRef(new Set())
  const [completionSound, setCompletionSound] = useState(() => {
    try { return window.localStorage.getItem('acp.release.completionSound') === 'on' } catch { return false }
  })
  const [sel, setSel] = useState(null)
  useEffect(() => {
    publishLock.current = false; setPublishing(false)
    continuationReported.current = new Set()
    frozenDestination.current = undefined
    setReleaseDestination(null); setDestinationLocked(false); setDestinationPending(true)
    setAllowRemainingIssues(false); setDone({}); setReleaseResults({}); setPubUrls({}); setReleaseId(null)
    setPublication(null); setRepublishing(false)
    setReleaseFolder(null); setReleaseFolders([]); setReleasePreview(null); setPackagePreview(null)
    setSelectedFiles(new Set()); selectionInitialized.current = false
    setConfirm(null); setSel(null); setBuilderStep(1); setReleaseAnnouncement(''); setReleaseError(null)
  }, [run?.id, releaseOwner])
  useEffect(() => {
    let live = true, controller, timer, deadline, cancel, failures = 0
    const epoch = authEpoch()
    const binding = JSON.stringify([releaseScopeKey, releaseOwner, epoch, readOnly])
    if (automaticBinding.current !== binding) {
      automaticBinding.current = binding
      partialChoice.current = null
      setAutomaticAuthorization(null)
      setAllowRemainingIssues(false)
    }
    setAutomaticStatusPending(true)
    setAutomaticStatusError('')
    if (!run?.id || !releaseFiles.length || readOnly) { setAutomaticStatusPending(false); return }
    const current = () => live && authEpoch() === epoch
    const refresh = async () => {
      if (!current()) return
      controller = new AbortController() // A timed-out attempt cannot poison later GETs.
      try {
        const result = await Promise.race([
          getAutomaticRelease(run.id, releaseFiles.map(file => file.file), { signal: controller.signal }),
          new Promise((_, reject) => { cancel = () => reject(new Error('Cancelled')); deadline = setTimeout(() => { controller.abort(); reject(new Error('Automatic publication status timed out.')) }, 20000) }),
        ])
        if (!current()) return
        failures = 0
        const saved = result?.authorization
        setAutomaticAuthorization(saved ? { ...saved, observedScope: releaseScopeKey, observedOwner: releaseOwner, observedRunId: result.run_id, observedEpoch: epoch } : null)
        setAutomaticStatusPending(false)
        setAutomaticStatusError('')
        if (partialChoice.current !== releaseScopeKey && saved?.allow_remaining_issues === true
          && ['active', 'waiting', 'processing', 'publishing', 'blocked', 'completed'].includes(saved.status)
          && releaseFiles.every(file => saved.files?.includes(file.file))) setAllowRemainingIssues(true)
        if (saved && (['active','waiting','processing','publishing','blocked'].includes(saved.status)
          || (saved.package && !['done','dead','cancelled'].includes(saved.package.status)))) timer = window.setTimeout(refresh, 5000)
      } catch (error) {
        if (current()) {
          failures += 1
          const retry = ![401, 403].includes(error?.status) && failures < 5
          setAutomaticStatusPending(false)
          setAutomaticStatusError(retry ? 'Automatic publication status is temporarily unavailable. ACP will check again automatically.' : 'Automatic publication status could not be confirmed. Refresh status before publishing again.')
          if (retry) timer = window.setTimeout(refresh, [5000, 10000, 20000, 30000][failures-1])
        }
      } finally { clearTimeout(deadline); cancel = null }
    }
    refresh()
    return () => { live = false; window.clearTimeout(timer); clearTimeout(deadline); controller?.abort(); cancel?.() }
  }, [releaseScopeKey, releaseOwner, readOnly, automaticStatusRefresh])
  useEffect(() => {
    if (!run?.id) { setPackageJob(null); return }
    let stored = null
    try { stored = JSON.parse(window.localStorage.getItem(`acp.release.package.${run.id}`) || 'null') } catch {}
    setPackageJob(stored)
  }, [run?.id])
  useEffect(() => {
    if (!packageJob?.job_id || !run?.id || ['done', 'dead', 'cancelled'].includes(packageJob.status)) return
    let live = true, timer
    const refresh = async () => {
      try {
        const status = await getQueueJob(packageJob.job_id)
        if (!live) return
        const next = { ...packageJob, ...status }
        setPackageJob(next)
        window.localStorage.setItem(`acp.release.package.${run.id}`, JSON.stringify(next))
        if (!['done', 'dead', 'cancelled'].includes(status.status)) timer = window.setTimeout(refresh, 2000)
        else if (status.status === 'done') setReleaseAnnouncement('Your ZIP package is ready to download.')
      } catch (error) {
        if (live) {
          setReleaseError({ summary: 'Package progress could not be refreshed.', details: error?.message || 'ACP will try again.' })
          timer = window.setTimeout(refresh, 5000)
        }
      }
    }
    refresh()
    return () => { live = false; if (timer) window.clearTimeout(timer) }
  }, [packageJob?.job_id, packageJob?.status, run?.id])
  // Why is the publish queue empty? A remediated file only becomes certifiable once its
  // human-review findings are approved. Fetch the pending HITL queue so the empty state can
  // say "N findings await review — approve them in Review first" instead of a dead-end.
  const [pendingReview, setPendingReview] = useState({ items: 0, files: 0, byFile: {} })
  const [processingReview, setProcessingReview] = useState({})
  useEffect(() => {
    let live = true, timer
    if (!run?.id) { setPendingReview({ items: 0, files: 0, byFile: {} }); setProcessingReview({}); return }
    const refresh = async () => {
      try {
        const q = await listHitlQueue(run.id)
        if (!live) return
        const scoped = (q || []).filter(item => releaseFiles.some(file => file.file === item.file))
        const pending = scoped.filter(item => item.rule_id !== 'auto/verify' && (!item.status || item.status === 'pending'))
        const byFile = pending.reduce((counts, item) => ({ ...counts, [item.file]: (counts[item.file] || 0) + 1 }), {})
        const applying = scoped.filter(item => item.status === 'approved' && !item.resolution && !item.applied && !item.apply_outcome)
        setPendingReview({ items: pending.length, files: Object.keys(byFile).length, byFile })
        setProcessingReview(applying.reduce((counts, item) => ({ ...counts, [item.file]: (counts[item.file] || 0) + 1 }), {}))
        if (applying.length) timer = window.setTimeout(refresh, 5000)
      } catch { /* Keep the last confirmed state until the next refresh. */ }
    }
    refresh()
    return () => { live = false; window.clearTimeout(timer) }
  }, [releaseScopeKey, JSON.stringify(releaseFiles.map(file => [file.file, file.remediated_at, file.corrected_sha256]))])
  // The REAL release policy, read from the platform settings, so the release summary describes where
  // a copy actually lands instead of a hard-coded guess. Best-effort — if it can't be read we fall
  // back to the always-true half (a durable Blob copy) rather than assert a Drive folder we're
  // unsure of.
  const [settings, setSettings] = useState(null)
  useEffect(() => {
    let live = true
    setSettingsPending(true)
    getSettings().then((s) => {
      if (!live || !s) return
      setSettings(s)
      setReleaseTemplates(Array.isArray(s.release_templates) ? s.release_templates : [])
      const preference = (run?.source === 'local' || s.release_destination?.provider === run?.source) ? s.release_destination || null : null
      setReleaseDestination((current) => frozenDestination.current !== undefined ? frozenDestination.current : current && (run?.source === 'local' || current.provider === run?.source) ? current : preference)
    }).catch(() => {}).finally(() => { if (live) setSettingsPending(false) })
    return () => { live = false }
  }, [run?.id, run?.source, releaseOwner])
  const ms = mirrorState(settings)
  const driveMirrorEnabled = ms === MIRROR.ON
  const driveMirrorFolder = settings?.drive_mirror_folder?.trim() || 'Remediated'
  const releaseProvider = releaseDestination?.provider || automaticAuthorization?.destination?.provider || run?.source
  const currentAutomaticAuthorization = automaticAuthorization?.observedScope === releaseScopeKey
    && automaticAuthorization?.observedOwner === releaseOwner && automaticAuthorization?.observedEpoch === authEpoch()
    && automaticAuthorization?.run_id === automaticAuthorization?.observedRunId ? automaticAuthorization : null
  const automaticCoveredFiles = automaticAuthorization?.observedScope === releaseScopeKey
    && automaticAuthorization?.observedOwner === releaseOwner
    && automaticAuthorization?.observedEpoch === authEpoch()
    && automaticAuthorization?.run_id && automaticAuthorization.run_id === automaticAuthorization.observedRunId
    && automaticAuthorization.id
    && automaticAuthorization.destination?.provider === releaseProvider
    && (!releaseDestination || ['provider', 'folder_id', 'drive_id', 'site_id'].every(key => (releaseDestination[key] || null) === (automaticAuthorization.destination?.[key] || null)))
      ? automaticAuthorization.files || [] : []
  automaticAdmission.current = {pending:automaticStatusPending,error:!!automaticStatusError,covered:automaticCoveredFiles}
  const sourceProduct = releaseProvider === 'sharepoint' ? 'SharePoint'
    : releaseProvider === 'drive' ? 'Google Drive' : run?.sourceName || 'connected source'
  const anyDrive = releaseProvider === 'drive' && ready.some((f) => f.drive_file_id)
  const currentDeliveryPlan = {
    method: deliveryMethod,
    destination: releaseDestination,
    preserve_hierarchy: preserveHierarchy,
    include_manifest: includeManifest,
    include_verification_report: includeVerificationReport,
    download_format: downloadFormat,
    package_name: packageName,
    release_folder_name: releaseFolderName,
  }
  const applyDeliveryTemplate = (template) => {
    setDeliveryMethod(template.method || 'publish')
    if (!destinationLocked) setReleaseDestination(template.destination?.provider === releaseProvider ? template.destination : null)
    setPreserveHierarchy(template.preserve_hierarchy !== false)
    setIncludeManifest(template.include_manifest !== false)
    setIncludeVerificationReport(Boolean(template.include_verification_report))
    setDownloadFormat(template.download_format || 'zip')
    setPackageName(template.package_name || '')
    if (!destinationLocked) setReleaseFolderName(template.release_folder_name || '')
    setReleasePreview(null); setPackagePreview(null); setKeptInAcp(false)
    setReleaseAnnouncement(`${template.name} delivery template applied.`)
  }
  const persistDeliveryTemplates = async (next, successMessage) => {
    setTemplateSaving(true); setReleaseError(null)
    try {
      const saved = await putMyReleaseTemplates(next)
      const templates = Array.isArray(saved?.release_templates) ? saved.release_templates : next
      setReleaseTemplates(templates)
      setReleaseAnnouncement(successMessage)
    } catch (error) {
      setReleaseError({ summary: 'Delivery templates could not be saved.', details: error?.message || 'Try again.' })
    } finally { setTemplateSaving(false) }
  }
  const saveDeliveryTemplate = (template) => {
    const next = releaseTemplates.filter((item) => item.name.toLowerCase() !== template.name.toLowerCase())
    return persistDeliveryTemplates([...next, template], `${template.name} delivery template saved.`)
  }
  const deleteDeliveryTemplate = (template) => persistDeliveryTemplates(
    releaseTemplates.filter((item) => item.name !== template.name), `${template.name} delivery template deleted.`)
  // A release is confirmed before it runs: { kind: 'all' } or { kind: 'file', file }. The buttons
  // set this; the modal's confirm calls the real publish path below.
  const [confirm, setConfirm] = useState(null)
  useEffect(() => {
    if (!confirm) return
    const previousFocus = document.activeElement
    const frame = window.requestAnimationFrame(() => confirmCancelRef.current?.focus())
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); setConfirm(null); return }
      if (e.key !== 'Tab') return
      const focusable = [...(confirmDialogRef.current?.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])') || [])]
      if (!focusable.length) return
      const first = focusable[0]; const last = focusable[focusable.length - 1]
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('keydown', onKey)
      if (previousFocus?.isConnected) window.requestAnimationFrame(() => previousFocus.focus())
    }
  }, [confirm])
  // Source-staleness (Phase 3): has each file's SOURCE changed in Drive since the scan? Best-effort
  // — a scan with nothing trackable returns all-untracked, and any error leaves the map empty (no
  // badges) rather than blocking the queue. Never marks a file "unchanged" it can't actually verify.
  const [srcStatus, setSrcStatus] = useState({ byFile: {}, stale: 0 })
  const [rescanning, setRescanning] = useState({})
  const loadSourceStatus = () => {
    if (!run?.id) return setSrcStatus({ byFile: {}, stale: 0 })
    getSourceStatus(run.id)
      .then((s) => {
        const byFile = {}
        ;(s?.files || []).forEach((r) => { byFile[r.file] = r })
        setSrcStatus({ byFile, stale: s?.stale_count || 0 })
      })
      .catch(() => setSrcStatus({ byFile: {}, stale: 0 }))
  }
  useEffect(() => {
    loadSourceStatus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, ready.length])
  const srcOf = (f) => releaseSourceState(srcStatus.byFile[f.file])
  const previewBlockers = Object.fromEntries([...(releasePreview?.blockers || []), ...(packagePreview?.blockers || [])].map((item) => [item.file, item.reason]))
  const stateOf = (file) => releaseReadiness(file, { done, results: releaseResults, sourceState: srcOf, pending: pendingReview.byFile, processing: processingReview, allowRemainingIssues })
  const states = releaseFiles.map(stateOf)
  const progressDocuments = releaseFiles.map((file, index) => ({ file:file.file, progressState:releaseProgressState(states[index]) }))
  const deliveringCount = states.filter((state) => state.status === 'delivering').length
  const staleReady = ready.filter((f) => !done[f.file] && srcOf(f) === 'stale')
  const publishableReady = ready.filter((f) => stateOf(f).status === 'ready')
  const selectableReady = ready.filter((f) => canSelectRelease(stateOf(f)))
  const selectedReady = selectableReady.filter((f) => selectedFiles.has(f.file))
  const packagePlanKey = JSON.stringify({ files: selectedReady.map((file) => file.file).sort(),
    packageName: packageName.trim().replace(/\.zip$/i, ''), preserveHierarchy, includeManifest })
  const activePackageJob = packageJob?.plan_key === packagePlanKey ? packageJob : null
  const selectedPublishable = selectedReady.filter((f) => !done[f.file] && !automaticCoveredFiles.includes(f.file))
  const deliveryPlanKey = JSON.stringify({ files: selectedPublishable.map((file) => [file.file, file.remediated_at, file.corrected_sha256 || null]).sort(), destination: releaseDestination, releaseFolderName, preserveHierarchy, allowRemainingIssues })
  const previewIsCurrent = reviewedPlanKey === deliveryPlanKey
  const selectedSizes = selectedReady.map(releaseFileSize)
  const selectedEstimatedBytes = selectedSizes.length > 0 && selectedSizes.every((size) => size != null)
    ? selectedSizes.reduce((total, size) => total + size, 0) : undefined
  useEffect(() => {
    setSelectedFiles((old) => {
      const eligible = new Set(selectableReady.map((f) => f.file))
      if (!selectionInitialized.current && eligible.size) { selectionInitialized.current = true; return eligible }
      const next = new Set([...old].filter((file) => eligible.has(file)))
      return next
    })
    // Source freshness and release completion can change the eligible set.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, selectableReady.map((file) => file.file).join('\n')])
  const rescanBusy = Object.keys(rescanning).length > 0
  const rescanStale = async () => {
    const targets = staleReady.map((f) => f.file)
    if (!targets.length || rescanBusy) return
    setRescanning(Object.fromEntries(targets.map((f) => [f, true])))
    await Promise.allSettled(targets.map((f) => rescoreFile(run?.id, f)))
    // The re-scan runs on the worker; re-check shortly so the refreshed baselines clear the badges.
    // Honest: this re-checks the sources, it does not block on the job finishing.
    setTimeout(() => { loadSourceStatus(); setRescanning({}) }, 4000)
  }
  const orgLabel = me?.email
    ? me.email.split('@')[1]?.replace(/\.[^.]+$/, '') || me.name || 'your organisation'
    : me?.name || 'your organisation'
  const notifyReleaseComplete = (successful, failed) => {
    const body = `${successful} ${successful === 1 ? 'copy' : 'copies'} delivered${failed ? `; ${failed} need attention` : ''}.`
    if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      const notice = new Notification(failed ? 'Release completed with issues' : 'Release complete', { body })
      notice.onclick = () => { window.focus(); document.getElementById('workflow-tab-publish')?.click() }
    }
    if (completionSound) {
      try {
        const AudioContext = window.AudioContext || window.webkitAudioContext
        const audio = new AudioContext(); const oscillator = audio.createOscillator(); const gain = audio.createGain()
        oscillator.frequency.value = failed ? 330 : 660; gain.gain.value = 0.04
        oscillator.connect(gain); gain.connect(audio.destination); oscillator.start(); oscillator.stop(audio.currentTime + 0.14)
      } catch { /* sound is optional */ }
    }
  }
  const rememberRelease = (res, expectedFiles = []) => {
    if (res?.release_id) {
      setReleaseId(res.release_id)
      if ('parent_folder_id' in res) {
        const fixed = res.parent_folder_id ? { provider: res.source || releaseProvider, folder_id: res.parent_folder_id, folder_name: res.parent_folder_name || 'Saved destination' } : null
        frozenDestination.current = fixed
        setReleaseDestination(fixed); setDestinationLocked(true)
      }
    }
    if (res?.parent_folder_id) setReleaseDestination({
      provider: res.source || releaseProvider, folder_id: res.parent_folder_id,
      folder_name: res.parent_folder_name || 'Selected provider folder',
    })
    const roots = res?.release_folders || res?.roots || []
    const mappedRoots = roots.map((root) => ({
      id: root.folder_id || root.id, name: root.folder_name || root.name,
      url: root.folder_url || root.url, location: root.provider_location,
    })).filter((root) => root.id)
    if (mappedRoots.length) setReleaseFolders(mappedRoots)
    if (res?.release_folder_name || mappedRoots[0]) setReleaseFolder({
      id: res.release_folder_id || mappedRoots[0]?.id,
      name: res.release_folder_name || mappedRoots[0]?.name,
      url: res.release_folder_url || mappedRoots[0]?.url,
      createdAt: res.created_at || new Date().toISOString(),
    })
    const reported = res?.published || []
    // Only explicit server delivery results establish success; an empty response is unknown.
    const rows = reported.length || res?.release_id
      ? reported
      : [] // Missing per-file confirmation remains unknown, including older servers.
    setReleaseResults((old) => ({ ...old,
      ...Object.fromEntries(rows.map((row) => [row.file, row])),
    }))
    const successful = rows.filter((row) => row.status === 'published')
    if (successful.length) {
      setDone((old) => ({ ...old,
        ...Object.fromEntries(successful.map((row) => [row.file, true])),
      }))
      setPubUrls((old) => ({ ...old,
        ...Object.fromEntries(successful.filter((row) => row.published_url)
          .map((row) => [row.file, row.published_url])),
      }))
    }
    const failed = rows.filter((row) => ['failed', 'interrupted'].includes(row.status)).length
    const inFlight = rows.filter((row) => row.status === 'queued' || row.status === 'running').length
    if (inFlight) releaseHadPendingRef.current = true
    else if (rows.length && releaseHadPendingRef.current) {
      releaseHadPendingRef.current = false
      notifyReleaseComplete(successful.length, failed)
    }
    const notCurrent = successful.filter((row) => row.publication_state && row.publication_state !== 'current').length
    const current = successful.length - notCurrent
    setReleaseAnnouncement(inFlight
      ? `${inFlight} corrected ${inFlight === 1 ? 'copy is' : 'copies are'} being released.`
      : !rows.length ? 'Delivery has not been confirmed. Refresh release status before retrying.'
      : `${current} corrected ${current === 1 ? 'copy' : 'copies'} released${notCurrent ? `; ${notCurrent} published ${notCurrent === 1 ? 'copy is' : 'copies are'} out of date or unconfirmed` : ''}${failed ? `; ${failed} need attention` : ''}.`)
    return successful
  }
  const applyReleaseStatus = (status) => {
    if (status?.release_id) {
      const fixed = status.parent_folder_id ? { provider: status.source || releaseProvider, folder_id: status.parent_folder_id, folder_name: status.parent_folder_name || 'Saved destination' } : null
      frozenDestination.current = fixed
      setReleaseDestination(fixed)
      setDestinationLocked(true)
      if (status.release_folder_name) setReleaseFolderName(status.release_folder_name)
    }
    if (status && 'publication' in status) setPublication(status.publication || null)
    return rememberRelease({
    ...status, release_folders: status.roots,
    published: (status.documents || []).map((row) => ({
      file: row.file, status: row.status,
      original_relative_path: row.source_relative_path,
      released_relative_path: row.destination_relative_path,
      released_document_id: row.released_document_id,
      published_url: row.released_document_url,
      corrected_checksum: row.corrected_checksum, artifact_digest: row.artifact_digest,
      verification: row.verification, published_at: row.published_at, corrected_copy_assessment: row.corrected_copy_assessment,
      created: !!row.created_result,
      failure_category: row.failure_category, explanation: row.explanation,
      recovery_explanation: row.recovery_explanation,
      publication_state: row.publication_state, published_artifact_digest: row.published_artifact_digest,
      current_artifact_digest: row.current_artifact_digest,
    })),
  })
  }
  const followQueuedRelease = async (expectedFiles, context) => {
    const scanId = run.id
    // All queued providers use durable jobs. Follow the persisted release,
    // not the originating request: navigation, a worker restart, or a replica change cannot erase
    // progress. The normal load effect below restores the same state after a page reload.
    for (let attempt = 0; attempt < 180; attempt += 1) {
      if (!ownsRelease(context)) return null
      let status
      try { status = await getReleaseStatus(scanId) } catch (error) {
        if (ownsRelease(context)) setReleaseError({
          summary: 'Delivery is queued, but its progress could not be refreshed.',
          details: error?.message || 'The saved delivery continues in the background.',
          retryLabel: 'Refresh delivery status',
          retry: () => ownsRelease(context) && followQueuedRelease(expectedFiles, context),
        })
        return null
      }
      if (!ownsRelease(context)) return null
      applyReleaseStatus(status)
      setReleaseError(null)
      const rows = status?.documents || []
      if (expectedFiles.every(file => rows.some(row => row.file === file && ['published', 'failed', 'interrupted'].includes(row.status)))) return status
      await new Promise(resolve => {
        const timer = window.setTimeout(() => { context.cancelWait = null; resolve() }, 2000)
        context.cancelWait = () => { window.clearTimeout(timer); context.cancelWait = null; resolve() }
      })
    }
    if (ownsRelease(context)) setReleaseAnnouncement('Release is still running safely in the background. You may leave this page and return later.')
    return null
  }
  useEffect(() => {
    let live = true
    let timer = null
    if (!run?.id) return undefined
    const refresh = async () => {
      try {
        const status = await getReleaseStatus(run.id)
        if (!live) return
        setDestinationPending(false)
        if (!status?.release_id) return
        applyReleaseStatus(status)
        setReleaseError(null)
        const pending = (status.documents || []).some((row) => row.status === 'queued' || row.status === 'running')
          || (status.documents || []).some((row) => row.publication_state === 'publishing') || status.publication?.state === 'publishing'
        if (pending) timer = window.setTimeout(refresh, 2000)
      } catch (error) {
        if (!live) return
        setReleaseError({
          summary: 'Release progress could not be refreshed.',
          details: error?.message || 'ACP could not reach the release status service.',
          retry: refresh,
        })
      }
    }
    refresh()
    return () => { live = false; if (timer) window.clearTimeout(timer) }
    // Release state is durable; reload and resume polling when the selected scan changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, releaseOwner])
  // After an explicit republish, follow the durable release until publication is no longer in flight.
  const refreshReleaseStatus = async () => {
    const context = releaseContext.current
    try {
      const status = await getReleaseStatus(run.id)
      if (ownsRelease(context)) { applyReleaseStatus(status); setReleaseError(null) }
    } catch (error) {
      if (ownsRelease(context)) setReleaseError({ summary: 'Release progress could not be refreshed.',
        details: error?.message || 'ACP could not reach the release status service.', retry: refreshReleaseStatus })
    }
    if (ownsRelease(context)) setReportsRefresh((value) => value + 1)
  }
  // Settled means publication is no longer in flight, whatever the outcome. A failed republish
  // leaves the delivered receipt untouched (published, out_of_date, same digest) and reports the
  // refusal as publication.out_of_date[i].last_attempt_failure, so digest movement is NOT the test.
  // Success is DIGEST MOVEMENT to the version that was sent: a row now naming the sent digest was
  // published, even if a newer correction saved meanwhile makes it out_of_date again.
  // Returns 'current' | 'published_newer' (sent version published, a newer one exists) | 'failed'
  // (settled, sent version not delivered) | 'settled' | 'pending' | 'unknown'.
  // `items` are ONLY the admitted files: a refused file is never polled or shown as publishing.
  const afterRepublish = async (result, items) => {
    const context = releaseContext.current
    const files = items.map((item) => item.file)
    const settled = (status) => status?.publication?.state !== 'publishing' && files.every((file) => {
      const row = (status?.documents || []).find((item) => item.file === file)
      return row && !['queued', 'running'].includes(row.status) && row.publication_state !== 'publishing'
    })
    const sent = Object.fromEntries(items.map((item) => [item.file, item.current_artifact_digest || null]))
    const delivered = (row) => row?.status === 'published' && !!sent[row.file] && row.published_artifact_digest === sent[row.file]
    const outcome = (status) => {
      const rows = files.map((file) => (status?.documents || []).find((item) => item.file === file))
      if (rows.every((row) => row?.status === 'published' && row.publication_state === 'current')) return 'current'
      if (rows.some((row) => !delivered(row) && (row?.publication_state === 'out_of_date' || ['failed', 'interrupted'].includes(row?.status)))) return 'failed'
      return rows.every(delivered) ? 'published_newer' : 'settled'
    }
    setRepublishing(true)
    setReportsRefresh((value) => value + 1)
    try {
      for (let attempt = 0; attempt < 180; attempt += 1) {
        if (!ownsRelease(context)) return 'unknown'
        let status
        try { status = await getReleaseStatus(run.id) } catch (error) {
          if (ownsRelease(context)) setReleaseError({ summary: 'The updated copy was requested, but its progress could not be refreshed.',
            details: error?.message || 'Refresh release status to see whether it was published.', retryLabel: 'Refresh delivery status', retry: refreshReleaseStatus })
          return 'unknown'
        }
        if (!ownsRelease(context)) return 'unknown'
        applyReleaseStatus(status)
        setReleaseError(null)
        if (settled(status)) {
          ;(status.documents || []).filter((row) => files.includes(row.file) && (delivered(row) || (row.status === 'published' && row.publication_state === 'current')))
            .forEach((row) => onPublish?.(row.file))
          return outcome(status)
        }
        await new Promise((resolve) => {
          const timer = window.setTimeout(() => { context.cancelWait = null; resolve() }, 2000)
          context.cancelWait = () => { window.clearTimeout(timer); context.cancelWait = null; resolve() }
        })
      }
      if (ownsRelease(context)) setReleaseAnnouncement('The updated copy is still publishing safely in the background. You may leave this page and return later.')
      return 'pending'
    } finally {
      if (ownsRelease(context)) { setRepublishing(false); setReportsRefresh((value) => value + 1) }
    }
  }
  const partialReleaseOptions = (fileNames) => allowRemainingIssues ? {
    allowRemainingIssues: true,
    expectedArtifacts: Object.fromEntries(ready.filter(file => fileNames.includes(file.file)).map(file => [file.file, file.corrected_sha256])),
  } : {}
  const publishSelectedFiles = (fileNames, folderName = '') => (releaseDestination || allowRemainingIssues)
    ? publishAllFiles(run?.id, fileNames, folderName, { destination: releaseDestination, ...partialReleaseOptions(fileNames) })
    : folderName ? publishAllFiles(run?.id, fileNames, folderName) : publishAllFiles(run?.id, fileNames)
  const publish = async (file) => {
    if (readOnly || done[file] || automaticAdmission.current.pending || automaticAdmission.current.error || automaticAdmission.current.covered.includes(file)) return
    const context = releaseContext.current
    try {
      const res = allowRemainingIssues ? await publishSelectedFiles([file]) : releaseDestination
        ? await publishFile(run?.id, file, releaseDestination)
        : await publishFile(run?.id, file)
      if (!ownsRelease(context)) return
      const successful = rememberRelease(res, [file])
      if (res?.queued) {
        const status = await followQueuedRelease([file], context)
        if (!ownsRelease(context)) return
        const completed = (status?.documents || []).find((row) => row.file === file && row.status === 'published')
        if (completed) onPublish?.(file)
        return
      }
      if (successful.some((row) => row.file === file)) onPublish?.(file)
    } catch (error) {
      if (!ownsRelease(context)) return
      setReleaseAnnouncement('Release failed. The original file is unchanged; retry when the connection is available.')
      setReleaseError({ summary: 'The corrected copy could not be released.', details: error?.message || 'The release service did not complete the request.', retry: () => publish(file) })
    }
  }
  const recoverReleaseDestination = async (error, fileNames) => {
    const code = error?.detail?.code || error?.code
    const mismatch = code === 'release_destination_changed' || /authorized Release destination changed/i.test(error?.message || '')
    if (error?.status !== 409 || (!mismatch && code !== 'release_destination_not_ready')) return false
    const scanId = run?.id
    setDestinationPending(true)
    try {
      const status = await getReleaseStatus(scanId)
      if (currentRunId.current !== scanId) return true
      if (!status?.release_id && !mismatch) { setDestinationPending(false); return false }
      if (!status?.release_id) throw new Error('The saved release destination is not available yet.')
      if (!mismatch && (status.parent_folder_id || null) === (releaseDestination?.folder_id || null)) {
        setDestinationPending(false)
        return false
      }
      applyReleaseStatus(status)
      setDestinationPending(false)
      if (fileNames.every(name => {
        const file = releaseFiles.find(item => item.file === name)
        const result = status.documents?.find(item => item.file === name)
        return file && result && deliveryIsCurrent(file, result)
      })) {
        setReleaseError(null)
        setReleaseAnnouncement('The selected copies have already been published. Open the published folder below.')
        return true
      }
      setReleaseError({ summary: 'Saved release destination restored',
        details: 'This release started with a different destination. The saved folder is now shown above. Confirm it before publishing the remaining selected copies.',
        retryFiles: fileNames })
    } catch {
      if (currentRunId.current !== scanId) return true
      if (!mismatch) { setDestinationPending(false); return false }
      setReleaseError({ summary: 'Refresh the saved release destination',
        details: 'The destination changed, but its saved details could not be loaded. Refresh them before publishing; no retry has been sent.',
        retry: () => recoverReleaseDestination(error, fileNames), retryLabel: 'Refresh saved destination' })
    }
    return true
  }
  const publishAll = async (fileNames = null, preferredFolderName = '', exact = false) => {
    if (publishLock.current || publishing || readOnly || automaticAdmission.current.error || automaticAdmission.current.pending || destinationPending || (settingsPending && !destinationLocked)) return
    const operation = { scanId: run?.id, context: releaseContext.current }
    publishLock.current = operation
    setPublishing(true)
    setReleaseError(null)
    setReleaseAnnouncement('Publishing your selected copies. Please wait for confirmation.')
    const requested = fileNames ? new Set(fileNames) : null
    const pending = selectableReady.filter((f) => !done[f.file] && !automaticAdmission.current.covered.includes(f.file) && (!requested || requested.has(f.file))).map((f) => f.file)
    if (!pending.length) { publishLock.current = false; setPublishing(false); return }
    try {
      const res = exact
        ? await publishAllFiles(run?.id, pending, preferredFolderName, { destination: releaseDestination,
          ...partialReleaseOptions(pending), expectedArtifacts: Object.fromEntries(selectableReady.filter(f => pending.includes(f.file)).map(f => [f.file, f.corrected_sha256])) })
        : await publishSelectedFiles(pending, preferredFolderName)
      if (!ownsRelease(operation.context)) return
      const successful = rememberRelease(res, pending)
      if (res?.queued) {
        const status = await followQueuedRelease(pending, operation.context)
        if (!ownsRelease(operation.context)) return
        ;(status?.documents || []).filter((row) => pending.includes(row.file) && row.status === 'published')
          .forEach((row) => onPublish?.(row.file))
        return
      }
      successful.forEach((row) => onPublish?.(row.file))
    } catch (error) {
      if (!ownsRelease(operation.context)) return
      if (!await recoverReleaseDestination(error, pending)) {
        const copy = releaseErrorCopy(error, sourceProduct)
        setReleaseError({ summary: 'The selected copies could not be released.', details: copy.message,
          diagnosticCode: copy.diagnosticCode, retryLabel: 'Retry eligible copies',
          retry: copy.retryable ? () => publishAll(fileNames, preferredFolderName, exact) : null })
      }
    } finally {
      if (ownsRelease(operation.context) && publishLock.current === operation) {
        publishLock.current = false
        setPublishing(false)
      }
    }
  }
  const downloadSelected = async () => {
    if (downloading || !selectedReady.length) return
    setDownloading(true)
    try {
      if (activePackageJob?.status === 'done') {
        await downloadPreparedReleasePackage(run?.id, activePackageJob.job_id, activePackageJob.package_name || packageName)
      } else if (downloadFormat === 'zip' && ((packagePreview?.estimated_bytes || 0) >= 50 * 1024 * 1024 || selectedReady.length >= 100)) {
        const queued = await prepareReleasePackage(run?.id, selectedReady.map((file) => file.file), packageName,
          { preserveHierarchy, includeManifest })
        const next = { ...queued, package_name: packageName, plan_key: packagePlanKey }
        setPackageJob(next)
        window.localStorage.setItem(`acp.release.package.${run.id}`, JSON.stringify(next))
        setReleaseAnnouncement('Package preparation started. You can leave this tab and return when it is ready.')
        setDownloading(false)
        return
      } else {
        await downloadReleasePackage(run?.id, selectedReady.map((file) => file.file), packageName,
          { preserveHierarchy, includeManifest, downloadFormat })
      }
      if (includeVerificationReport) await openReport(run?.id, `acp-verification-${run?.id}.pdf`)
      setReleaseAnnouncement(downloadFormat === 'original'
        ? `Corrected file ${selectedReady[0]?.file || ''} downloaded.`
        : `Package downloaded with ${selectedReady.length} corrected ${selectedReady.length === 1 ? 'file' : 'files'}${includeManifest ? ' and a release manifest' : ''}.`)
    } catch (error) {
      setReleaseAnnouncement(error?.message || 'The corrected files could not be packaged for download.')
      setReleaseError({ summary: 'The ZIP package could not be downloaded.', details: error?.message || 'ACP could not build the release package.', retry: downloadSelected })
    }
    setDownloading(false)
  }
  // W5 — set-level certification status (graduation.js). A release can go out CONDITIONALLY while
  // some in-scope documents are still held; once those held documents are remediated (each is
  // re-validated on its own remediation path — no whole-estate re-scan), the set can GRADUATE to
  // full by releasing the now-verified formerly-held documents. Derived purely from what's already
  // on screen: the certification universe, the session's released map, and externally-certified
  // files. `files` refreshes after a remediation (App refetches on acp:file-remediated), so this
  // recomputes and the graduation offer appears without a reload.
  const setStatus = releaseSetStatus(releaseFiles.map((file) => ({ ...file, published_at: null, compliant: hasCorrectedCopy(file) })), done, [])
  // Retired direct-publish shortcut: retained for reversibility. The controls below now use
  // the standard delivery preview and confirmation before any external copy is written.
  const graduate = async () => {
    if (publishing || setStatus.status !== SET_STATUS.GRADUATABLE || !setStatus.graduatable.length) return
    setPublishing(true)
    const context = releaseContext.current
    const targets = setStatus.graduatable
    try {
      const res = await publishSelectedFiles(targets)
      if (!ownsRelease(context)) return
      const successful = rememberRelease(res, targets)
      if (res?.queued) {
        const status = await followQueuedRelease(targets, context)
        if (!ownsRelease(context)) return
        ;(status?.documents || []).filter((row) => row.status === 'published')
          .forEach((row) => onPublish?.(row.file))
        setPublishing(false)
        return
      }
      successful.forEach((row) => onPublish?.(row.file))
    } catch { /* best-effort — local state still updates */ }
    if (ownsRelease(context)) setPublishing(false)
  }
  const publishedCount = Object.keys(done).length
  const pubStarted = Object.keys(done).length > 0   // zero the outcome cards until the user releases
  const reportDate = new Date(run?.completed_at || Date.now()).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })
  // Real publish history: files carry their own published_at once the scan is re-fetched
  // (persisted server-side by POST /scans/{sid}/publish) -- this replaces a client-derived
  // list that showed the SAME date for every entry and reset on reload. Falls back to
  // "just now" for a file published THIS session, before the next refetch catches up.
  const publishedAtByFile = {}
  files.forEach((f) => { if (f.published_at) publishedAtByFile[f.file] = f.published_at })
  const publishedEntries = Object.keys(done).map((file) => ({ file,
    publishedAt: releaseResults[file]?.published_at || publishedAtByFile[file] || null,
  })).sort((a, b) => (b.publishedAt || '').localeCompare(a.publishedAt || ''))
  const fmtPublished = (e) => e.publishedAt
    ? new Date(e.publishedAt).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : 'Delivery time unavailable'
  const publishedList = publishedEntries.map((e) => e.file)
  const sourcePath = (f) => f.source_relative_path || f.parent_folder || f.file
  const failedCount = releaseFiles.filter((file) => releaseResults[file.file]?.status === 'failed').length
  const outOfDateCount = releaseFiles.filter((file) => releaseResults[file.file]?.publication_state === 'out_of_date').length
  const unconfirmedCount = releaseFiles.filter((file) => releaseResults[file.file]?.publication_state === 'identity_unknown').length
  const publicationKey = JSON.stringify([publication?.state || null, (publication?.out_of_date || []).map((item) => [item.file, item.published_artifact_digest, item.current_artifact_digest])])
  const reportsRefreshKey = `${reportsRefresh}:${publicationKey}`
  const correctionNotice = <ReleaseCorrectionNotice scanId={run?.id} publication={publication} readOnly={readOnly}
    busy={republishing || publishing} onRepublished={afterRepublish} onRefresh={refreshReleaseStatus} />
  const failedReady = ready.filter((f) => !done[f.file] && releaseResults[f.file]?.status === 'failed' && canSelectRelease(stateOf(f)))
  const downloadReleaseManifest = async () => {
    setManifestError('')
    try {
      const payload = await getReleaseManifest(run.id)
      const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url; link.download = `acp-release-${run?.id || 'manifest'}.json`; link.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setManifestError(e?.message || 'The release manifest could not be downloaded.')
    }
  }
  const startRelease = () => {
    setBuilderStep(1)
    window.requestAnimationFrame(() => {
      if (builderRef.current?.closest('details')) builderRef.current.closest('details').open = true
      builderRef.current?.scrollIntoView({ behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
      builderRef.current?.focus({ preventScroll: true })
    })
  }
  const chooseDelivery = () => {
    if (!selectedPublishable.length) setDeliveryMethod('download')
    setKeptInAcp(false)
    setBuilderStep(2)
  }
  const validateDeliveryName = (value, label) => {
    const name = value.trim().replace(label === 'ZIP filename' ? /\.zip$/i : /$^/, '').trim()
    if (!name) return ''
    if (name.endsWith('.')) return `${label} cannot end with a period.`
    if (name.length > 100) return `${label} must be 100 characters or fewer.`
    if (/[<>:"/\\|?*\u0000-\u001f\u007f]/.test(name)) return `${label} contains a character that cannot be used in a file or folder name.`
    return ''
  }
  const deliveryNameError = deliveryMethod === 'download' && downloadFormat === 'zip'
    ? validateDeliveryName(packageName, 'ZIP filename')
    : deliveryMethod === 'publish' && !releaseFolder ? validateDeliveryName(releaseFolderName, 'Release folder name') : ''
  const reviewDelivery = async () => {
    setReleasePreview(null); setPackagePreview(null); setReviewedPlanKey(null)
    if (deliveryMethod === 'download') {
      setPreviewingRelease(true)
      try {
        const preview = await previewReleasePackage(run?.id, selectedReady.map((file) => file.file),
          { preserveHierarchy, includeManifest })
        setPackagePreview(preview)
        setBuilderStep(3)
      } catch (error) {
        setReleaseError({ summary: 'The download could not be checked.', details: error?.message || 'ACP could not preview the package.', retry: reviewDelivery })
      }
      setPreviewingRelease(false)
      return
    }
    if (deliveryMethod === 'acp') { setReleasePreview(null); setBuilderStep(3); return }
    setPreviewingRelease(true)
    setReleaseAnnouncement('')
    try {
      const preview = await previewReleaseDestination(
        run?.id, selectedPublishable.map((file) => file.file),
        releaseFolder?.name || releaseFolderName, preserveHierarchy, releaseDestination, partialReleaseOptions(selectedPublishable.map(file => file.file)))
      setReleasePreview(preview)
      setReviewedPlanKey(JSON.stringify({ files: selectedPublishable.map((file) => [file.file, file.remediated_at, file.corrected_sha256 || null]).sort(), destination: releaseDestination, releaseFolderName: !releaseFolder && !releaseFolderName.trim() ? preview.folder_name || '' : releaseFolderName, preserveHierarchy, allowRemainingIssues }))
      if (!releaseFolder && !releaseFolderName.trim()) setReleaseFolderName(preview.folder_name || '')
      setBuilderStep(3)
    } catch (error) {
      setReleaseAnnouncement(error?.message || 'The release destination could not be previewed.')
      setReleaseError({ summary: 'The destination could not be checked.', details: error?.message || 'ACP could not preview the release destination.', retry: reviewDelivery })
    }
    setPreviewingRelease(false)
  }
  const reviewFailedRelease = () => {
    setSelectedFiles(new Set(failedReady.map((f) => f.file)))
    setDeliveryMethod('publish')
    setReleasePreview(null); setPackagePreview(null); setBuilderStep(2)
    window.requestAnimationFrame(() => {
      if (builderRef.current?.closest('details')) builderRef.current.closest('details').open = true
      builderRef.current?.scrollIntoView({ behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
      builderRef.current?.focus({ preventScroll: true })
    })
  }
  const keepSelectedInAcp = () => {
    setKeptInAcp(true)
    setReleaseAnnouncement(`${selectedReady.length} corrected ${selectedReady.length === 1 ? 'file remains' : 'files remain'} securely in ACP. No external copies were created and the originals were not changed.`)
  }
  const downloadQueuedPackage = async () => {
    if (!packageJob?.job_id || packageJob.status !== 'done' || downloading) return
    setDownloading(true)
    try {
      await downloadPreparedReleasePackage(run.id, packageJob.job_id, packageJob.package_name || '')
      setReleaseAnnouncement('Prepared ZIP package downloaded.')
    } catch (error) {
      setReleaseError({ summary: 'The prepared ZIP could not be downloaded.', details: error?.message || 'Try preparing it again.' })
    } finally { setDownloading(false) }
  }

  const changeRemainingIssues = value => { partialChoice.current = releaseScopeKey; setAllowRemainingIssues(value); setReleasePreview(null); setPackagePreview(null); setReviewedPlanKey(null); setBuilderStep(1); setDeliveryMethod('publish'); setSelectedFiles(new Set()); selectionInitialized.current = false }
  const driveReconnect = automaticCoveredFiles.length > 0
    && (automaticAuthorization.requires_reconnect === true || automaticAuthorization.can_resume === true)
    && automaticAuthorization.resumable !== false && <DriveReleaseReconnect
      key={`${run?.id}:${automaticAuthorization.id}`} scanId={run?.id} authorizationId={automaticAuthorization.id}
      authorization={automaticAuthorization} owner={releaseOwner} provider={automaticAuthorization.destination?.provider}
      requiresReconnect={automaticAuthorization.requires_reconnect === true} readOnly={readOnly || automaticStatusPending}
      onRefresh={() => setAutomaticStatusRefresh(value => value + 1)}
      onResume={() => resumeAutomaticRelease(run.id, automaticAuthorization.id)} />
  const manualDeliveryAvailable = !automaticStatusPending && !automaticStatusError && releaseFiles.some(file => !automaticCoveredFiles.includes(file.file))
  const savedDomain = releaseBatchDomain(releaseBatchProgress({stage:'release',release_batch_progress:automaticAuthorization?.batch_progress}))
  const savedMembership = savedDomain && automaticAuthorization?.batch_progress?.authorization_id === automaticAuthorization?.id
    ? automaticAuthorization.batch_progress.file_membership : {}
  const deliveryRows = releaseFiles.map((file,index) => {
    const category = automaticCoveredFiles.includes(file.file) ? savedMembership[file.file] || 'unclassified' : null
    // Batch membership says "published" for the request; only the receipt's publication state
    // (or its digest, for older servers) says whether that copy is still the current one.
    const result = releaseResults[file.file]
    const receiptNotCurrent = result?.publication_state ? result.publication_state !== 'current'
      : result?.status === 'published' && !deliveryIsCurrent(file, result)
    if (category === 'published' && receiptNotCurrent) return states[index]
    // A server verdict that the delivered copy is an earlier version (or unconfirmed) outranks any
    // batch classification: "Classification unavailable" must not hide an exact version mismatch.
    if (category && ['out_of_date', 'identity_unknown', 'publishing'].includes(result?.publication_state)) return states[index]
    // An unclassified plan member with a server-confirmed CURRENT receipt (e.g. after republish) is delivered.
    if (category === 'unclassified' && result?.publication_state === 'current' && deliveryIsCurrent(file, result)) return states[index]
    return category ? ({
      published:{status:'released',label:'Published',reason:'Current corrected copy confirmed at the saved destination.'},
      waiting:{status:'waiting',label:'Waiting for delivery',reason:'ACP is waiting for processing and release checks.'},
      processing:{status:'delivering',label:'Publishing',reason:'ACP is delivering this covered copy automatically.'},
      failed:{status:'failed',label:'Delivery needs recovery',reason:'Automatic delivery requires recovery. See the saved-plan status.'},
      skipped:{status:'skipped',label:'Skipped',reason:'Delivery is not confirmed. See the recorded reason.'},
      unclassified:{status:'unknown',label:'Classification unavailable',reason:'ACP has not confirmed the delivery classification.'},
    })[category] : states[index]
  })
  const automaticDelivery = !readOnly && ['active', 'waiting', 'processing', 'publishing', 'blocked'].includes(automaticAuthorization?.status)
    ? automaticAuthorization : null
  const manualReady = automaticDelivery
    ? publishableReady.filter(file => !automaticDelivery.files?.includes(file.file)) : publishableReady
  const recoveryActions = releaseError && <ReleaseRecoveryActions error={releaseError}
    disabled={readOnly || publishing || destinationPending || automaticStatusPending}
    destinationLocked={destinationLocked || Boolean(automaticDelivery)}
    onReconnect={() => document.getElementById('workflow-tab-integrations')?.click()}
    onCheckStatus={async () => {
      const context = releaseContext.current
      const status = await getReleaseStatus(run.id).catch(() => null)
      if (status && ownsRelease(context)) { applyReleaseStatus(status); return true }
      return false
    }}
    destinationPicker={['drive', 'sharepoint'].includes(releaseProvider) ? <ReleaseDestinationPicker
      provider={releaseProvider} value={releaseDestination}
      onChange={value => { setReleaseDestination(value); setReleasePreview(null); setReleaseError(null) }}
      onError={error => setReleaseError({ summary: 'Destination unavailable', details: releaseErrorCopy(error, sourceProduct).message,
        diagnosticCode: releaseErrorCopy(error, sourceProduct).diagnosticCode })} /> : null} />
  if (embedded) return <ReleaseDeliveryCard ready={manualReady} readyCount={publishableReady.length} automaticRelease={automaticDelivery} scopeCount={releaseFiles.length}
    publishedCount={publishedCount} deliveringCount={deliveringCount} failedCount={failedCount}
    publishing={publishing} loading={automaticStatusPending || destinationPending || (settingsPending && !destinationLocked)} readOnly={readOnly}
    allowRemainingIssues={allowRemainingIssues} onRemainingIssuesChange={changeRemainingIssues}
    onPublish={names => publishAll(names, releaseFolderName, true)} onOpenDetails={onOpenDetails}
    destinationLabel={automaticDelivery?.destination_label || (releaseDestination ? `${releaseDestination.folder_name} / Remediated / ${releaseFolder?.name || releaseFolderName || 'Timestamp + user email'}` : releaseProvider === 'drive' ? 'Google Drive / Remediated / Timestamp + user email' : releaseProvider === 'sharepoint' ? 'SharePoint source library / Remediated / Timestamp + user email' : 'ACP managed storage')}
    announcement={releaseAnnouncement} error={automaticDelivery && !manualReady.length && releaseError?.summary === 'Saved release destination restored' ? null : releaseError}
    folders={releaseFolders.length ? releaseFolders : releaseFolder?.url ? [releaseFolder] : []}>
    {driveReconnect}
    {recoveryActions}
    {correctionNotice}
    {(releaseId || publishedList.length > 0 || outOfDateCount > 0) && <ReleaseReports scanId={run?.id} publishedCount={publishedCount} readOnly={readOnly} refreshKey={reportsRefreshKey} />}
  </ReleaseDeliveryCard>

  return (
    <>
      {progressQueue && <ProgressQueueDrawer title={progressQueue.key ? PROGRESS_STATES.find(([key]) => key === progressQueue.key)?.[1] || 'Document progress' : 'All release documents'}
        scopeLabel={`${releaseFiles.length} files in this release scope`}
        files={releaseFiles.flatMap((file,index) => {
          const progress = progressDocuments[index].progressState
          if (progressQueue.key && progress !== progressQueue.key) return []
          return [{file:file.file,status:file.assessment_blocked ? 'blocked' : progress,
            label:file.assessment_blocked ? 'Blocked' : PROGRESS_STATES.find(([key]) => key === progress)?.[1],
            reason:file.assessment_blocked_reason || states[index].reason,findingCount:file.total_findings}]
        })} onClose={() => setProgressQueue(null)}/>}
      {/* ABOVE the conformance report, not below it. The artifact this screen produces is a
          compliance record, and "certified" against an unstated scope is a claim nobody can
          check later — so what was assessed is stated before what was concluded. */}
      {/* The document selection reaches THIS screen for the first time. Remediate has always
          honoured it; the screen that produces the compliance record never knew about it, so
          a report could be read as covering an estate that two documents of it were fixed in. */}
      <ScopeBanner run={run} fileCount={files.length}
                   docScope={documentScopeSentence(documentSelection(files, triage))} />
      <div role="tablist" aria-label="Release views" className="rem-workspace-tabs">
        {[["manage", "Manage publication"], ["reports", "Reports"]].map(([key, label]) => <button key={key} type="button" role="tab" id={`release-tab-${key}`} aria-selected={releaseTab === key} aria-controls={`release-panel-${key}`} tabIndex={releaseTab === key ? 0 : -1} onClick={() => setReleaseTab(key)} onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? 'manage' : event.key === 'End' ? 'reports' : releaseTab === 'manage' ? 'reports' : 'manage'; setReleaseTab(next); document.getElementById(`release-tab-${next}`)?.focus() } }}>{label}</button>)}
      </div>
      <div role="tabpanel" id="release-panel-manage" aria-labelledby="release-tab-manage" hidden={releaseTab !== 'manage'}>
      {driveReconnect}
      <section className="panel release-overview" aria-labelledby="release-title">
        <div className="release-overview__heading">
          <div>
            <h2 id="release-title" style={{ margin: 0 }}>Release</h2>
            <p className="muted" style={{ margin: '5px 0 0' }}>{allowRemainingIssues ? 'Deliver saved corrected copies with remaining issues recorded. Originals stay unchanged.' : 'Deliver corrected copies without changing the originals. Choose below whether to include remaining issues.'}</p>
          </div>
          <div className="release-overview__actions">
            <button className="ghost" onClick={() => run?.id && openReport(run.id)}>Download report</button>
            {manualDeliveryAvailable && <button className="ghost" title="Inspect individual files or choose package options"
                    onClick={startRelease}>More delivery options</button>}
          </div>
        </div>
        <AutomaticPublicationStatus authorization={currentAutomaticAuthorization} pending={automaticStatusPending} error={automaticStatusError} compact destinationLabel={releaseDestination?.name || releaseProvider}
          outOfDateFiles={(publication?.out_of_date || []).map((item) => item?.file).filter(Boolean)} />
        <ReleaseOutcomeSummary scanId={run?.id} authorization={automaticCoveredFiles.length ? currentAutomaticAuthorization : null}
          pending={automaticStatusPending} error={automaticStatusError} files={releaseFiles} results={releaseResults}
          snapshot={remediationSnapshot} folders={releaseFolders.length ? releaseFolders : releaseFolder?.url ? [releaseFolder] : []}
          onRetry={() => setAutomaticStatusRefresh(value => value + 1)} />
        <p className="muted">Manage remaining work in Remediate.</p>
        <p hidden aria-label="Release status overview" className="release-clarity-counts">
          <span><b>{publishableReady.length}</b> Ready</span>
          <span><b>{publishedCount}</b> Delivered</span>
          {deliveringCount > 0 && <span><b>{deliveringCount}</b> Delivering</span>}
        </p>
        {/* Retired duplicate accounting kept for reversibility; the concise status line is live. */}
        <div hidden data-retired="release-accounting">
        <p aria-label="Release status overview" style={{ margin: '14px 0 0', fontSize: 13 }}>
          <b>{publishableReady.length}</b> ready <span className="muted"> · </span>
          <b>{pendingReview.files}</b> need review <span className="muted"> · </span>
          <b>{staleReady.length}</b> source changed <span className="muted"> · </span>
          <b>{pubStarted ? publishedCount : 0}</b> released
          {failedCount > 0 && <><span className="muted"> · </span><b style={{ color: 'var(--error-fg-strong)' }}>{failedCount}</b> failed</>}
        </p>
        <dl className="stage-live-accounting" aria-label="Live release accounting">
          <div><dt>Released</dt><dd><LiveCounter value={pubStarted ? publishedCount : 0} /></dd></div>
          <div><dt>Ready</dt><dd>{publishableReady.length.toLocaleString()}</dd></div>
          <div><dt>Pending</dt><dd>{Math.max(0, ready.length - Object.keys(done).length - failedCount).toLocaleString()}</dd></div>
          {failedCount > 0 && <div className="stage-live-accounting__exception"><dt>Failed</dt><dd>{failedCount.toLocaleString()}</dd></div>}
        </dl>
        </div>
        {correctionNotice}
        <ReleaseReports compact scanId={run?.id} releaseId={releaseId} files={releaseFiles} results={releaseResults} publishedCount={publishedCount} readOnly={readOnly} refreshKey={reportsRefreshKey}>
        {({ reportSummary, reportsByFile }) => <ReleaseCompletionDocuments coveredFiles={automaticCoveredFiles} scopeId={savedDomain?.scope_id} revision={savedDomain?.revision} files={releaseFiles} states={deliveryRows} progressDocuments={progressDocuments} results={releaseResults} urls={pubUrls}
          filter={outcomeFilter} onFilter={setOutcomeFilter} readOnly={readOnly} publishing={publishing}
          onRetry={names => publishAll(names, releaseFolder?.name || releaseFolderName, true)} reportActions={reportSummary} reportsByFile={reportsByFile}
          receipt={(releaseId || publishedList.length > 0 || outOfDateCount > 0) && <details className="release-receipt" aria-label="Delivery receipt"><summary>Delivery receipts and destination links</summary><section>
          <h3>{failedCount ? 'Partial delivery receipt' : deliveringCount ? 'Delivery in progress' : outOfDateCount || unconfirmedCount ? 'Delivery receipt — includes copies that are not current' : 'Delivery receipt'}</h3>
          <p><b>{publishedCount} delivered</b> · {failedCount} failed · {deliveringCount} in progress · {releaseFiles.length - publishedCount - outOfDateCount - unconfirmedCount} in-scope files not delivered.</p>
          {(outOfDateCount > 0 || unconfirmedCount > 0) && <p><b>{outOfDateCount} published {outOfDateCount === 1 ? 'copy is' : 'copies are'} out of date</b>{unconfirmedCount ? ` · ${unconfirmedCount} published ${unconfirmedCount === 1 ? 'version' : 'versions'} can’t be confirmed` : ''}. This receipt records an earlier version than the current corrected copy.</p>}
          <p className="muted">Recorded delivery within this document scope; changing the checkboxes does not change this receipt. Originals unchanged.</p>
          {releaseId && <small>Release {releaseId}</small>}
          {releaseFolders.filter((folder) => folder.url).map((folder) => <p key={folder.id}><a href={folder.url} target="_blank" rel="noopener noreferrer">Open {folder.name || 'delivery folder'} ↗</a></p>)}
          <button className="ghost small" onClick={downloadReleaseManifest}>Download delivery receipt (manifest)</button>
          {manifestError && <p role="alert">{manifestError}</p>}
        </section></details>} />}
      </ReleaseReports>
      <details className="release-safeguards" style={{ marginTop: 12, borderTop: '1px solid var(--line)', paddingTop: 10 }}>
          <summary style={{ cursor: 'pointer', fontSize: 12.5, fontWeight: 600 }}>Release safeguards, destination, and evidence</summary>
          <div style={{ marginTop: 8, fontSize: 12.5, lineHeight: 1.6 }}>
            <p style={{ margin: 0 }}><b>Destination:</b> {releaseDestinationPhrase({ provider: releaseProvider, anyDrive, driveMirrorEnabled, driveMirrorFolder })}</p>
            <p style={{ margin: '4px 0' }}><b>Record:</b> {orgLabel} · WCAG 2.1 Level AA · {reportDate}. Original files are never overwritten.</p>
            <button className="linklike" onClick={() => run?.id && openReport(run.id)}>Download scope-limited report (PDF)</button>
            <div className="release-notification-settings">
              <label><input type="checkbox" checked={completionSound} onChange={(e) => {
                setCompletionSound(e.target.checked)
                try { window.localStorage.setItem('acp.release.completionSound', e.target.checked ? 'on' : 'off') } catch { /* preference stays in this tab */ }
              }} /> Play a short sound when a release finishes</label>
              {typeof Notification !== 'undefined' && Notification.permission === 'default' && (
                <button className="ghost small" onClick={() => Notification.requestPermission()}>Enable browser notifications</button>
              )}
            </div>
          </div>
        </details>
      </section>

      {automaticStatusError && <p role="status">{automaticStatusError} <button className="linklike" onClick={() => setAutomaticStatusRefresh(n => n + 1)}>Refresh automatic publication status</button></p>}
      <ReleaseQuickActions automaticStatusError={automaticStatusError} automaticStatusPending={automaticStatusPending} automaticNeedsReconnect={automaticAuthorization?.requires_reconnect === true} automaticNeedsAttention={automaticAuthorization?.status === 'blocked'} automaticCoveredFiles={automaticCoveredFiles} runId={run?.id} files={releaseFiles} ready={publishableReady} destination={releaseDestination}
        folderName={releaseFolderName} readOnly={readOnly} publishing={publishing} destinationLocked={destinationLocked} destinationPending={destinationPending || (settingsPending && !destinationLocked)}
        announcement={releaseError ? 'Publishing needs attention. See the message below.' : releaseAnnouncement}
        allowRemainingIssues={allowRemainingIssues}
        providerLabel={sourceProduct}
        publishedFolders={releaseFolders.length ? releaseFolders : releaseFolder?.url ? [releaseFolder] : []}
        fileStates={Object.fromEntries(releaseFiles.map((file, index) => [file.file, states[index]]))}
        releaseOptions={<div className="panel" style={{ marginTop: 12, padding: 14 }}>
          <label><input type="checkbox" checked={allowRemainingIssues} disabled={readOnly || publishing}
            onChange={event => changeRemainingIssues(event.target.checked)} /> Publish with remaining issues</label>
          <p className="muted" style={{ margin: '8px 0 0' }}>Optional: publish saved copies even when manual review or accessibility issues remain. Unapproved suggestions are not applied. Remaining issues stay in the audit record; publishing does not certify accessibility.</p>
        </div>}
        readyReasons={[...new Set(states.filter(state => state.status !== 'ready').map(state => state.reason))]}
        destinationLabel={releaseDestination ? `${releaseDestination.folder_name} / Remediated / ${releaseFolder?.name || releaseFolderName || 'Timestamp + user email'}` : releaseProvider === 'drive' ? 'Google Drive / Remediated / Timestamp + user email' : releaseProvider === 'sharepoint' ? 'SharePoint source library / Remediated / Timestamp + user email' : 'ACP managed storage'}
        destinationContent={['drive', 'sharepoint'].includes(releaseProvider) ? <ReleaseCopyDestination provider={releaseProvider} destination={releaseDestination} folder={releaseFolder} folders={releaseFolders} folderName={releaseFolderName} /> : <p>ACP managed storage</p>}
        destinationPicker={<>
          {run?.source === 'local' && <label>Publishing destination <select aria-label="Publishing destination" disabled={destinationLocked || publishing || readOnly} value={releaseProvider || 'local'} onChange={event => { const provider = event.target.value; setReleaseDestination(provider === 'local' ? null : {provider, folder_id: provider === 'drive' ? 'root' : '', folder_name: provider === 'drive' ? 'My Drive' : 'Choose a SharePoint folder'}); setReleasePreview(null) }}><option value="local">Download corrected copies</option><option value="drive">Google Drive</option><option value="sharepoint">SharePoint</option></select></label>}
          {['drive', 'sharepoint'].includes(releaseProvider) ? <ReleaseDestinationPicker provider={releaseProvider} value={releaseDestination}
            onChange={value => { setReleaseDestination(value); setReleasePreview(null) }}
            onError={error => setReleaseError({ summary: 'Destination unavailable', details: error?.message })} /> : <p>Corrected copies remain available in ACP’s managed storage for download.</p>}
        </>}
        onReady={names => publishAll(names, releaseFolderName, true)}
        onProgress={async result => {
          if (Object.values(result.progress || {}).some(value => value?.state === 'published')) {
            try {
              const status = await getReleaseStatus(run.id)
              applyReleaseStatus(status)
              for (const row of status.documents || []) {
                const key = `${row.file}:${row.artifact_digest || row.published_at}`
                if (row.status === 'published' && !continuationReported.current.has(key)) {
                  continuationReported.current.add(key); onPublish?.(row.file)
                }
              }
            } catch { /* The next durable refresh retries. */ }
          }
        }} />

      <AutomaticReleasePackage scanId={run?.id} authorization={automaticAuthorization} />
      {packageJob && <section className="release-notice release-package-job" role="status" aria-label="Prepared package status">
        <span><b>{packageJob.status === 'done' ? 'Download package ready' : packageJob.status === 'dead' ? 'Download package failed' : 'Download package in progress'}</b><br />
          {packageJob.status === 'done' ? 'Prepared safely and available after navigation or reload.' : packageJob.phase || 'The package continues in the background.'}</span>
        {packageJob.status === 'done'
          ? <button className="qbtn approve" disabled={downloading} onClick={downloadQueuedPackage}>{downloading ? 'Downloading…' : 'Download ZIP'}</button>
          : packageJob.status === 'dead' ? <button className="ghost" onClick={startRelease}>Prepare again</button> : null}
      </section>}
      {releaseError && (
        <section className="release-recovery" role="alert" aria-labelledby="release-error-title">
          <div>
            <b id="release-error-title">{releaseError.summary}</b>
            <p>{releaseError.details}</p>
            <details><summary>View details</summary><p>Scan {run?.id || 'unknown'} · {sourceProduct}{releaseError.diagnosticCode ? ` · Error code: ${releaseError.diagnosticCode}` : ''}. Completed copies remain safe and original files are unchanged.</p></details>
          </div>
          {recoveryActions}
          <div className="release-recovery__actions">
            {releaseError.retryFiles ? <button disabled={readOnly || publishing || destinationPending || !selectableReady.some(file => !done[file.file] && releaseError.retryFiles.includes(file.file))}
              onClick={() => { const names = releaseError.retryFiles; setReleaseError(null); publishAll(names, releaseFolder?.name || releaseFolderName, true) }}>Publish to saved destination</button>
              : releaseError.retry ? <button disabled={readOnly || publishing} onClick={() => { const retry = releaseError.retry; setReleaseError(null); retry() }}>{releaseError.retryLabel || 'Retry'}</button> : null}
            <button className="ghost" onClick={() => document.getElementById('workflow-tab-liveops')?.click()}>Open Live Operations</button>
            <button className="ghost" onClick={() => setReleaseError(null)}>Dismiss</button>
          </div>
        </section>
      )}
      {releaseAnnouncement && !releaseError && (
        <div className="release-notice" role="status">
          <span>{releaseAnnouncement}</span>
          <button className="ghost small" onClick={() => setReleaseAnnouncement('')}>Dismiss</button>
        </div>
      )}
      {/* Release Center — the controlled-release summary. NOT a conformance certificate: ACP's
          automated checks verify WITHIN the selected scope; they cannot certify overall WCAG
          conformance. The estate score and "certifiable/conformant" language are gone for exactly
          that reason, and the PDF is a secondary evidence artifact, not the headline. */}
      <details hidden className="panel release-record" style={{ borderLeft: '4px solid var(--success-fg)' }}>
        <summary className="release-record__summary">
          <span><b>Release details and evidence</b><small>{orgLabel} · WCAG 2.1 Level AA · {reportDate}</small></span>
          <span>{ready.length} ready · {pubStarted ? publishedCount : 0} released</span>
        </summary>
        <div className="release-record__body">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 18, flexWrap: 'wrap' }}>
          <div style={{ flex: '1 1 340px' }}>
            <h2 style={{ margin: 0 }}>🚀 Release Center</h2>
            <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
              {orgLabel} · WCAG 2.1 Level AA · {reportDate}
            </div>
            <p style={{ fontSize: 13.5, lineHeight: 1.6, margin: '12px 0 0', maxWidth: 620 }}>
              <b style={{ color: 'var(--success-fg)' }}>{run?.certifiable ?? 0}</b> of <b>{(run?.files ?? 0).toLocaleString()}</b> documents were <b>automatically verified within the selected scope</b> and are ready to release{run?.error ? <> · {run.error} could not be analysed</> : null}. ACP verifies the criteria in scope — it does not certify overall conformance.
            </p>
          </div>
          <div style={{ textAlign: 'right', minWidth: 150, fontSize: 13.5, lineHeight: 1.9 }}>
            <div><b style={{ color: 'var(--success-fg)', fontSize: 17 }}>{ready.length}</b> ready for release</div>
            <div><b style={{ color: pubStarted ? 'var(--success-fg)' : 'var(--muted)', fontSize: 17 }}>{pubStarted ? publishedCount : 0}</b> released</div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Policy: remediated copy → {releaseDestinationPhrase({ provider: releaseProvider, anyDrive, driveMirrorEnabled, driveMirrorFolder })}</div>
          </div>
        </div>
        <div style={{ marginTop: 14, fontSize: 12.5 }}>
          <span className="muted">Evidence &amp; reports: </span>
          <button className="linklike" onClick={() => run?.id && openReport(run.id)}>⤓ Download scope-limited report (PDF)</button>
        </div>
        <div aria-label="Release destination" style={{ marginTop: 14, padding: 14,
          border: '1px solid var(--line)', borderRadius: 10, display: 'grid', gap: 6 }}>
          <b>{releaseFolder?.name || 'Remediated / timestamp created when release starts'}</b>
          <span className="muted">{run?.sourceName || run?.source || 'Connected source'} · {releaseFolder?.createdAt ? new Date(releaseFolder.createdAt).toLocaleString() : 'Not created yet'}</span>
          <span><b>{publishedCount}</b> published · <b>{Math.max(0, ready.length - Object.keys(done).length)}</b> remaining · <b>{failedCount}</b> failed</span>
          <span>Original files are unchanged.</span>
          {releaseFolders.length <= 1 && releaseFolder?.url && <a href={releaseFolder.url} target="_blank" rel="noopener noreferrer" aria-label={`Open release folder ${releaseFolder.name}`}>Open release folder ↗</a>}
          {releaseFolders.length > 1 && <div style={{ display: 'grid', gap: 4 }}>
            {releaseFolders.filter((folder) => folder.url).map((folder) => <a key={folder.location || folder.id} href={folder.url} target="_blank" rel="noopener noreferrer" aria-label={`Open release folder ${folder.name} in ${folder.location || 'connected source'}`}>Open {folder.location?.replace(/^graph:/, 'library ') || folder.name} ↗</a>)}
          </div>}
        </div>
        <div className="sr-only" aria-live="polite">{releaseAnnouncement}</div>
        </div>
      </details>

      {/* Release policy — an HONEST description of what the platform actually does, read from the
          real settings, not a selector for a behaviour ACP can't perform. There is one policy:
          write a corrected COPY; the original is never overwritten. The explainer says plainly why
          replace-in-place isn't on offer. */}
      <details hidden className="panel" style={{ borderLeft: '3px solid var(--info-fg)' }}>
        <summary style={{ cursor: 'pointer' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
          <b style={{ fontSize: 13.5 }}>Release policy</b>
          <span style={{ fontSize: 13 }}>
            <span aria-hidden="true" style={{ color: 'var(--info-fg)' }}>●</span> Remediated copy → {releaseDestinationPhrase({ provider: releaseProvider, anyDrive, driveMirrorEnabled, driveMirrorFolder })}
          </span>
        </div>
        </summary>
        <div className="muted" style={{ fontSize: 12.5, marginTop: 6, lineHeight: 1.6 }}>
          The source file is <b>never overwritten</b>. ACP verifies the criteria in scope — it does not certify overall WCAG conformance.
        </div>
        <div className="release-notification-settings">
          <label><input type="checkbox" checked={completionSound} onChange={(e) => {
            setCompletionSound(e.target.checked)
            try { window.localStorage.setItem('acp.release.completionSound', e.target.checked ? 'on' : 'off') } catch { /* preference stays in this tab */ }
          }} /> Play a short sound when a release finishes</label>
          {typeof Notification !== 'undefined' && Notification.permission === 'default' && (
            <button className="ghost small" onClick={() => Notification.requestPermission()}>Enable browser notifications</button>
          )}
        </div>
        <details style={{ marginTop: 8 }}>
          <summary className="linklike" style={{ cursor: 'pointer', fontSize: 12.5 }}>Why can’t I replace the original?</summary>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 6, lineHeight: 1.6, maxWidth: 640 }}>
            Replacing the source file in place is not part of Release. ACP writes corrected copies to a separate timestamped location in Google Drive or each source SharePoint library, which is why your originals are never modified.
          </div>
        </details>
      </details>

      <details hidden className="panel">
        <summary style={{ cursor: 'pointer', fontWeight: 600, listStyle: 'revert' }}>What “release” does <span className="muted" style={{ fontWeight: 400 }}>· what happens to every verified document</span></summary>
        <div className="pubsteps" style={{ marginTop: 12 }}>
          <div className="pubstep"><b>✓ Marked released</b><span className="muted">the re-validated fixed copy becomes the document of record</span></div>
          <div className="pubstep"><b>⤓ Fixed copy in Blob</b><span className="muted">{driveMirrorEnabled && anyDrive ? `the accessible copy lives in ACP Blob storage and the Drive “${driveMirrorFolder}” mirror` : 'the accessible copy lives in ACP Blob storage'}</span></div>
          <div className="pubstep"><b>📦 Original untouched</b><span className="muted">the fixed copy is written to a separate “remediated” folder — the source file is never overwritten</span></div>
          <div className="pubstep"><b>🏷 Audit recorded</b><span className="muted">the verified-in-scope status + timestamp are written to the audit log</span></div>
        </div>
      </details>

      {manualDeliveryAvailable && <details className="release-advanced"><summary>Choose individual files, package options, and full delivery details</summary>
      <section className="panel release-workspace" ref={builderRef} tabIndex={-1} aria-labelledby="release-workspace-title">
      {/* W5 — conditional-release → full-certification graduation. Shown only once a release has
          started (setStatus is NONE before that, and this renders nothing). */}
      {setStatus.status === SET_STATUS.CONDITIONAL && (
        <section className="panel" style={{ borderLeft: '4px solid var(--warn-fg)' }} aria-label="Conditional release status">
          <b style={{ fontSize: 13.5, color: 'var(--warn-fg)' }}>◐ Conditionally released</b>
          <p>{setStatus.released} of {setStatus.total} in-scope files delivered · {setStatus.heldOpen} need verification · {setStatus.verifiedUnreleased} corrected copies await Release.</p>
          <div hidden data-retired="release-graduation-explanation">
          <p style={{ fontSize: 13, lineHeight: 1.6, margin: '8px 0 0', maxWidth: 680 }}>
            <b>{setStatus.released}</b> of <b>{setStatus.total}</b> in-scope documents are released.
            {setStatus.heldOpen > 0 && <> <b>{setStatus.heldOpen}</b> {setStatus.heldOpen === 1 ? 'document is' : 'documents are'} still <b>held</b> pending remediation.</>}
            {setStatus.verifiedUnreleased > 0 && <> <b>{setStatus.verifiedUnreleased}</b> previously-held {setStatus.verifiedUnreleased === 1 ? 'document has' : 'documents have'} been remediated and can be graduated in below.</>}
          </p>
          <p className="muted" style={{ fontSize: 12.5, marginTop: 8, maxWidth: 680 }}>
            Remediate the held documents (each is re-validated on its own remediation path). This release becomes <b>complete within the selected scope</b> once every held document is verified and released — <b>no whole-estate re-scan required</b>.
          </p>
          </div>
          {setStatus.verifiedUnreleased > 0 && (
            <button className="qbtn approve" style={{ marginTop: 10 }} disabled={readOnly || publishing}
                    title={readOnly ? 'Scan History replay — switch to the latest scan to release' : 'Release the remediated formerly-held documents'}
                    onClick={() => { setSelectedFiles(new Set(setStatus.graduatable)); setBuilderStep(2); setReleasePreview(null); builderRef.current?.focus() }}>
              {publishing ? 'Releasing…' : `↑ Release ${setStatus.verifiedUnreleased} remediated document${setStatus.verifiedUnreleased === 1 ? '' : 's'}`}
            </button>
          )}
        </section>
      )}
      {setStatus.status === SET_STATUS.GRADUATABLE && (
        <section className="panel" style={{ borderLeft: '4px solid var(--success-fg)', background: '#F3F8EC' }} aria-label="Ready to release remaining documents">
          <b style={{ fontSize: 13.5, color: 'var(--success-fg)' }}>✓ Ready to release remaining documents</b>
          <p style={{ fontSize: 13, lineHeight: 1.6, margin: '8px 0 0', maxWidth: 680 }}>
            Every previously-held document has been remediated and re-validated. Release the remaining <b>{setStatus.verifiedUnreleased}</b> {setStatus.verifiedUnreleased === 1 ? 'document' : 'documents'} to complete this release within the selected scope — <b>no whole-estate re-scan required</b>.
          </p>
          <button className="qbtn approve" style={{ marginTop: 10 }} disabled={readOnly || publishing}
                  title={readOnly ? 'Scan History replay — switch to the latest scan to release' : undefined}
                  onClick={() => { setSelectedFiles(new Set(setStatus.graduatable)); setBuilderStep(2); setReleasePreview(null); builderRef.current?.focus() }}>
            {publishing ? 'Graduating…' : `Review delivery for ${setStatus.verifiedUnreleased} documents`}
          </button>
        </section>
      )}
      {setStatus.status === SET_STATUS.FULL && setStatus.total > 0 && (
        <section className="panel" style={{ borderLeft: '4px solid var(--success-fg)' }} aria-label="Release complete">
          <b style={{ fontSize: 13.5, color: 'var(--success-fg)' }}>Release complete</b>
          <span className="muted" style={{ fontSize: 13, marginLeft: 8 }}>all {setStatus.total} in-scope documents released. Verification covers the selected checks, not overall accessibility conformance.</span>
        </section>
      )}


        <div className="rubrichdr">
          <h2 id="release-workspace-title" style={{ margin: 0 }}>Choose files <span className="muted">· {selectedReady.length} selected · {releaseFiles.length} in scope</span></h2>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="ghost small" disabled={!selectableReady.length} onClick={() => setSelectedFiles(new Set(selectableReady.map((f) => f.file)))}>Select all</button>
            <button className="ghost small" disabled={!selectedReady.length} onClick={() => setSelectedFiles(new Set())}>Clear</button>
          </div>
        </div>
        {staleReady.length > 0 && (
          <div style={{ marginTop: 10, padding: '10px 14px', borderRadius: 9, background: '#FDECEC', border: '1px solid #E9A8A8', color: '#8A1F1F', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <span>⚠ <b>{staleReady.length} document{staleReady.length !== 1 ? 's' : ''}</b> changed at the source in {sourceProduct} since this scan — re-scan before releasing, or a released fix may be built on an out-of-date version.</span>
            <button className="ghost small" disabled={readOnly || rescanBusy} onClick={rescanStale}>
              {rescanBusy ? 'Re-scanning…' : `↻ Re-scan changed sources (${staleReady.length})`}
            </button>
          </div>
        )}
        {ready.length === 0 ? (
          pendingReview.items > 0 ? (
            <div className="muted" style={{ marginTop: 10, padding: '12px 14px', borderRadius: 9, background: '#FBF1DF', border: '1px solid #EAD9BF', color: '#7A5A12' }}>
              <b>No files are ready for release.</b> {pendingReview.items} finding{pendingReview.items !== 1 ? 's' : ''} await{pendingReview.items === 1 ? 's' : ''} human review across {pendingReview.files} document{pendingReview.files !== 1 ? 's' : ''}. Eligible proposals can be authorized above. Remaining manual work is available in <b>Remediate → Review</b>. Only applied and verified changes make a document ready.
              <div style={{ marginTop: 9 }}><a className="qbtn approve" href="?tab=remediate&mode=review">Review {pendingReview.files} {pendingReview.files === 1 ? 'file' : 'files'}</a></div>
            </div>
          ) : (
            <p className="muted" style={{ marginTop: 10 }}>Nothing verified yet — remediate documents and approve their review items in Remediate first.</p>
          )
        ) : null}
        {releaseFiles.length > 0 && <ReleaseFileSelection
          files={releaseFiles} selectedFiles={selectedFiles} setSelectedFiles={setSelectedFiles}
          done={done} sourceState={srcOf} sourceProduct={sourceProduct}
          releaseProvider={releaseProvider} driveMirrorEnabled={driveMirrorEnabled}
          driveMirrorFolder={driveMirrorFolder} releaseFolder={releaseFolder}
          releaseResults={releaseResults} selectedFile={sel} setSelectedFile={setSel}
          sourcePath={sourcePath} pending={pendingReview.byFile} processing={processingReview} blockers={previewBlockers} allowRemainingIssues={allowRemainingIssues}
          destinationLabel={releaseDestination?.folder_name ? `${releaseDestination.folder_name} / Remediated / ${releaseFolder?.name || releaseFolderName || '<release name>'}` : undefined}
        />}
        {ready.length > 0 && (
          <div className="release-builder" aria-label="Release builder">
            <div className="release-builder__steps" aria-label="Release steps">
              <span className={builderStep === 1 ? 'active' : 'complete'} aria-current={builderStep === 1 ? 'step' : undefined}>1 <b>Choose files</b></span>
              <span className={builderStep === 2 ? 'active' : builderStep > 2 ? 'complete' : ''} aria-current={builderStep === 2 ? 'step' : undefined}>2 <b>Choose delivery</b></span>
              <span className={builderStep === 3 ? 'active' : ''} aria-current={builderStep === 3 ? 'step' : undefined}>3 <b>Review</b></span>
            </div>
            {/* Retired duplicate plan card; the sticky action carries the live selection and destination. */}
            <div hidden data-retired="release-plan-summary"><ReleasePlanSummary compact count={selectedReady.length}
              excluded={staleReady.length + selectedReady.filter((file) => done[file.file]).length}
              method={deliveryMethod} provider={sourceProduct}
              destination={releaseDestination
                ? `${releaseDestination.folder_name} / Remediated / <release name>`
                : releaseDestinationPhrase({ provider: releaseProvider, anyDrive, driveMirrorEnabled, driveMirrorFolder })}
              preserveStructure={preserveHierarchy} estimatedBytes={packagePreview?.estimated_bytes ?? selectedEstimatedBytes} /></div>
            {builderStep === 1 ? (
              <ReleaseStepPanel id="release-files-step" heading="Choose files" focusOnMount={false} className="release-builder__continue release-action-sticky">
                <span className="muted">{selectedReady.length ? `${selectedReady.length} selected · ${selectedPublishable.length} awaiting Release · ${sourceProduct}` : 'Select at least one ready file.'}</span>
                <button className="qbtn approve" disabled={!selectedReady.length} onClick={chooseDelivery}>Choose delivery</button>
              </ReleaseStepPanel>
            ) : builderStep === 2 ? <ReleaseStepPanel id="release-delivery-step" heading="Choose delivery" focusOnMount>
              <fieldset className="release-methods">
                <legend>Where should the corrected files go?</legend>
                <label className={`${deliveryMethod === 'publish' ? 'selected' : ''}${!selectedPublishable.length ? ' disabled' : ''}`}>
                  <input type="radio" name="delivery-method" value="publish" checked={deliveryMethod === 'publish'} disabled={!selectedPublishable.length} onChange={() => { setDeliveryMethod('publish'); setKeptInAcp(false) }} />
                  <b>Publish to {sourceProduct}</b>
                  <span>{selectedPublishable.length ? `Create ${selectedPublishable.length} protected ${selectedPublishable.length === 1 ? 'copy' : 'copies'} in the connected source.` : 'Every selected file is already published. Choose download to retrieve another copy.'}</span>
                </label>
                <label className={deliveryMethod === 'download' ? 'selected' : ''}>
                  <input type="radio" name="delivery-method" value="download" disabled={allowRemainingIssues} checked={deliveryMethod === 'download'} onChange={() => { setDeliveryMethod('download'); setKeptInAcp(false) }} />
                  <b>Download to this device</b>
                  <span>{allowRemainingIssues ? 'ZIP downloads require verified copies. Turn off publishing with remaining issues to use this option.' : 'Build one ZIP package with the source folder structure and a release manifest; your browser chooses where to save it.'}</span>
                </label>
                <label className={deliveryMethod === 'acp' ? 'selected' : ''}>
                  <input type="radio" name="delivery-method" value="acp" checked={deliveryMethod === 'acp'} onChange={() => { setDeliveryMethod('acp'); setKeptInAcp(false) }} />
                  <b>Keep in ACP for later</b>
                  <span>Create no external copy. Return when you are ready to publish or download.</span>
                </label>
              </fieldset>
              <ReleaseTemplates templates={releaseTemplates} currentPlan={currentDeliveryPlan}
                provider={releaseProvider} saving={templateSaving}
                onApply={applyDeliveryTemplate} onSave={saveDeliveryTemplate} onDelete={deleteDeliveryTemplate} />
              <div className="release-destination-config" aria-live="polite">
                {deliveryMethod === 'download' ? <>
                  <div className="release-destination-config__heading"><b>{downloadFormat === 'original' ? 'Download corrected file' : 'Download package'}</b><span>Saved by your browser</span></div>
                  {selectedReady.length === 1 && <fieldset className="release-download-format">
                    <legend>Download format</legend>
                    <label><input type="radio" name="download-format" checked={downloadFormat === 'original'} onChange={() => setDownloadFormat('original')} /> Corrected file directly</label>
                    <label><input type="radio" name="download-format" checked={downloadFormat === 'zip'} onChange={() => setDownloadFormat('zip')} /> ZIP package</label>
                  </fieldset>}
                  {downloadFormat === 'zip' && <div className="release-name-field">
                  <label htmlFor="release-package-name"><b>ZIP filename</b> <span>Optional</span></label>
                  <input id="release-package-name" value={packageName} onChange={(e) => setPackageName(e.target.value)} placeholder={`acp-release-${run?.id || 'scan'}.zip`} aria-describedby="release-name-help release-name-error" />
                  <small id="release-name-help">“.zip” is added automatically. Leave blank to use the scan-based name.</small>
                  </div>}
                  {downloadFormat === 'zip' && <div className="release-download-options">
                    <label><input type="checkbox" checked={preserveHierarchy} onChange={(event) => setPreserveHierarchy(event.target.checked)} /> Preserve source folder structure</label>
                    <label><input type="checkbox" checked={includeManifest} onChange={(event) => setIncludeManifest(event.target.checked)} /> Include release manifest with checksums</label>
                  </div>}
                  <div className="release-download-options"><label><input type="checkbox" checked={includeVerificationReport} onChange={(event) => setIncludeVerificationReport(event.target.checked)} /> Also download scope-limited verification report (PDF)</label></div>
                  <div className="release-includes"><span>✓ Corrected files</span>{downloadFormat === 'zip' && preserveHierarchy && <span>✓ Original folder structure</span>}{downloadFormat === 'zip' && includeManifest && <span>✓ Release manifest with checksums</span>}</div>
                </> : deliveryMethod === 'acp' ? <>
                  <div className="release-destination-config__heading"><b>ACP controlled storage</b><span>No external delivery</span></div>
                  <p className="muted">The corrected copies stay associated with this scan. You can return to Release later and choose another destination.</p>
                  <div className="release-includes"><span>✓ Corrected copies retained</span><span>✓ Review decisions retained</span><span>✓ Originals unchanged</span></div>
                </> : releaseFolder ? <>
                  <div className="release-destination-config__heading"><b>{sourceProduct} destination</b><span>Connected source</span></div>
                  <div className="release-destination-path">{releaseDestination
                    ? `${releaseDestination.folder_name} / Remediated / ${releaseFolder.name}`
                    : releaseDestinationPhrase({ provider: releaseProvider, anyDrive, driveMirrorEnabled, driveMirrorFolder })}</div>
                  <div className="release-name-field">
                  <label><b>Release folder name</b></label>
                  <div className="release-name-existing">{releaseFolder.name}</div>
                  <small>This scan’s release has started, so retries keep the same destination.</small>
                  </div>
                </> : <>
                  <div className="release-destination-config__heading"><b>{sourceProduct} destination</b><span>Connected source</span></div>
                  {(releaseProvider === 'drive' || releaseProvider === 'sharepoint') && <ReleaseDestinationPicker
                    provider={releaseProvider} value={releaseDestination}
                    onChange={(value) => { setReleaseDestination(value); setReleasePreview(null) }}
                    onError={(error) => setReleaseError({ summary: 'The destination could not be saved.', details: error?.message || 'Try choosing the folder again.' })} />}
                  <div className="release-destination-path">{releaseDestination
                    ? `${releaseDestination.folder_name} / Remediated / <release name>`
                    : releaseDestinationPhrase({ provider: releaseProvider, anyDrive, driveMirrorEnabled, driveMirrorFolder })}</div>
                  <div className="release-name-field">
                  <label htmlFor="release-folder-name"><b>Release folder name</b> <span>Optional</span></label>
                  <input id="release-folder-name" value={releaseFolderName} onChange={(e) => setReleaseFolderName(e.target.value)} placeholder="Automatic: release date and time" aria-describedby="release-name-help release-name-error" />
                  <small id="release-name-help">This name becomes permanent when this scan’s first release starts.</small>
                  </div>
                </>}
                {deliveryNameError && <div id="release-name-error" className="release-name-error" role="alert">{deliveryNameError}</div>}
              </div>
              <div className="release-builder__continue release-builder__navigation release-action-sticky">
                <span className="release-action-context"><b>{selectedReady.length} selected · {selectedPublishable.length} awaiting Release</b><span>{deliveryMethod === 'download' ? 'This device' : deliveryMethod === 'acp' ? 'ACP storage' : `${sourceProduct} · ${releaseDestination?.folder_name || 'Source location'} / Remediated / ${releaseFolder?.name || releaseFolderName || '<release name>'}`}</span></span>
                <button className="ghost" onClick={() => setBuilderStep(1)}>Back to files</button>
                <button className="qbtn approve" disabled={Boolean(deliveryNameError) || previewingRelease} onClick={reviewDelivery}>{previewingRelease ? 'Checking destination…' : 'Review release'}</button>
              </div>
            </ReleaseStepPanel> : <ReleaseStepPanel id="release-review-step" heading="Review release" focusOnMount>
              <div className="release-plan">
                <div>
                  <b>Review your release plan</b>
                  <p>{deliveryMethod === 'publish'
                    ? `${selectedPublishable.length} unreleased corrected ${selectedPublishable.length === 1 ? 'copy' : 'copies'} will be published to ${(releaseFolder?.name || releaseFolderName.trim()) ? `the “${releaseFolder?.name || releaseFolderName.trim()}” release folder` : releaseDestinationPhrase({ provider: releaseProvider, anyDrive, driveMirrorEnabled, driveMirrorFolder })}. Already released files are excluded. Original files will not be changed.`
                    : deliveryMethod === 'download'
                      ? downloadFormat === 'original'
                        ? `The corrected copy of “${selectedReady[0]?.file || 'this file'}” will download directly${includeVerificationReport ? ', followed by a separate scope-limited PDF report download' : ''}. Your browser will ask where to save it. The original file will not be changed.`
                        : `${selectedReady.length} corrected ${selectedReady.length === 1 ? 'file' : 'files'} will be packaged in “${packageName.trim().replace(/\.zip$/i, '') || `acp-release-${run?.id || 'scan'}`}.zip”${preserveHierarchy ? ' with the source folder structure' : ' in one flat folder'}${includeManifest ? ' and a release manifest' : ''}${includeVerificationReport ? ', followed by a separate scope-limited PDF report download' : ''}. Your browser will ask where to save it. Original files will not be changed.`
                      : `${selectedReady.length} corrected ${selectedReady.length === 1 ? 'file' : 'files'} will remain securely in ACP. No external copy will be created and original files will not be changed.`}</p>
                  {deliveryMethod === 'publish' && releasePreview && <div className="release-preview">
                    <div className="release-preview__heading"><b>Exact destination preview</b><span>{releasePreview.documents?.length || 0} files · {releasePreview.folder_state === 'existing' ? 'existing release folder' : 'new release folder'}</span></div>
                    {releasePreview.preflight && <div className={`release-preflight ${releasePreview.preflight.ready ? 'release-preflight--ready' : 'release-preflight--blocked'}`} role={releasePreview.preflight.ready ? 'status' : 'alert'}>
                      <b>{releasePreview.preflight.ready ? '✓ Destination ready' : 'Destination needs attention'}</b>
                      {releasePreview.preflight.message && <span>{releasePreview.preflight.message}</span>}
                    </div>}
                    {(releasePreview.documents || []).slice(0, 5).map((item) => <div className="release-preview__path" key={item.file}><span>{item.action === 'reuse' ? '↻ Reuse' : '+ Create'}</span><code>{item.destination_path}</code></div>)}
                    {(releasePreview.documents || []).length > 5 && <small>+{releasePreview.documents.length - 5} more paths</small>}
                    <p>{releasePreview.collision_policy}</p>
                    {(releasePreview.blockers || []).map((item) => <div className="release-name-error" role="alert" key={item.file}>{item.file}: {item.reason}</div>)}
                  </div>}
                  {deliveryMethod === 'download' && packagePreview && <div className="release-preview">
                    <div className="release-preview__heading"><b>Download preview</b><span>{packagePreview.files || 0} files · {formatReleaseBytes(packagePreview.estimated_bytes)}</span></div>
                    {(packagePreview.paths || []).slice(0, 5).map((path) => <div className="release-preview__path" key={path}><span>+ Include</span><code>{path}</code></div>)}
                    {(packagePreview.paths || []).length > 5 && <small>+{packagePreview.paths.length - 5} more paths</small>}
                    {!packagePreview.estimate_complete && <p className="muted">Final size will be calculated while preparing the download.</p>}
                    {(packagePreview.blockers || []).map((item) => <div className="release-name-error" role="alert" key={item.file}>{item.file}: {item.reason}</div>)}
                  </div>}
                  {deliveryMethod === 'download' && activePackageJob && <div className={`release-preflight ${activePackageJob.status === 'done' ? 'release-preflight--ready' : activePackageJob.status === 'dead' ? 'release-preflight--blocked' : ''}`} role="status">
                    <b>{activePackageJob.status === 'done' ? 'ZIP package ready' : activePackageJob.status === 'dead' ? 'Package preparation failed' : 'Preparing ZIP package'}</b>
                    <span>{activePackageJob.status === 'done' ? 'The prepared download is available even after leaving and returning to Release.' : activePackageJob.phase || 'This work continues safely in the background.'}</span>
                  </div>}
                  <ReleaseModelProvenance scanId={run?.id} selectedFiles={selectedReady.map((file) => file.file)} />
                </div>
                <div className="release-plan__actions release-action-sticky">
                  <span className="release-action-context"><b>{selectedReady.length} selected · {selectedPublishable.length} awaiting Release</b><span>{deliveryMethod === 'download' ? 'Download to this device' : deliveryMethod === 'acp' ? 'Keep in ACP · no external delivery' : `${sourceProduct} · ${releaseDestination?.folder_name || 'Source location'} / Remediated / ${releasePreview?.folder_name || releaseFolder?.name || releaseFolderName || '<release name>'}`}</span></span>
                  <button className="ghost" onClick={() => setBuilderStep(2)}>Back to delivery</button>
                  {deliveryMethod === 'publish'
                    ? <button className="qbtn approve" disabled={readOnly || publishing || !selectedPublishable.length || !releasePreview?.can_release || !previewIsCurrent} onClick={() => setConfirm({ kind: 'selected', files: selectedPublishable.map((f) => f.file), folderName: releasePreview?.folder_name || releaseFolder?.name || releaseFolderName.trim() })}>{publishing ? 'Publishing…' : `Publish ${selectedPublishable.length} ${selectedPublishable.length === 1 ? 'copy' : 'copies'}`}</button>
                    : deliveryMethod === 'download'
                      ? <button className="qbtn approve" disabled={downloading || !selectedReady.length || packagePreview?.can_download === false || (activePackageJob && !['done', 'dead'].includes(activePackageJob.status))} onClick={downloadSelected}>{downloading ? 'Preparing download…' : activePackageJob?.status === 'done' ? 'Download prepared ZIP' : activePackageJob?.status === 'dead' ? 'Retry package preparation' : downloadFormat === 'original' ? 'Download corrected file' : `Download ZIP (${selectedReady.length})`}</button>
                      : <button className="qbtn approve" disabled={!selectedReady.length || keptInAcp} onClick={keepSelectedInAcp}>{keptInAcp ? 'Kept in ACP' : `Keep ${selectedReady.length} in ACP`}</button>}
                </div>
              </div>
            </ReleaseStepPanel>}
          </div>
        )}
        {failedCount > 0 && (
          <div className="release-outcome release-outcome--partial" role="alert">
            <div>
              <b>{failedCount} corrected {failedCount === 1 ? 'copy needs' : 'copies need'} attention</b>
              <p>Successful files remain published. Retrying sends only the failed copies, so completed work is not duplicated.</p>
              {failedCount > failedReady.length && <p className="release-outcome__blocked">{failedCount - failedReady.length} failed {failedCount - failedReady.length === 1 ? 'file is' : 'files are'} no longer retryable until its source, review or verification blocker is resolved.</p>}
            </div>
            <button className="qbtn approve" disabled={!failedReady.length || publishing} onClick={reviewFailedRelease}>
              Review and retry failed ({failedReady.length})
            </button>
          </div>
        )}
        {/* Retired audit summary; the durable receipt above replaces its certificate-based totals. */}
        <div hidden data-retired="release-audit-summary">
        {publishedList.length > 0 ? (
          <div style={{ marginTop: 14 }}>
            {Object.keys(done).length === ready.length && ready.length > 0 && <div className="okline" style={{ marginBottom: 10 }}><b>{ready.length} corrected {ready.length === 1 ? 'copy' : 'copies'} released</b>{releaseFolder?.url && <> · <a href={releaseFolder.url} target="_blank" rel="noopener noreferrer">Open release folder ↗</a></>}</div>}
            <button className="ghost small" onClick={downloadReleaseManifest}>Download release manifest</button>
            {manifestError && <div role="alert" style={{ color: 'var(--error-fg-strong)', marginTop: 8 }}>{manifestError}</div>}
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--muted)', marginBottom: 6 }}>📋 Audit trail · {publishedEntries.length} released</div>
            {publishedEntries.slice(0, 8).map((e) => (
              <div key={e.file} style={{ fontSize: 12.5, padding: '5px 0', borderBottom: '1px solid var(--line)' }}>
                ✓ <b>{e.file}</b> <span className="muted">· fixed copy in Blob · source not overwritten · audit recorded · {fmtPublished(e)}{pubUrls[e.file] && <> · <a href={pubUrls[e.file]} target="_blank" rel="noopener noreferrer">↗ open in {sourceProduct}</a></>}</span>
              </div>
            ))}
            {publishedEntries.length > 8 && <div className="muted" style={{ fontSize: 12, marginTop: 5 }}>+{publishedEntries.length - 8} more</div>}
          </div>
        ) : (
          <p className="muted" style={{ marginTop: 12 }}>Releasing writes the fixed copy to {releaseDestinationPhrase({ provider: releaseProvider, anyDrive, driveMirrorEnabled, driveMirrorFolder })} and records each release in the audit trail here.</p>
        )}
        </div>
      </section>

      </details>}
      </div>
      <div role="tabpanel" id="release-panel-reports" aria-labelledby="release-tab-reports" hidden={releaseTab !== 'reports'}>
      <section className="panel"><h3>Assessment reports</h3><button className="ghost" onClick={() => run?.id && openReport(run.id)}>Download scope-limited report (PDF)</button></section>


      <section className="panel" aria-label="Reports and delivery history">
        <h3>Reports and delivery history</h3>
        {/* Retired duplicate reports mount: compact table actions above retain downloads and recovery. */}
        <ReleaseHistory refreshKey={`${run?.id || ''}:${publishedCount}:${failedCount}`} />
      </section>

      </div>
      {/* Confirmation before a release runs. States, in checkable terms, exactly what will happen —
          destination, that the original is untouched, the audit entry, and that this is not a
          conformance certificate. Escape or a backdrop click cancels. */}
      {confirm && (() => {
        const isBatch = confirm.kind === 'all' || confirm.kind === 'selected'
        const requested = new Set(confirm.files || [])
        const targets = confirm.kind === 'selected' ? selectedPublishable.filter((f) => requested.has(f.file)) : confirm.kind === 'all' ? ready.filter((f) => !done[f.file]) : ready.filter((f) => f.file === confirm.file)
        const cnt = targets.length
        const batchAnyDrive = targets.some((f) => f.drive_file_id)
        const lines = releaseConfirmLines({ count: cnt, provider: releaseProvider, anyDrive: batchAnyDrive, driveMirrorEnabled, driveMirrorFolder, allowRemainingIssues })
        const onGo = () => { if (readOnly || publishing || !previewIsCurrent) return; setConfirm(null); if (isBatch) publishAll(targets.map((f) => f.file), confirm.folderName || ''); else publish(confirm.file) }
        return (
          <div role="dialog" aria-modal="true" aria-label="Confirm release" onClick={() => setConfirm(null)}
               style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.42)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000, padding: 20 }}>
            <div ref={confirmDialogRef} onClick={(e) => e.stopPropagation()}
                 style={{ background: 'var(--panel, #fff)', color: 'var(--ink)', border: '1px solid var(--line)', borderRadius: 12, padding: '20px 22px', maxWidth: 520, width: '100%', boxShadow: '0 12px 40px rgba(0,0,0,0.25)' }}>
              <h3 style={{ margin: '0 0 12px' }}>{isBatch ? `Publish ${cnt} corrected ${cnt === 1 ? 'copy' : 'copies'}?` : `Release ${confirm.file}?`}</h3>
              <ul style={{ margin: '0 0 18px', paddingLeft: 18, fontSize: 13.5, lineHeight: 1.65 }}>
                {confirm.folderName && <li>Release folder: “{confirm.folderName}”</li>}
                {allowRemainingIssues && <li><strong>Publish with remaining issues:</strong> unresolved findings and pending approvals remain recorded. This release does not mark them approved, inspected, verified or compliant.</li>}
                {lines.map((l, i) => <li key={i}>{l}</li>)}
              </ul>
              <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
                <button ref={confirmCancelRef} className="ghost" onClick={() => setConfirm(null)}>Cancel</button>
                <button className="qbtn approve" onClick={onGo} disabled={cnt === 0 || readOnly || publishing || !previewIsCurrent}>{isBatch ? `Publish ${cnt}` : 'Release'}</button>
              </div>
            </div>
          </div>
        )
      })()}
    </>
  )
}

// Retired: duplicate per-file receipt list; report actions now live in Publication outcomes.
export function RetiredDeliveryReceiptFiles({ publishedEntries, fmtPublished, pubUrls }) {
  return <>{publishedEntries.map(entry => <div className="release-receipt__file" key={entry.file}><b>{entry.file}</b><span>{fmtPublished(entry)}</span>{pubUrls[entry.file] && <a href={pubUrls[entry.file]} target="_blank" rel="noopener noreferrer">Open delivered copy ↗</a>}</div>)}</>
}
