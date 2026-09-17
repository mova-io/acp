import { useState, useEffect, useCallback, useId } from 'react'
import Drawer from './Drawer.jsx'
import { handleWorkflowTabKeyDown } from './workflowTabs.js'
import { retentionOf } from './FileDrawer.jsx'
import {
  listDispositionPolicies, getInventoryDiff, previewDispositionPolicy,
  listDispositionApprovals, approveDisposition, rejectDisposition, listDispositionAudit,
} from './api.js'
import {
  filesForSource, runsForSource, inventoryFacts, dispositionRows, dispositionOf,
  runOutcome, runDurationMs, needsAttention, scopeFacts, folderOf, inventoryChangeLine,
  matchCounts, matchedFilesForSource, sourceKeys,
  fmtSize, fmtDuration, fmtWhen, fmtCount, orAbsent, NOT_AVAILABLE, NOT_CONFIGURED,
} from './sourceOps.js'

// "Manage <source>" — a SOURCE OPERATIONS panel, not a miniature compliance dashboard.
//
// What this replaced, and why: the drawer used to open on a compliance donut, a top-flagged-
// documents list and a paragraph about an "agent", under the subtitle
// `undefined · 0 docs · agent: undefined`. Every one of those is an Assess fact rendered on a
// Sources surface, and the subtitle was actively false — the card beside it said **Healthy**
// while the drawer said the connection had no department, no documents and no agent. (It had
// all three: the OneDrive card is a hard-coded CONNECTABLE row with no `dept`/`agent`, and its
// file rows are keyed `sharepoint`, not `sp-root`. See sourceOps.sourceKeys.)
//
// The four tabs answer the four questions an operator actually opens this for: is the
// connection working (Overview), what can ACP see (Scope), what happened to the files (Rules),
// and what has this source been doing (Activity). Compliance stays in Assess; the only bridge
// is a stated handoff count on Overview.
//
// Colour discipline: red means access failed. Archive/delete candidates are amber — they are
// awaiting a human, not broken.

const TONE = {
  ok:     ['var(--success-fg)', 'var(--success-bg)'],
  review: ['var(--warn-fg)', 'var(--warn-bg)'],
  fail:   ['var(--error-fg-strong)', '#FCEBEB'],
  muted:  ['#5F5E5A', '#EEEDEA'],
}
const TABS = ['Overview', 'Scope', 'Rules', 'Activity']

// The database stores policy matches as JSON text, while some API versions and fixtures return
// the already-decoded array. Accept both: calling `.map` on the raw production string was the
// drawer-breaking exception reported from Manage My Drive.
export function policyMatchRows(value) {
  if (Array.isArray(value)) return value.filter((row) => row && typeof row === 'object')
  if (value && typeof value === 'object') return [value]
  if (typeof value !== 'string' || !value.trim()) return []
  try {
    const parsed = JSON.parse(value)
    return Array.isArray(parsed)
      ? parsed.filter((row) => row && typeof row === 'object')
      : parsed && typeof parsed === 'object' ? [parsed] : []
  } catch {
    return []
  }
}

const Pill = ({ tone = 'muted', children }) => {
  const [fg, bg] = TONE[tone] || TONE.muted
  return <span className="badge" style={{ background: bg, color: fg }}>{children}</span>
}

// A label/value row. `value` of null prints the stated absence rather than the field vanishing —
// a dropped row and a configured-but-empty row are indistinguishable once one of them is gone.
const Fact = ({ label, value, absent = NOT_CONFIGURED }) => (
  <div style={{ display: 'flex', gap: 12, padding: '6px 0', borderBottom: '1px solid var(--line)', fontSize: 13 }}>
    <span className="muted" style={{ flex: '0 0 118px' }}>{label}</span>
    <span style={{ flex: 1, minWidth: 0, wordBreak: 'break-word' }}>{orAbsent(value, absent)}</span>
  </div>
)

const Kpi = ({ label, value }) => (
  <div style={{ border: '1px solid var(--line)', borderRadius: 10, padding: '10px 12px', background: 'var(--surface)' }}>
    <div style={{ fontSize: 19, fontWeight: 600, lineHeight: 1.2 }}>{value}</div>
    <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>{label}</div>
  </div>
)

