import { titleCaseStep } from './processingActivity.js'
import { useState, useEffect } from 'react'
import { normalizeLive } from './liveAssessment.js'
import LiveHeartbeatBars from './LiveHeartbeatBars.jsx'
import LiveCounter from './LiveCounter.jsx'
import SourceVisibility from './SourceVisibility.jsx'
import Term from './Term.jsx'
import AssessmentEvidenceDetails from './AssessmentEvidenceDetails.jsx'

// The Assess RUNNING screen (approved board assess-03). It replaces the mid-run KPI scoreboard
// (LiveAssessment.jsx, kept but no longer mounted here) with a single per-DOCUMENT focus card.
//
// WHY NOT A SCOREBOARD. Board 3's rule: "no metric renders mid-run — a partially-filled count of
// failures reads as a verdict, and there is no honest way to caption one mid-run." The KPIs the old
// panel showed live on the Overview now, where they fill in once a run has FINISHED and can be read
// as a result. Here, mid-run, the screen answers one question — which document is being worked, and
// how far through the estate we are — and says plainly that the results arrive at the end.
//
// EVERYTHING IS FROM THE LIVE SNAPSHOT, or omitted. Document position and the progress bar come from
// completed-vs-eligible; the current file and step from the live_queue "current" block; the ETA from
// rolling throughput. The board's finer per-document checklist (pages/elements read, checks-done)
// needs per-document progress the snapshot does not carry yet, so it is not invented — the current
// STEP is shown instead of a fabricated checklist.
//
// STOP LIVES HERE NOW, board-exact. It used to stay in App.jsx's shared scan-progress banner (also
// used by Discover), duplicating nothing but sitting in the wrong place relative to the board. App
// now suppresses ITS OWN Stop specifically while this card is the one showing (view === 'assess' &&
// assessPhase === 'running') and passes the same cancel handler down as `onStop` — so there is still
// exactly one Stop control on screen at any moment, just the board's chosen one.

function stepLabel(cur) {
  if (!cur) return null
  if (cur.action) return titleCaseStep(cur.action)
  if (cur.criterionName) return titleCaseStep(`Checking ${cur.criterionName}`)
  if (cur.criterion) return titleCaseStep(`Checking ${cur.criterion}`)
  // A rendered shared-feed sentence may contain a filename or remediation language.
  // Use the stage-safe fallback when structured action telemetry is unavailable.
  return 'Assessing This Document'
}

function activityLines(cur, completed, total, processing) {
  const lines = []
  if (cur?.action) lines.push(cur.action)
  if (cur?.criterionName || cur?.criterion_name) {
    const name = cur.criterionName || cur.criterion_name
    lines.push(`Checking ${name}${cur?.criterion ? ` · WCAG ${cur.criterion}` : ''}`)
  } else if (cur?.criterion) lines.push(`Checking WCAG ${cur.criterion}`)
  if (processing > 0) lines.push(`${processing.toLocaleString()} document${processing === 1 ? '' : 's'} processing in parallel`)
  if (!lines.length) lines.push(`Opening and assessing document ${Math.min(total, completed + 1).toLocaleString()} of ${total.toLocaleString()}`)
  return [...new Set(lines.map(titleCaseStep))]
}

export function criterionTag(criterion) {
  const value = String(criterion || '')
  const internalSc = value.match(/^SC_(\d+)_(\d+)_(\d+)$/i)
  const sc = value.match(/^(\d+\.\d+\.\d+)(?=\s|$)/)
  return internalSc ? internalSc.slice(1).join('.') : sc ? sc[1] : value
}

function fmtElapsedSecs(s) {
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const r = s % 60
  return r ? `${m}m ${r}s` : `${m}m`
}

export function refreshedSeconds(measuredAt, now = Date.now()) {
  const at = Number(measuredAt)
  if (!Number.isFinite(at) || at <= 0) return null
  return Math.max(0, Math.floor((now - at) / 1000))
}

function workerDetail(queue) {
  const workers = queue?.workers
  if (!workers || workers.max == null) return 'Worker status unavailable for this run'
  if (workers.capacityScope === 'per_replica') {
    return `Assessment service online · ${workers.max.toLocaleString()} slots per replica`
  }
  const active = workers.busy || 0
  const standingBy = workers.idle == null ? Math.max(0, workers.max - active) : workers.idle
  return `${active.toLocaleString()} active · ${standingBy.toLocaleString()} standing by · ${workers.max.toLocaleString()} total`
}