export default function SourceDrawer({ source, files = [], scans = [], onClose, onPickFile, onScan, onOpenAssess, busy = false,
                                       initialTab = 'Overview' }) {
  // A fresh mount every time this drawer opens (Integrations.jsx renders it only while
  // `selSrc` is set), so the literal prop-as-initial-state read is enough — no effect needed
  // to keep it in sync with a prop that can't change under an already-mounted drawer.
  const [tab, setTab] = useState(initialTab)
  const tabIds = useId()
  const [policies, setPolicies] = useState(null)   // null = still loading / unavailable
  const [policyErr, setPolicyErr] = useState('')
  const [invDiff, setInvDiff] = useState(null)     // null = none to show; the line is omitted
  const [previews, setPreviews] = useState({})     // policy_id -> preview | 'error'
  const [openRule, setOpenRule] = useState(null)   // policy_id whose matches are expanded
  const [approvals, setApprovals] = useState(null) // null = not loaded; 'error' = failed
  const [deciding, setDeciding] = useState(null)   // audit id mid-decision
  const [audit, setAudit] = useState(null)         // null = not loaded; 'error' = failed

  // Discovery rules are a real backend resource (disposition policies), so they are fetched, not
  // described. A failed fetch says so; it does not fall back to an encouraging empty list, which
  // would read as "no rules configured" — the one answer that stops an operator looking.
  useEffect(() => {
    if (!source) return
    let live = true
    listDispositionPolicies()
      .then((p) => { if (live) setPolicies(Array.isArray(p) ? p : []) })
      .catch((e) => { if (live) { setPolicies([]); setPolicyErr(e?.message || 'could not load discovery rules') } })
    return () => { live = false }
  }, [source])

  // What changed since this source's PREVIOUS run. The baseline is passed explicitly rather than
  // left to the route's default, because the drawer already knows which run it is naming on
  // screen — so the number and the date beside it cannot drift apart. A failed fetch leaves
  // invDiff null and the line simply does not render; there is no zero-filled fallback, because
  // "0 new · 0 changed" is a claim we would not have earned.
  const runIds = runsForSource(scans, source).slice(0, 2).map((r) => r.id)
  const [curId, prevId] = runIds
  useEffect(() => {
    if (!curId || !prevId) { setInvDiff(null); return }
    let live = true
    getInventoryDiff(curId, prevId)
      .then((d) => { if (live) setInvDiff(d) })
      .catch(() => { if (live) setInvDiff(null) })
    return () => { live = false }
  }, [curId, prevId])

  // Rule match counts come from the real preview endpoint, and it is not cheap: each call
  // evaluates the policy over the whole `documents` table in Python. So they are fetched when the
  // RULES TAB is opened, not when the drawer is — a per-source panel should not run N full-table
  // scans just because somebody looked at Overview.
  // The pending-approval queue loads with the Rules tab too. Cheap (one query), unlike the
  // per-policy previews beside it.
  const loadApprovals = useCallback(() => {
    listDispositionApprovals()
      .then((a) => setApprovals(Array.isArray(a) ? a : []))
      .catch(() => setApprovals('error'))
  }, [])
  useEffect(() => {
    if (tab !== 'Rules') return
    loadApprovals()
  }, [tab, loadApprovals])

  // The lifecycle trail loads with the ACTIVITY tab — that is where "what has this source been
  // doing" already lives, and a decision recorded in the Rules tab shows up here as history.
  const loadAudit = useCallback(() => {
    listDispositionAudit(200)
      .then((a) => setAudit(Array.isArray(a) ? a : []))
      .catch(() => setAudit('error'))
  }, [])
  useEffect(() => {
    if (tab !== 'Activity') return
    loadAudit()
  }, [tab, loadAudit])

  useEffect(() => {
    if (tab !== 'Rules' || !policies || !policies.length) return
    let live = true
    const pending = policies.filter((p) => p.policy_id && previews[p.policy_id] === undefined)
    if (!pending.length) return
    // Sequential, not Promise.all: these are full scans, and firing six at once at a database is
    // how a "just showing a count" panel becomes the reason a scan times out.
    ;(async () => {
      for (const p of pending) {
        try {
          const r = await previewDispositionPolicy(p.policy_id)
          if (!live) return
          setPreviews((s) => ({ ...s, [p.policy_id]: r }))
        } catch {
          if (!live) return
          setPreviews((s) => ({ ...s, [p.policy_id]: 'error' }))
        }
      }
    })()
    return () => { live = false }
  }, [tab, policies])

  if (!source) return null

  const labelOf = (f) => retentionOf(f).label
  const mine = filesForSource(files, source)
  const runs = runsForSource(scans, source)
  const latest = runs[0] || null
  const outcome = runOutcome(latest)
  const inv = inventoryFacts(mine)
  const rows = dispositionRows(mine, labelOf)
  const attention = needsAttention({ files: mine, runs, labelOf })
  const assessable = rows.find((r) => r.key === 'assessable')?.count || 0
  const title = `Manage ${source.name || source.type || 'source'}`

  // The header states the connection first, because that is the question the drawer is opened
  // with. "Connected" is a fact about the token; the run outcome is a separate fact and is never
  // folded into it — a connected source whose last run could not read 18 files is both.
  const subtitle = latest
    ? `Connected · Last discovery ${fmtWhen(latest.completed_at)} · ${outcome.label}`
    : 'Connected · No discovery completed yet'

  return (
    <Drawer title={title} subtitle={subtitle} onClose={onClose}>
      <div className="subtabs" role="tablist" aria-label="Source operations">
        {TABS.map((t) => (
          <button key={t} role="tab" id={`${tabIds}-tab-${t}`} aria-selected={tab === t}
                  aria-controls={`${tabIds}-panel`} tabIndex={tab === t ? 0 : -1}
                  onKeyDown={handleWorkflowTabKeyDown}
                  className={`tab${tab === t ? ' on' : ''}`}
                  style={{ display: 'inline-block', padding: '5px 12px', fontSize: 13 }}
                  onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>

      {attention.length > 0 ? (
        <>
          <h4 className="drawerh">Needs attention <span style={{ float: 'right' }}>{attention.length}</span></h4>
          <div style={{ display: 'grid', gap: 8 }}>
            {attention.map((a) => {
              const [fg, bg] = TONE[a.tone] || TONE.muted
              return (
                <div key={a.key} style={{ background: bg, borderRadius: 9, padding: '9px 11px' }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: fg }}>{a.title}</div>
                  <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{a.detail}</div>
                </div>
              )
            })}
          </div>
        </>
      ) : (
        <p style={{ marginTop: 16, fontSize: 13, color: TONE.ok[0] }}>✓ No discovery issues</p>
      )}

      {/* The per-tab body. "Needs attention" above is shown on every tab, so it sits outside. */}
      <div role="tabpanel" id={`${tabIds}-panel`} aria-labelledby={`${tabIds}-tab-${tab}`}>
      {tab === 'Overview' && (
        <>
          <h4 className="drawerh">Discovery summary</h4>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8 }}>
            <Kpi label="documents discovered" value={fmtCount(inv.documents)} />
            <Kpi label="total size" value={fmtSize(inv.bytesKb)} />
            <Kpi label="owners" value={fmtCount(inv.owners)} />
            <Kpi label="departments" value={fmtCount(inv.departments)} />
          </div>

          <h4 className="drawerh">Latest discovery</h4>
          {!latest ? (
            <p className="muted" style={{ fontSize: 13 }}>
              No discovery has completed. ACP has access, but the source has not yet been inventoried.
            </p>
          ) : (
            <div style={{ fontSize: 13 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <Pill tone={outcome.key === 'ok' ? 'ok' : outcome.key === 'warn' ? 'review' : 'muted'}>{outcome.label}</Pill>
                <span className="muted">{fmtWhen(latest.completed_at)}</span>
              </div>
              <Fact label="Files examined" value={latest.files != null ? Number(latest.files).toLocaleString() : null} absent={NOT_AVAILABLE} />
              <Fact label="Could not read" value={latest.error != null ? Number(latest.error).toLocaleString() : null} absent={NOT_AVAILABLE} />
              <Fact label="Duration" value={runDurationMs(latest) == null ? null : fmtDuration(runDurationMs(latest))} absent={NOT_AVAILABLE} />
              <Fact label="Run ID" value={latest.id} absent={NOT_AVAILABLE} />
              {(() => {
                const line = inventoryChangeLine(invDiff)
                if (!line) return null
                return (
                  <div style={{ marginTop: 8 }}>
                    <div style={{ fontSize: 13 }}>{line.text}</div>
                    <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                      since the previous discovery{line.since ? ` on ${fmtWhen(line.since)}` : ''}
                    </div>
                    {line.note && <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{line.note}</div>}
                  </div>
                )
              })()}
            </div>
          )}

          <h4 className="drawerh">Discovery outcome</h4>
          {/* A partition: every discovered file is in exactly one row and the rows sum to the
              total, so the table can be added up. Overlapping tallies cannot. */}
          <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key}>
                  <td style={{ padding: '6px 0', borderBottom: '1px solid var(--line)' }}>
                    <Pill tone={r.tone}>{r.label}</Pill>
                  </td>
                  <td style={{ padding: '6px 0', borderBottom: '1px solid var(--line)', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                    {r.count.toLocaleString()}
                  </td>
                </tr>
              ))}
              <tr>
                <td className="muted" style={{ padding: '6px 0' }}>Total discovered</td>
                <td className="muted" style={{ padding: '6px 0', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                  {inv.documents.toLocaleString()}
                </td>
              </tr>
            </tbody>
          </table>

          <h4 className="drawerh">Next step</h4>
          <p className="muted" style={{ fontSize: 13 }}>
            {assessable.toLocaleString()} discovered file{assessable === 1 ? ' is' : 's are'} eligible for assessment.
            Archive and deletion candidates are held back until they are reviewed.
          </p>
          {onOpenAssess && <button className="ghost small" onClick={onOpenAssess}>Open Assess</button>}
        </>
      )}

      {tab === 'Scope' && (
        <>
          <h4 className="drawerh">Discovery scope</h4>
          <Fact label="Source" value={source.name || source.type} absent={NOT_AVAILABLE} />
          {scopeFacts(source).map((f) => <Fact key={f.label} label={f.label} value={f.value} />)}

          <h4 className="drawerh">Locations</h4>
          <LocationList files={mine} />

          <h4 className="drawerh">Not covered</h4>
          {/* Three operationally different things, never merged into one "excluded" number:
              a rule we configured, a permission Microsoft/Google refused, and a read that
              failed. Only the middle one is a connection problem. */}
          <UncoveredList files={mine} labelOf={labelOf} latest={latest} />
        </>
      )}

      {tab === 'Rules' && (
        <>
          <h4 className="drawerh">
            Active discovery rules
            {policies && <span style={{ float: 'right' }}>{policies.filter((p) => p.enabled !== false).length}</span>}
          </h4>
          {policies == null ? <p className="muted" style={{ fontSize: 13 }}>Loading…</p>
            : policyErr ? <p style={{ fontSize: 13, color: TONE.fail[0] }}>Could not load discovery rules — {policyErr}</p>
              : policies.length === 0 ? (
                <p className="muted" style={{ fontSize: 13 }}>
                  No discovery rules are configured. Rules tag, archive or flag files for deletion review
                  as they are discovered; without any, every discovered file is inventoried and left in place.
                </p>
              ) : (
                <div className="findings">
                  {policies.map((p) => (
                    <div key={p.policy_id || p.name} style={{ padding: '8px 0', borderBottom: '1px solid var(--line)' }}>
                      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <span style={{ fontSize: 13, fontWeight: 600, flex: 1, minWidth: 0 }}>{orAbsent(p.name)}</span>
                        <Pill tone={p.action === 'delete' ? 'review' : p.action === 'archive' ? 'review' : 'muted'}>
                          {p.action === 'delete' ? 'deletion review' : orAbsent(p.action)}
                        </Pill>
                        <Pill tone={p.enabled === false ? 'muted' : 'ok'}>{p.enabled === false ? 'disabled' : 'enabled'}</Pill>
                      </div>
                      <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
                        {policyMatchRows(p.match).map((m) => `${m.field} ${m.op} ${m.value}`).join(' · ') || NOT_AVAILABLE}
                      </div>
                      <RuleMatches source={source} policy={p} preview={previews[p.policy_id]}
                                   open={openRule === p.policy_id}
                                   onToggle={() => setOpenRule(openRule === p.policy_id ? null : p.policy_id)} />
                    </div>
                  ))}
                </div>
              )}
          <h4 className="drawerh">
            Pending approvals
            {Array.isArray(approvals) && (
              <span style={{ float: 'right' }}>{approvals.filter((a) => matchesThisSource(a, source)).length}</span>
            )}
          </h4>
          <ApprovalQueue approvals={approvals} source={source} deciding={deciding}
                         onDecide={async (id, decision) => {
                           setDeciding(id)
                           try {
                             // execute:false — record the decision, never touch the file. See api.js.
                             if (decision === 'approve') await approveDisposition(id, { execute: false })
                             else await rejectDisposition(id)
                             loadApprovals()
                             setAudit(null)   // force a reload; the trail has a new row now
                           } catch {
                             setApprovals('error')
                           } finally {
                             setDeciding(null)
                           }
                         }} />

          <h4 className="drawerh">Review queues</h4>
          {/* Counted from the SAME partition Overview renders, not recomputed — two tabs
              disagreeing about how many files await review is worse than neither showing it. */}
          <ReviewQueues rows={rows} files={mine} labelOf={labelOf} onPickFile={onPickFile} />

          <p className="muted" style={{ fontSize: 12, marginTop: 12 }}>
            A delete rule tags files for <b>deletion review</b>. ACP never deletes on a rule alone —
            an approval is recorded first, and the action is a recoverable trash move.
          </p>
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            These are <b>lifecycle rules</b> — they tag, archive or flag files as they are discovered.
            <i> Which WCAG criteria</i> get assessed where is set separately, under
            <b> Assess → WCAG scope rules</b>.
          </p>
        </>
      )}

      {tab === 'Activity' && (
        <>
          <h4 className="drawerh">
            Run history
            <span style={{ float: 'right' }}>{runs.length}</span>
          </h4>
          <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>
            Discovery runs for this source only, newest first. Expand a run for its timing,
            coverage, errors, owner, and run ID.
          </p>
          {runs.length === 0 ? (
            <p className="muted" style={{ fontSize: 13 }}>No discovery runs recorded for this source.</p>
          ) : (
            <div className="findings">
              {runs.slice(0, 12).map((r) => {
                const o = runOutcome(r)
                return (
                  <details key={r.id} style={{ padding: '8px 0', borderBottom: '1px solid var(--line)' }}>
                    <summary style={{ cursor: 'pointer', fontSize: 13, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                      <span className="muted">{fmtWhen(r.completed_at || r.started_at)}</span>
                      <Pill tone={o.key === 'ok' ? 'ok' : o.key === 'warn' ? 'review' : 'muted'}>{o.label}</Pill>
                      <span className="muted">
                        {r.files != null ? `${Number(r.files).toLocaleString()} examined` : NOT_AVAILABLE}
                        {(r.error || 0) > 0 && ` · ${Number(r.error).toLocaleString()} could not be read`}
                      </span>
                    </summary>
                    <div style={{ marginTop: 6 }}>
                      <Fact label="Started" value={r.started_at ? fmtWhen(r.started_at) : null} absent={NOT_AVAILABLE} />
                      <Fact label="Completed" value={r.completed_at ? fmtWhen(r.completed_at) : null} absent={NOT_AVAILABLE} />
                      <Fact label="Duration" value={runDurationMs(r) == null ? null : fmtDuration(runDurationMs(r))} absent={NOT_AVAILABLE} />
                      <Fact label="Files examined" value={r.files != null ? Number(r.files).toLocaleString() : null} absent={NOT_AVAILABLE} />
                      <Fact label="Could not read" value={r.error != null ? Number(r.error).toLocaleString() : null} absent={NOT_AVAILABLE} />
                      <Fact label="Started by" value={r.owner_email} absent={NOT_AVAILABLE} />
                      <Fact label="Run ID" value={r.id} absent={NOT_AVAILABLE} />
                    </div>
                  </details>
                )
              })}
            </div>
          )}
          <h4 className="drawerh">
            Lifecycle decisions
            {Array.isArray(audit) && (
              <span style={{ float: 'right' }}>{audit.filter((a) => matchesThisSource(a, source)).length}</span>
            )}
          </h4>
          <AuditTrail audit={audit} source={source} />

          {onPickFile && mine.length > 0 && (
            <>
              <h4 className="drawerh">Files that could not be read</h4>
              <UnreadableFiles files={mine} labelOf={labelOf} onPickFile={onPickFile} />
            </>
          )}
        </>
      )}
      </div>

      {/* Persistent footer — configuration actions stay reachable from every tab. */}
      <div style={{ position: 'sticky', bottom: 0, marginTop: 22, paddingTop: 12,
                    borderTop: '1px solid var(--line)', background: 'var(--card)',
                    display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {onScan && <button disabled={busy} onClick={() => onScan(source)}>{busy ? 'Discovering…' : 'Run discovery'}</button>}
        <button className="ghost small" onClick={onClose}>Close</button>
      </div>
    </Drawer>
  )
}

// Does this pending approval belong to the source whose drawer this is? The queue is
// estate-wide in the backend (disposition_audit has no source column; the route joins it in
// from `documents`), so rendering all of it under a heading naming one source would be the
// same boundary error the rule match counts avoid.
//
// A row whose document has vanished belongs to NO source and is shown to everyone: it is the
// row most in need of a decision, and hiding it from every drawer would hide it entirely.
function matchesThisSource(a, source) {
  if (a && a.document_exists === false) return true
  const keys = new Set(sourceKeys(source))
  return keys.has(String((a && a.source) || '').toLowerCase())
}

// Decisions waiting on a person. Rendered above the candidate lists, because something asking
// for a human now outranks a list of things that might one day need one.
function ApprovalQueue({ approvals, source, deciding, onDecide }) {
  if (approvals === null) return <p className="muted" style={{ fontSize: 13 }}>Loading…</p>
  if (approvals === 'error') {
    return (
      <p style={{ fontSize: 13, color: TONE.fail[0] }}>
        Could not load the approval queue. Pending actions are unaffected — none of them run
        without a decision.
      </p>
    )
  }
  const mine = approvals.filter((a) => matchesThisSource(a, source))
  const elsewhere = approvals.length - mine.length
  if (!mine.length) {
    return (
      <p className="muted" style={{ fontSize: 13 }}>
        Nothing is awaiting approval for this source.
        {elsewhere > 0 && ` ${elsewhere.toLocaleString()} pending on other sources.`}
      </p>
    )
  }
  return (
    <>
      <div style={{ display: 'grid', gap: 8 }}>
        {mine.map((a) => {
          const busy = deciding === a.id
          const destructive = a.action === 'delete'
          return (
            <div key={a.id} style={{ border: '1px solid var(--line)', borderRadius: 9, padding: '9px 11px' }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <Pill tone={destructive ? 'review' : 'muted'}>
                  {destructive ? 'deletion review' : orAbsent(a.action)}
                </Pill>
                <span className="fname" style={{ fontSize: 13, flex: 1, minWidth: 0 }}>
                  {a.document_exists === false
                    ? <span className="muted">document no longer exists ({orAbsent(a.doc_id)})</span>
                    : orAbsent(a.path || a.doc_id)}
                </span>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
                {orAbsent(a.detail, '')}
                {a.ts && ` · queued ${fmtWhen(a.ts)}`}
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 7, alignItems: 'center' }}>
                <button className="ghost small" disabled={busy}
                        onClick={() => onDecide(a.id, 'approve')}>
                  {busy ? 'Recording…' : 'Approve'}
                </button>
                <button className="ghost small" disabled={busy}
                        onClick={() => onDecide(a.id, 'reject')}>Reject</button>
              </div>
            </div>
          )
        })}
      </div>
      {/* Said once, under the buttons, rather than per row: Approve here is a RECORDED DECISION,
          not a file operation. ACP's connectors are read-only (CAN_WRITE_BACK is false) and the
          executor is Drive-only, so a button implying otherwise would promise what the
          deployment cannot do. */}
      <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        <b>Approve records the decision and leaves the file untouched.</b> ACP holds read-only
        access to this source, so nothing is moved or trashed from here — the approval and its
        audit record are what a later, separately-authorised run acts on.
      </p>
      {elsewhere > 0 && (
        <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          {elsewhere.toLocaleString()} more pending on other sources.
        </p>
      )}
    </>
  )
}

// Every disposition outcome, newest first — the append-only record behind the queue. It answers
// a different question: not "what needs me now" but "what happened to this file, under which
// rule, and how it ended".
//
// Scoped the same way the queue is, and for the same reason: disposition_audit has no source
// column, so an unscoped render would put the whole estate's history under a heading naming one
// source. The elsewhere count is stated rather than the rows silently dropped.
const AUDIT_TONE = {
  applied: 'ok',
  approved: 'review',
  pending_approval: 'review',
  rejected: 'muted',
  failed: 'fail',
}
// What each outcome MEANT for the file — the column an auditor is actually reading for. Kept
// beside the raw result rather than replacing it, since the stored value is what the record says.
const AUDIT_MEANING = {
  applied: 'carried out',
  approved: 'decision recorded · file not touched',
  pending_approval: 'awaiting a decision',
  rejected: 'declined · file not touched',
  failed: 'attempted and did not complete',
}

function AuditTrail({ audit, source }) {
  if (audit === null) return <p className="muted" style={{ fontSize: 13 }}>Loading…</p>
  if (audit === 'error') {
    return (
      <p style={{ fontSize: 13, color: TONE.fail[0] }}>
        Could not load the lifecycle trail. The record itself is unaffected — it is append-only
        and nothing here writes to it.
      </p>
    )
  }
  const mine = audit.filter((a) => matchesThisSource(a, source))
  const elsewhere = audit.length - mine.length
  if (!mine.length) {
    return (
      <p className="muted" style={{ fontSize: 13 }}>
        No lifecycle decisions recorded for this source.
        {elsewhere > 0 && ` ${elsewhere.toLocaleString()} recorded on other sources.`}
      </p>
    )
  }
  return (
    <>
      <div className="findings">
        {mine.slice(0, 50).map((a) => (
          <div key={a.id} style={{ padding: '7px 0', borderBottom: '1px solid var(--line)' }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <span className="muted" style={{ fontSize: 12 }}>{a.ts ? fmtWhen(a.ts) : NOT_AVAILABLE}</span>
              <Pill tone={AUDIT_TONE[a.result] || 'muted'}>{orAbsent(a.result)}</Pill>
              <span style={{ fontSize: 12.5, flex: 1, minWidth: 0 }}>
                {a.document_exists === false
                  ? <span className="muted">document no longer exists ({orAbsent(a.doc_id)})</span>
                  : orAbsent(a.path || a.doc_id)}
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {orAbsent(a.action)} · {AUDIT_MEANING[a.result] || orAbsent(a.detail, '')}
              {' · rule: '}
              {/* A deleted rule shows as absent rather than as its id — an id in the name slot
                  reads as a name. */}
              {a.policy_name ? <b>{a.policy_name}</b> : <span className="muted">no longer configured</span>}
            </div>
          </div>
        ))}
      </div>
      {mine.length > 50 && (
        <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          Showing the 50 most recent of {mine.length.toLocaleString()}.
        </p>
      )}
      {elsewhere > 0 && (
        <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          {elsewhere.toLocaleString()} more recorded on other sources.
        </p>
      )}
    </>
  )
}

// A rule's match count, split by boundary. The preview endpoint evaluates over the WHOLE
// documents table, so "284 files" under a per-source heading would be a true number placed
// where it says something false. Both halves are shown, each labelled.
function RuleMatches({ source, policy, preview, open, onToggle }) {
  const label = source.name || source.type || 'this source'
  if (preview === undefined) {
    return <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Counting matches…</div>
  }
  if (preview === 'error') {
    return (
      <div style={{ fontSize: 12, marginTop: 4, color: TONE.fail[0] }}>
        Could not count matches — the rule is still active; only this count is missing.
      </div>
    )
  }
  const { forSource, total, splitKnown } = matchCounts(preview, source)
  const rows = matchedFilesForSource(preview, source)
  return (
    <div style={{ marginTop: 5 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap', fontSize: 12 }}>
        <span>
          {splitKnown
            ? <><b>{forSource.toLocaleString()}</b> matched in {label}</>
            : <span className="muted">Match count for {label} {NOT_AVAILABLE.toLowerCase()}</span>}
        </span>
        <span className="muted">· {total.toLocaleString()} across all sources</span>
        {splitKnown && forSource > 0 && (
          <button className="linklike" style={{ fontSize: 12 }} onClick={onToggle}>
            {open ? 'Hide matches' : 'View matches'}
          </button>
        )}
      </div>
      {open && (
        <div className="findings" style={{ marginTop: 5 }}>
          {rows.slice(0, 25).map((d) => (
            <div key={d.doc_id} className="filelistrow" style={{ cursor: 'default' }}>
              <span className="fname" style={{ fontSize: 12.5, flex: 1, minWidth: 0 }}>
                {orAbsent(d.path || d.doc_id)}
              </span>
              <span className="muted" style={{ fontSize: 11.5 }}>{orAbsent(d.department, '')}</span>
            </div>
          ))}
          {rows.length > 25 && (
            <div className="muted" style={{ fontSize: 11.5, padding: '4px 0' }}>
              +{(rows.length - 25).toLocaleString()} more not listed
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// The two queues that need a human before anything moves. Counts come from the disposition
// partition, so they are this source's own files and agree with Overview by construction.
function ReviewQueues({ rows, files, labelOf, onPickFile }) {
  const [open, setOpen] = useState(null)
  const count = (k) => rows.find((r) => r.key === k)?.count || 0
  const QUEUES = [
    ['archive', 'Archive candidates', 'Reversible — the file is moved, not removed.'],
    ['delete', 'Deletion review', 'An approval is recorded before anything is trashed, and the trash is recoverable.'],
  ]
  const any = QUEUES.some(([k]) => count(k) > 0)
  if (!any) {
    return <p className="muted" style={{ fontSize: 13 }}>Nothing is awaiting review from this source.</p>
  }
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      {QUEUES.filter(([k]) => count(k) > 0).map(([k, title, why]) => {
        const n = count(k)
        const listed = files.filter((f) => dispositionOf(f, labelOf) === k)
        return (
          <div key={k} style={{ background: TONE.review[1], borderRadius: 9, padding: '9px 11px' }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: TONE.review[0] }}>
                {n.toLocaleString()} {title}
              </span>
              <button className="linklike" style={{ fontSize: 12 }}
                      onClick={() => setOpen(open === k ? null : k)}>
                {open === k ? 'Hide' : 'Review'}
              </button>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{why}</div>
            {open === k && (
              <div className="findings" style={{ marginTop: 6 }}>
                {listed.slice(0, 25).map((f) => (
                  <button className="filelistrow" key={f.file} onClick={() => onPickFile && onPickFile(f)}>
                    <span className="fname" style={{ fontSize: 12.5, flex: 1, minWidth: 0 }}>{f.file}</span>
                    <span className="muted" style={{ fontSize: 11.5 }}>{orAbsent(f.modifiedAge, '')}</span>
                  </button>
                ))}
                {listed.length > 25 && (
                  <div className="muted" style={{ fontSize: 11.5, padding: '4px 0' }}>
                    +{(listed.length - 25).toLocaleString()} more not listed
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// Folders ACP actually saw. A source that reports bare filenames has no folder structure to
// show — that is stated, not rendered as "0 locations", which reads as a scope problem.
function LocationList({ files }) {
  const folders = [...new Set(files.map(folderOf).filter(Boolean))].sort()
  if (!folders.length) {
    return (
      <p className="muted" style={{ fontSize: 13 }}>
        {NOT_AVAILABLE} — this source reports flat file names, so ACP has no folder structure to list.
        The discovery boundary above is what limits what it reads.
      </p>
    )
  }
  return (
    <ul style={{ margin: 0, padding: 0, listStyle: 'none', fontSize: 13 }}>
      {folders.slice(0, 40).map((f) => (
        <li key={f} style={{ padding: '4px 0', borderBottom: '1px solid var(--line)' }}>
          <span style={{ color: TONE.ok[0] }}>✓</span> /{f}
        </li>
      ))}
      {folders.length > 40 && <li className="muted" style={{ padding: '4px 0' }}>+{folders.length - 40} more</li>}
    </ul>
  )
}

// The three reasons a file is not in the assessable set, kept apart on purpose.
function UncoveredList({ files, labelOf, latest }) {
  const excluded = files.filter((f) => dispositionOf(f, labelOf) === 'excluded').length
  const unreadable = files.filter((f) => dispositionOf(f, labelOf) === 'unreadable').length
  const runErr = latest && (latest.error || 0) > 0 ? latest.error : 0
  const items = [
    ['Excluded by a configured rule', excluded, 'muted',
      'ACP was told not to inventory these.'],
    ['Inaccessible — permission denied', unreadable, 'fail',
      'The source refused access, or the file could not be opened with the credentials ACP holds.'],
    ['Failed during the last discovery run', runErr, 'fail',
      'Read errors reported by the most recent run.'],
  ].filter(([, n]) => n > 0)
  if (!items.length) return <p className="muted" style={{ fontSize: 13 }}>Everything in scope was read.</p>
  return (
    <div style={{ display: 'grid', gap: 6 }}>
      {items.map(([label, n, tone, why]) => (
        <div key={label} style={{ fontSize: 13 }}>
          <Pill tone={tone}>{Number(n).toLocaleString()}</Pill>{' '}
          <b style={{ fontWeight: 600 }}>{label}</b>
          <div className="muted" style={{ fontSize: 12 }}>{why}</div>
        </div>
      ))}
    </div>
  )
}

function UnreadableFiles({ files, labelOf, onPickFile }) {
  const stuck = files.filter((f) => dispositionOf(f, labelOf) === 'unreadable').slice(0, 12)
  if (!stuck.length) return <p className="muted" style={{ fontSize: 13 }}>Every discovered file was readable.</p>
  return (
    <div className="findings">
      {stuck.map((f) => (
        <button className="filelistrow" key={f.file} onClick={() => onPickFile(f)}>
          <span className="fname" style={{ fontSize: 13, flex: 1, minWidth: 0 }}>{f.file}</span>
          <span className="muted" style={{ fontSize: 12 }}>{orAbsent(f.openIssue, 'could not open')}</span>
        </button>
      ))}
    </div>
  )
}