// One preparation step row: icon (✓ / pulsing dot / ○), label, right-aligned detail.
function PrepStep({ label, detail, status, sublines = [] }) {
  return (
    <div role="listitem">
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ width: 16, flexShrink: 0, display: 'flex', alignItems: 'center',
                       justifyContent: 'center' }} aria-hidden="true">
          {status === 'done' && <span style={{ color: 'var(--green,#1a7f45)', fontSize: 13.5 }}>✓</span>}
          {status === 'active' && <span className="prep-pulse" />}
          {status === 'pending' && <span style={{ color: 'var(--muted)', fontSize: 13 }}>○</span>}
        </span>
        <span style={{ flex: 1, fontSize: 13.5,
                       color: status === 'pending' ? 'var(--muted)' : 'var(--ink)' }}>
          {label}
        </span>
        {detail && (
          <span className="muted" style={{ fontSize: 12.5, fontVariantNumeric: 'tabular-nums' }}>
            {detail}
          </span>
        )}
      </div>
      {sublines.length > 0 && (
        <ul style={{ margin: '5px 0 1px 26px', paddingLeft: 16, color: 'var(--muted)',
                     fontSize: 12.5, lineHeight: 1.55 }}>
          {sublines.map((line) => <li key={line}>{line}</li>)}
        </ul>
      )}
    </div>
  )
}

// Four-step preparation checklist shown while 0 files have completed (indeterminate phase).
// Each step infers its completion state from real snapshot data — no fabricated percentages.
// One pulsing dot (●) marks the active step; done steps show ✓; not-yet-started show ○.
function PrepChecklist({ m, total, completed, processing, elapsed }) {
  const q = m.queue
  const workers = q ? q.workers : null
  const workersCount = workers ? (workers.busy + (workers.idle ?? 0)) : 0
  const workersMax = workers ? workers.max : null

  const invDone = total > 0
  // Workers considered fully started when all slots are filled, OR when some workers are
  // active and the queue already has work (they are busy enough to have queued items).
  const workersDone = workers !== null && (
    (workersMax !== null && workersCount >= workersMax) ||
    (workersCount > 0 && q && (q.queued > 0 || q.inFlight > 0))
  )
  const queueDone = !!(q && (q.queued > 0 || q.inFlight > 0))

  const phases = [
    {
      label: 'Validating scan inventory',
      done: invDone,
      detail: invDone ? `${total.toLocaleString()} files` : null,
    },
    {
      label: 'Starting assessment workers',
      done: workersDone,
      detail: workersMax != null
        ? `${workersCount} of ${workersMax} ready`
        : workersCount > 0 ? `${workersCount} ready` : null,
    },
    {
      label: 'Building the document queue',
      done: queueDone,
      detail: q && q.queued > 0 ? `${q.queued.toLocaleString()} queued` : null,
    },
    // Generic on purpose: several workers open and assess documents concurrently, so naming a
    // single "first" document is both visually noisy and quickly inaccurate. The count at right
    // is the authoritative live snapshot and updates until the determinate view takes over.
    {
      label: 'Assessment in progress',
      done: false,
      detail: `${completed.toLocaleString()} of ${total.toLocaleString()} completed${processing > 0 ? ` · ${processing.toLocaleString()} processing` : ''}`,
    },
  ]

  const firstActiveIdx = phases.findIndex(p => !p.done)
  const steps = phases.map((p, i) => ({
    ...p,
    status: p.done ? 'done' : i === firstActiveIdx ? 'active' : 'pending',
  }))

  const isStalled = elapsed >= 120

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
                    gap: 8, marginBottom: 14, flexWrap: 'wrap' }}>
        <div style={{ fontSize: 14.5, fontWeight: 650 }}>Preparing assessment</div>
        {/* elapsed ticks every second — aria-hidden keeps it out of the polite live region */}
        <div className="muted" style={{ fontSize: 12.5, fontVariantNumeric: 'tabular-nums' }}
             aria-hidden="true">
          {fmtElapsedSecs(elapsed)} elapsed
        </div>
      </div>

      {/* aria-live on the step list so completions are announced politely, not on every tick. */}
      <div aria-live="polite" aria-atomic="false" role="list" aria-label="Preparation steps"
           style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
        {steps.map((s, i) => <PrepStep key={i} {...s} />)}
      </div>

      {isStalled && (
        <p role="alert" style={{ margin: '12px 0 0', fontSize: 12.5, lineHeight: 1.5,
                                 color: 'var(--amber,#92400e)' }}>
          Preparation is taking longer than usual.
          {workers && workersMax != null && ` ${workersCount} of ${workersMax} workers are ready.`}
          {' '}No documents have been assessed yet.
        </p>
      )}
    </div>
  )
}

function CompletedAssessmentResults({ m, total, completed }) {
  const kpis = new Map(m.kpiCards.map((item) => [item.key, item]))
  const findings = kpis.get('findings_so_far')
  const unable = kpis.get('unable_to_assess')
  const outcomes = new Map(m.outcomeChips.map((item) => [item.key, item]))
  const facts = [
    findings && !findings.pending && { label: 'Findings recorded', value: findings.value },
    outcomes.get('passed') && { label: 'Documents passed', value: outcomes.get('passed').count },
    outcomes.get('review') && { label: 'Need review', value: outcomes.get('review').count },
    outcomes.get('failed') && { label: 'Could not complete', value: outcomes.get('failed').count },
    outcomes.get('skipped') && { label: 'Skipped', value: outcomes.get('skipped').count },
  ].filter(Boolean)

  return (
    <section aria-label="Completed assessment results"
             style={{ margin: '2px 0 14px', border: '1px solid var(--line,#e4e8ec)',
                      borderRadius: 9, padding: '10px 12px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12,
                    alignItems: 'baseline', flexWrap: 'wrap' }}>
        <strong style={{ fontSize: 13 }}>Assessment results</strong>
        <span className="muted" style={{ fontSize: 11.5 }}>
          {completed.toLocaleString()} of {total.toLocaleString()} eligible documents finalized
        </span>
      </div>
      {facts.length > 0 && (
        <dl className="stage-live-accounting" aria-label="Final assessment accounting"
            style={{ marginTop: 9 }}>
          {facts.map((fact) => (
            <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value.toLocaleString()}</dd></div>
          ))}
        </dl>
      )}
      <ul style={{ margin: '9px 0 0', paddingLeft: 18, color: 'var(--muted)',
                   fontSize: 12.5, lineHeight: 1.55 }}>
        <li>Conformance results were saved for {completed.toLocaleString()} document{completed === 1 ? '' : 's'}.</li>
        {findings && !findings.pending && (
          <li>{findings.value.toLocaleString()} accessibility finding{findings.value === 1 ? '' : 's'} recorded across the completed assessment.</li>
        )}
        {findings?.pending && <li>The original findings total is unavailable in this saved summary.</li>}
        {unable && !unable.pending && unable.value > 0 && (
          <li>{unable.value.toLocaleString()} document{unable.value === 1 ? '' : 's'} could not be assessed and require{unable.value === 1 ? 's' : ''} follow-up.</li>
        )}
        <li>Remediation recommendations are ready for supported findings.</li>
      </ul>
    </section>
  )
}

export default function AssessRunProgress({ snapshot, throughput, onStop }) {
  const m = normalizeLive(snapshot)

  const total = m.available ? (m.totals.eligible || m.totals.discovered || 0) : 0
  const completed = m.available && snapshot?.kpis ? Number(snapshot.kpis.completed) || 0 : 0
  const processing = m.available && snapshot?.kpis ? Number(snapshot.kpis.processing) || 0 : 0
  const pct = total ? Math.min(100, Math.round((completed / total) * 100)) : 0
  const isPreparing = m.available && pct === 0
  const isFinished = total > 0 && completed >= total
  const measuredAt = snapshot?._live?.measuredAt

  // Elapsed seconds since this screen first appeared — stops ticking once real progress begins.
  const [startedAt] = useState(() => Date.now())
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!isPreparing) return
    setElapsed(Math.round((Date.now() - startedAt) / 1000))
    const t = setInterval(() => setElapsed(Math.round((Date.now() - startedAt) / 1000)), 1000)
    return () => clearInterval(t)
  }, [isPreparing, startedAt])

  const [refreshAge, setRefreshAge] = useState(() => refreshedSeconds(measuredAt))
  useEffect(() => {
    setRefreshAge(refreshedSeconds(measuredAt))
    if (isFinished || !measuredAt) return
    const timer = setInterval(() => setRefreshAge(refreshedSeconds(measuredAt)), 1000)
    return () => clearInterval(timer)
  }, [isFinished, measuredAt])

  if (!m.available) return null

  const cur = m.queue ? m.queue.current : null
  const eta = throughput && (throughput.etaText || (throughput.calibrating ? 'estimating…' : null))
  const opinion = m.secondOpinion
  const documents = m.documents
  const updateMode = snapshot?._live?.mode || 'live'

  const documentActivity = (documents?.items?.length > 0 || (!isFinished && cur?.file)) && (
              <section aria-label="Completed document activity"
                       style={{ marginTop: 14, border: '1px solid var(--line,#e4e8ec)',
                                borderRadius: 9, overflow: 'hidden' }}>
                <strong className="vh">Document activity</strong>
                <div className="assessfile" style={{ margin: '0 0 8px', padding: '10px 12px', background: 'var(--info-bg,#eff6ff)', border: '1px solid var(--info-border,#cfe0f9)', borderRadius: 8 }}>
                  {!isFinished && cur?.file && <>
                    <span className="assessfilelabel muted">Processing Now:</span>
                    <span className="assessfname">{cur.file}</span>
                    <span className="assess-live-stage">{stepLabel(cur)}</span>
                  </>}
                  <span className="muted assessphase">{completed.toLocaleString()} of {total.toLocaleString()} documents assessed</span>
                </div>
                {documents?.truncated && (
                  <p className="muted" style={{ margin: '0 12px 5px', fontSize: 12 }}>
                    Showing {documents.displayed.toLocaleString()} most recent completed documents
                  </p>
                )}
                <ul className="assesslist" aria-label="Durable per-document assessment progress"
                    style={{ maxHeight: 420, overflowY: 'auto', margin: 0, padding: '7px 11px' }}>
                  {(documents?.items || []).map((row) => (
                    <li key={row.file} className="done">
                      <span className="alstate" aria-hidden="true">✓</span>
                      <span className="alname" title={row.file}>{row.file}</span>
                      {row.criteria.length
                        ? <span className="alscs">{row.criteria.map((criterion) => (
                            <b key={criterion} title={criterion}>{criterionTag(criterion)}</b>
                          ))}</span>
                        : <span className="alclean">no failures</span>}
                    </li>
                  ))}
                </ul>
              </section>
            )

  return (
    <section className="assess-run-progress" role="region"
             aria-label={isFinished ? 'Assessment complete' : 'Assessment in progress'}
             style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="assess-run-card" style={{ border: '1px solid var(--line,#e4e8ec)', borderRadius: 12,
                                                padding: '14px 16px', background: 'var(--panel,#fff)' }}>
        <SourceVisibility source={m.source} scope={m.scope} />
        <dl className="stage-live-accounting" aria-label="Live assessment accounting">
          <div><dt><Term k="assessment_assessed">Assessed</Term></dt><dd><LiveCounter value={completed} /></dd></div>
          <div><dt><Term k="assessment_processing">Processing</Term></dt><dd>{processing.toLocaleString()}</dd></div>
          <div><dt><Term k="assessment_waiting">Waiting</Term></dt><dd>{Math.max(0, total - completed - processing).toLocaleString()}</dd></div>
          <div><dt><Term k="assessment_eligible">Eligible</Term></dt><dd>{total.toLocaleString()}</dd></div>
        </dl>
        {isPreparing ? (
          <><PrepChecklist m={m} total={total} completed={completed}
                         processing={processing} elapsed={elapsed} />{documentActivity}</>
        ) : (
          <>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                          gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
              <strong style={{ fontSize: 14.5 }}>{isFinished ? 'Assessment complete' : 'Assessing documents'}</strong>
              <div style={{ display: 'grid', justifyItems: 'end', gap: 3 }}>
                {!isFinished && (
                  <LiveHeartbeatBars measuredAt={measuredAt} stage="assess" />
                )}
              <span role="status" style={{ fontSize: 11.5, padding: '2px 7px', borderRadius: 4,
                                            display: 'inline-flex', alignItems: 'center', gap: 5,
                                            background: updateMode === 'reconnecting' ? 'var(--amber-bg,#fff8e6)' : 'var(--green-bg,#f0f7e6)',
                                            color: updateMode === 'reconnecting' ? 'var(--amber,#92400e)' : 'var(--success-fg)',
                                            border: `1px solid ${updateMode === 'reconnecting' ? 'var(--amber-line,#e7c46a)' : 'var(--green-line,#a8cf7a)'}` }}>
                {!isFinished && updateMode === 'live' && <span className="pulsedot" aria-hidden="true" />}
                {isFinished ? 'Updates complete' : updateMode === 'reconnecting' ? 'Reconnecting · last update kept' : 'Live updates'}
                {!isFinished && refreshAge != null && <> · refreshed {refreshAge}s ago</>}
              </span>
              </div>
            </div>

            {opinion && (
              <div role="status" aria-label="Cloud second-opinion status"
                   style={{ border: '1px solid var(--line,#e4e8ec)', borderRadius: 8, padding: '9px 11px', fontSize: 12.5 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, flexWrap: 'wrap' }}>
                  <strong>Cloud second opinions · {opinion.status}</strong>
                  <span className="muted">{opinion.scan.used} of {opinion.scan.limit} requests this scan</span>
                </div>
                <div className="muted" style={{ marginTop: 3 }}>{opinion.reason}</div>
                <div className="muted" style={{ marginTop: 3 }}>
                  {opinion.day.remaining} daily requests remaining · ${Number(opinion.cost.estimated_remaining_usd).toFixed(2)} estimated budget remaining
                  {' · '}${Number(opinion.cost.measured_scan_usd).toFixed(4)} measured for this scan
                </div>
              </div>
            )}

            <progress value={completed} max={Math.max(1, total)}
                      aria-label={`Assessment: ${completed.toLocaleString()} of ${total.toLocaleString()} documents complete`}
                      aria-valuetext={`${completed.toLocaleString()} of ${total.toLocaleString()} documents complete`}
                      style={{ width: '100%', height: 7, display: 'block', marginBottom: 12 }} />

            {isFinished && <CompletedAssessmentResults m={m} total={total} completed={completed} />}

            <div role="list" aria-live="polite" aria-atomic="false"
                 style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
              <PrepStep status="done" label="Validated assessment scope"
                        detail={`${total.toLocaleString()} documents`} />
              <PrepStep status="done" label="Started assessment workers"
                        detail={workerDetail(m.queue)} />
              <PrepStep status={isFinished ? 'done' : 'active'} label="Opened and assessed documents"
                        detail={`${completed.toLocaleString()} of ${total.toLocaleString()} complete${processing > 0 ? ` · ${processing.toLocaleString()} processing` : ''}`}
                        sublines={!isFinished && cur ? activityLines(cur, completed, total, processing) : []} />
              <PrepStep status={isFinished ? 'done' : 'pending'} label="Finalized conformance results"
                        detail={isFinished ? 'Complete' : eta || 'After all documents finish'} />
            </div>

            {documentActivity}

            <details className="assess-live-details"
                     style={{ borderTop: '1px solid var(--line,#e4e8ec)', marginTop: 14 }}>
              <summary style={{ cursor: 'pointer', padding: '10px 0 4px', fontSize: 12.5,
                                fontWeight: 650, color: 'var(--ink)' }}>
                {isFinished ? 'Assessment details' : 'Live processing details'}
              </summary>
              <AssessmentEvidenceDetails scope={m.scope} activity={snapshot?.ai_activity} />
              {!isFinished && (cur || m.queue?.laneLabel) && (
                <div style={{ paddingTop: 7, fontSize: 12.5, lineHeight: 1.5 }}>
                  <div className="muted" style={{ marginBottom: 4 }}>Processing Now</div>
                  {cur ? (
                    <>
                      {cur.file && <strong style={{ fontFamily: 'var(--font-mono)' }}>{cur.file}</strong>}
                      <ul aria-live="polite" style={{ margin: '6px 0 0', paddingLeft: 20 }}>
                        {activityLines(cur, completed, total, processing).map((line) => <li key={line}>{line}</li>)}
                      </ul>
                    </>
                  ) : <span className="muted">{titleCaseStep(m.queue?.laneLabel || 'Waiting For A Worker')}</span>}
                </div>
              )}

            </details>

            {isFinished && (
              <p style={{ margin: '12px 0 0', fontSize: 12.5 }}>
                <strong>Assessment finished for {completed.toLocaleString()} documents.</strong>{' '}
                Conformance results and remediation recommendations are ready.
              </p>
            )}
          </>
        )}
      </div>

      {/* Stop, board-exact placement: the button beside the sentence explaining what it does, not a
          bare icon in a corner. Only while the run is actually active — a finished/cancelled run has
          nothing left to stop, and offering the control anyway would invite a confusing no-op click. */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
        {onStop && m.active && (
          <button type="button" className="ghost small" onClick={onStop}
                  title="Stop this run — documents already assessed are kept">
            Stop
          </button>
        )}
        {/* The board's central promise. Not a caption on a number — there is no number to caption. */}
        <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.6, flex: '1 1 260px' }}>
          Results appear when the run finishes, not before — a half-populated count of failures reads as a
          verdict, and there is no honest way to caption one mid-run. Stopping keeps the documents already
          assessed; nothing is written back to the connected source during assessment.
        </p>
      </div>
    </section>
  )
}
