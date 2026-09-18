import { useState, useRef, useEffect } from 'react'
import { resetDemoData, resetMyData, getAllowlist, setAllowlist, inviteTester, getSettings, updateSettings, getAiCosts, getAiProviders, putAiProvider, putAiProviderSecret, testAiProvider, getSecondOpinionPolicy, putSecondOpinionPolicy, getRemediationPilot, putRemediationPilot, getAiStatus, getAdmins, setAdmins, getMe, getToken, getCapacitySchedule, validateCapacitySchedule, putCapacitySchedule, getMyScope, putMyReleaseTimezone } from './api.js'
import { SIM } from './sim.js'
import WorkerReplicaControl from './WorkerReplicaControl.jsx'
import ReviewMemory from './ReviewMemory.jsx'
import RuleExplanations from './RuleExplanations.jsx'
import CapacitySchedule from './CapacitySchedule.jsx'
import PeopleAccess from './PeopleAccess.jsx'
import WorkspaceRoles from './WorkspaceRoles.jsx'
import { handleWorkflowTabKeyDown } from './workflowTabs.js'

// What a write is allowed to claim when the API layer marked its own answer `simulated`.
// A simulated response never reached a server, so it is neither a success nor a failure — the
// request was never made — and it must not borrow the vocabulary of either.
const SIM_NOT_WRITTEN =
  'SIM — nothing was written. This demo build has no backend, so the change is local to this browser '
  + 'tab and the platform still holds its previous value. Use a build served by the real API to change it.'
// One place that decides what a settings write may report, so a new caller cannot forget the flag.
const wrote = (s, ok) => (s?.simulated ? SIM_NOT_WRITTEN : ok)
// Three tones, not two. `msg.startsWith('✓') ? green : red` had no room for "the request was never
// made": amber says it plainly without dressing a demo build up as a platform error.
const msgColor = (m) => (m.startsWith('✓') ? 'var(--success-fg)' : m.startsWith('SIM') ? '#6B4A0B' : 'var(--error-fg-strong)')

// Danger zone — wipe scan results (Grafana + in-app charts) and/or Langfuse
// traces so the dashboards start fresh. Settings are preserved. Typed-confirm.
//
// EXPORTED, but no longer surfaced as a Settings tab (the panel is scoped to access management —
// Owners + Users). Kept in the module so the feature and its tests survive; re-add a tab in
// Settings() to bring it back. Same for DriveMirror and AIProvidersPanel below.
export function ResetData() {
  const [scope, setScope] = useState('all')
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState('')
  const SCOPES = [
    ['all', 'Both', 'Clear scan results and delete Langfuse traces — full clean slate.'],
    ['grafana', 'Grafana / charts only', 'Clear the scan-results tables. Langfuse traces are kept.'],
    ['langfuse', 'Langfuse only', 'Delete the project’s traces. Scan history & charts are kept.'],
  ]
  const run = () => {
    setBusy(true); setErr(''); setResult(null)
    resetDemoData(scope)
      .then((d) => { setResult(d); setTyped('') })
      .catch((e) => setErr(e.message || 'reset failed'))
      .finally(() => setBusy(false))
  }
  return (
    <div style={{ maxWidth: 560 }}>
      <h3 style={{ marginTop: 0 }}>Reset demo data</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Wipes <strong>all customer data</strong> for a completely fresh app: scan results,
        findings, per-file decisions, inventory, disposition audit, and the learned review
        memory — plus every remediated file, cached original and preview in blob storage, and
        Langfuse traces. Nothing from a prior customer is left behind.
        <strong> Your configuration is preserved</strong> — worker count, AI mode, schedule,
        rubric, and remediation programs. This cannot be undone.
      </p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, margin: '14px 0' }}>
        {SCOPES.map(([v, label, desc]) => (
          <label key={v} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', cursor: 'pointer' }}>
            <input type="radio" name="rscope" checked={scope === v} onChange={() => setScope(v)} style={{ marginTop: 3 }} />
            <span><b>{label}</b><br /><span className="muted" style={{ fontSize: 12 }}>{desc}</span></span>
          </label>
        ))}
      </div>
      <label style={{ fontSize: 13 }}>Type <code>RESET</code> to confirm:
        <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="RESET"
               style={{ marginLeft: 8, padding: '4px 8px', border: '1px solid var(--line)', borderRadius: 6 }} />
      </label>
      <div style={{ marginTop: 14 }}>
        <button onClick={run} disabled={busy || typed !== 'RESET'}
                style={{ background: typed === 'RESET' ? 'var(--error-fg-strong)' : '#ccc', color: '#fff', border: 'none',
                         borderRadius: 8, padding: '8px 16px', cursor: typed === 'RESET' ? 'pointer' : 'not-allowed', fontWeight: 600 }}>
          {busy ? 'Resetting…' : 'Reset data'}
        </button>
      </div>
      {result && (
        <p style={{ marginTop: 12, fontSize: 13, color: 'var(--success-fg)' }}>
          ✓ Reset done — cleared {result.cleared_tables?.length || 0} table(s)
          {result.scope !== 'grafana' && `, deleted ${result.langfuse_traces_deleted} Langfuse trace(s)`}.
          {result.scope !== 'grafana' && result.langfuse_traces_deleted === 0 &&
            ' (No traces deleted — if Langfuse still shows data, clear it from its UI / retention settings.)'}
        </p>
      )}
      {err && <p style={{ marginTop: 12, fontSize: 13, color: 'var(--error-fg-strong)' }}>⚠ {err}</p>}
    </div>
  )
}

// Self-service sibling of ResetData above: clears only the SIGNED-IN USER'S OWN scans, so two
// people testing concurrently never clear each other's work — no admin role needed, no scope
// choice (it's always "everything of mine"). Confirmation is required before deleting personal records.
export function ResetMyData() {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState('')
  const run = () => {
    if (!window.confirm('Reset your data? This permanently clears your scans, findings, decisions, comments, and fix records. Source files and delivered copies remain. Other users’ data will not change.')) return
    setBusy(true); setErr(''); setResult(null)
    resetMyData()
      .then((d) => { setResult(d) })
      .catch((e) => setErr(e.message || 'reset failed'))
      .finally(() => setBusy(false))
  }
  return (
    <div style={{ maxWidth: 560 }}>
      <h3 style={{ marginTop: 0 }}>Reset my data</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Wipes <strong>your own</strong> scans and everything tied to them — findings, decisions,
        review comments, applied fixes — so you can test with a clean slate. Other signed-in
        users' scans are untouched. Does <strong>not</strong> delete files from SharePoint /
        OneDrive / Drive, and does not remove already-written remediated copies from storage.
        This cannot be undone.
      </p>
      <div style={{ marginTop: 14 }}>
        <button onClick={run} disabled={busy}
                style={{ background: 'var(--error-fg-strong)', color: '#fff', border: 'none',
                         borderRadius: 8, padding: '8px 16px', cursor: busy ? 'wait' : 'pointer', fontWeight: 600 }}>
          {busy ? 'Resetting…' : 'Reset my data'}
        </button>
      </div>
      {result && (
        <p style={{ marginTop: 12, fontSize: 13, color: 'var(--success-fg)' }}>
          ✓ Reset done — cleared {result.cleared_tables?.length || 0} table(s) for {result.owner}.
        </p>
      )}
      {err && <p style={{ marginTop: 12, fontSize: 13, color: 'var(--error-fg-strong)' }}>⚠ {err}</p>}
    </div>
  )
}

// ADR 0010: Blob is always the primary, must-succeed write for a remediated file.
// This controls whether it's ALSO auto-mirrored to Drive right after, and which
// Drive folder that mirror lands in.
export function DriveMirror() {
  const [settings, setSettings] = useState(null)
  const [folder, setFolder] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  useEffect(() => {
    getSettings().then((s) => { setSettings(s); setFolder(s.drive_mirror_folder || 'Remediated') }).catch(() => {})
  }, [])
  const toggle = () => {
    if (!settings || busy) return
    setBusy(true); setMsg('')
    // The checkbox moving is not evidence of a write — under SIM it moves either way, because the
    // store it reflects is this browser tab. `wrote(s, '')` keeps the real build silent as before
    // and makes the demo build say which one just happened.
    updateSettings({ drive_mirror_enabled: !settings.drive_mirror_enabled })
      .then((s) => { setSettings(s); setMsg(wrote(s, '')) })
      .catch((e) => setMsg(e.message || 'update failed'))
      .finally(() => setBusy(false))
  }
  const [aiUrl, setAiUrl] = useState('')
  const [aiVision, setAiVision] = useState('')
  useEffect(() => { if (settings) { setAiUrl(settings.ai_base_url || ''); setAiVision(settings.ai_vision_model || '') } }, [settings])
  // Whether this signed-in user may actually write platform settings. PUT /settings is
  // owner-only (ACP_OWNER_EMAIL); the SPA gates Settings behind Platform Admin, so start
  // optimistic and let the /ai/providers probe below correct it on a 403.
  const [canEdit, setCanEdit] = useState(true)
  // Why vision is off, straight from the server. A pinned model name the endpoint has never had
  // is otherwise invisible in this panel, and this is the field that fixes it. Re-read after every
  // apply, since clearing the override is exactly what changes the answer.
  const [aiStatus, setAiStatus] = useState(null)
  const loadAiStatus = () => getAiStatus().then(setAiStatus).catch(() => {})
  useEffect(() => { loadAiStatus() }, [])
  // The endpoint save must never leave the form asserting something the server did not store.
  // A failed PUT used to leave the typed value on screen with the old value still live in
  // production, and the only notice was a line at the very bottom of the panel — below the
  // whole providers section, off-screen from the Apply button that caused it.
  //
  // `{ clearBoth: true }` is the detach path below. Tested as an option object rather than
  // positional args because this is also the onClick handler, and a React MouseEvent arriving
  // as the first parameter must not read as "clear the fields".
  const saveEndpoint = (opts) => {
    const clearBoth = opts?.clearBoth === true
    const want = clearBoth
      ? { ai_base_url: '', ai_vision_model: '' }
      : { ai_base_url: aiUrl.trim(), ai_vision_model: aiVision.trim() }
    const ok = clearBoth
      ? '✓ back to the deploy default endpoint and vision model'
      : '✓ endpoint switched — takes effect on every replica within ~30s, no restart'
    setBusy(true); setMsg('')
    updateSettings(want)
      .then((s) => {
        setSettings(s)
        // Trust the response, not the request: report what the server actually kept. A 200
        // whose body disagrees with what we sent is a silent no-op otherwise.
        //
        // `simulated` is checked FIRST because the drift test structurally cannot catch it: SIM
        // builds its response out of the request, so the two always agree and `drift` is always
        // empty. Comparing a response to a request only detects a no-op when something other than
        // this client authored the response.
        const drift = Object.keys(want).filter((k) => (s?.[k] ?? '') !== want[k])
        setMsg(s?.simulated ? SIM_NOT_WRITTEN
          : drift.length
          ? `⚠ the server kept ${drift.map((k) => `${k}="${s?.[k] ?? ''}"`).join(', ')} — your value was not applied`
          : ok)
        return loadAiStatus()
      })
      .catch((e) => {
        setAiUrl(settings.ai_base_url || ''); setAiVision(settings.ai_vision_model || '')
        setMsg(`⚠ not saved: ${e.message || 'update failed'} — the fields have been restored to the values the server holds`)
      })
      .finally(() => setBusy(false))
  }
  // Detaching a burst GPU means clearing BOTH fields (deploy/gpu/README.md). Doing it by hand is
  // where this came from on 2026-07-29: the URL was cleared, the model name was not, and the
  // orphaned name shadowed the deploy default until someone read the precedence code.
  const useDeployDefault = () => saveEndpoint({ clearBoth: true })
  const [costs, setCosts] = useState(null)
  useEffect(() => { getAiCosts().then(setCosts).catch(() => {}) }, [])
  const toggleAutoApply = () => {
    setBusy(true); setMsg('')
    updateSettings({ auto_apply_validated: !settings.auto_apply_validated })
      .then((s) => { setSettings(s); setMsg(wrote(s, '')) })
      .catch((e) => setMsg(e.message || 'update failed'))
      .finally(() => setBusy(false))
  }
  const saveFolder = () => {
    setBusy(true); setMsg('')
    updateSettings({ drive_mirror_folder: folder })
      .then((s) => { setSettings(s); setMsg(wrote(s, '✓ saved')) })
      .catch((e) => setMsg(e.message || 'update failed'))
      .finally(() => setBusy(false))
  }
  if (!settings) return <p className="muted">Loading…</p>
  const Roll = ({ label, r }) => (
    <div style={{ flex: 1, minWidth: 150, border: '1px solid var(--line)', borderRadius: 8, padding: '10px 12px' }}>
      <div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: .3 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700 }}>${(r?.cost_usd ?? 0).toFixed(2)}</div>
      <div className="muted" style={{ fontSize: 12 }}>{(r?.calls ?? 0).toLocaleString()} AI call{(r?.calls === 1) ? '' : 's'}{r?.avg_latency_ms ? ` · ${r.avg_latency_ms}ms avg` : ''}</div>
    </div>
  )
  const models = costs?.month?.by_model || []
  const rollout = costs?.shadow_rollout
  const rolloutRows = rollout?.rows || []
  const pilotRows = rolloutRows.filter((r) => r.verdict === 'enable')
  const rolloutLabel = {
    enable: 'Assisted pilot',
    'keep-human-only': 'Keep human-only',
    'insufficient-evidence': 'More evidence needed',
    'no-change-rule-code': 'Keep rule code',
  }
  // Reviewer decisions and post-write validation, joined to the call by the id each decision
  // carries. A model with calls but no linked decision reads "Not linked" — never a rate
  // computed over drafts nobody reviewed or over decisions that did not name their call.
  const reviewedCell = (r) => {
    if (!r || !Number(r.decisions)) return 'Not linked'
    const accepted = Number(r.approved || 0) + Number(r.edited || 0)
    return `${accepted} accepted (${Number(r.edited || 0)} edited) · ${Number(r.rejected || 0)} rejected`
  }
  const validationCell = (v) => {
    if (!v || !Number(v.validated)) return 'Not linked'
    const parts = [`${Number(v.cleared || 0)} cleared`]
    if (Number(v.regressed)) parts.push(`${v.regressed} regressed`)
    if (Number(v.still_failing)) parts.push(`${v.still_failing} still failing`)
    if (Number(v.could_not_verify)) parts.push(`${v.could_not_verify} unverified`)
    if (Number(v.unresolved)) parts.push(`${v.unresolved} not written`)
    return parts.join(' · ')
  }
  return (
    <div style={{ maxWidth: 560 }}>
      <h3 style={{ marginTop: 0 }}>AI usage &amp; cost <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>· governance</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Real per-call provenance, summed — never an estimate (ADR 0016). For this keyless
        local-AI build every external AI cost is a genuine <b>$0.00</b>: no per-token billing,
        and no document bytes leave your network. A governed cloud provider records its real
        cost and it appears here.
      </p>
      {costs && (
        <>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', margin: '10px 0' }}>
            <Roll label="Today" r={costs.today} />
            <Roll label="Last 30 days" r={costs.month} />
            <Roll label="All time" r={costs.all_time} />
          </div>
          {costs.all_time?.by_zone?.length > 0 && (
            <div className="muted" style={{ fontSize: 12 }}>
              Processing zone: {costs.all_time.by_zone.map((z) => `${z.key} (${z.calls})`).join(' · ')}
              {costs.all_time.by_zone.every((z) => z.key === 'local') && ' — nothing left your network 🟢'}
            </div>
          )}
          <section aria-labelledby="model-quality-title" style={{ marginTop: 16 }}>
            <h4 id="model-quality-title" style={{ margin: '0 0 4px' }}>Remediation model evidence</h4>
            <p className="muted" style={{ fontSize: 12, margin: '0 0 8px' }}>
              Last 30 days · measured calls only. Success means the model call completed; it does
              not mean a reviewer accepted the draft or the corrected file passed validation —
              those are the two columns on the right, counted from the decisions and re-scans
              that name this exact call.
            </p>
            {models.length ? (
              <div style={{ overflowX: 'auto' }}>
                <table className="simple-table" style={{ width: '100%', fontSize: 12 }}>
                  <thead><tr><th>Model</th><th>Location</th><th>Calls</th><th>Call success</th><th>Avg latency</th><th>Spend</th><th>Reviewer decisions</th><th>Post-write validation</th></tr></thead>
                  <tbody>{models.map((m) => {
                    const success = m.calls ? Math.round((Number(m.ok || 0) / Number(m.calls)) * 100) : null
                    return <tr key={`${m.provider}:${m.model}:${m.zone}`}>
                      <td><b>{m.model || 'Not reported'}</b><br /><span className="muted">{m.provider || 'Provider not reported'}</span></td>
                      <td>{m.zone || 'Not reported'}</td>
                      <td>{Number(m.calls || 0).toLocaleString()}</td>
                      <td>{success == null ? 'Not measured' : `${success}%`}{m.failed ? ` · ${m.failed} failed` : ''}</td>
                      <td>{m.avg_latency_ms ? `${Number(m.avg_latency_ms).toLocaleString()} ms` : 'Not measured'}</td>
                      <td>${Number(m.cost_usd || 0).toFixed(4)}</td>
                      <td>{reviewedCell(m.reviewed)}</td>
                      <td>{validationCell(m.validation)}</td>
                    </tr>
                  })}</tbody>
                </table>
              </div>
            ) : <p className="muted" style={{ fontSize: 12 }}>No model calls recorded in this window.</p>}
            <p className="muted" style={{ fontSize: 11.5, margin: '8px 0 0' }}>
              Reviewer decisions and post-write validation count only the drafts whose decision
              recorded the exact model call. Drafts reviewed before that link existed, human-authored
              values and multi-image cards read as not linked rather than estimated. "Regressed" means
              the write cleared its criterion but made another one fail that did not fail before;
              "not written" means the approved content could no longer be found in the document.
            </p>
          </section>
          {rollout && (
            <section aria-labelledby="shadow-rollout-title" style={{ marginTop: 18, borderTop: '1px solid var(--line)', paddingTop: 16 }}>
              <h4 id="shadow-rollout-title" style={{ margin: '0 0 4px' }}>Stronger-model rollout gates</h4>
              <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
                Two independent shadow runs against the same governed corpus. These recommendations
                can enable an assisted pilot only—every model draft still requires human approval.
              </p>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
                {Object.entries(rollout.summary_categories || {}).map(([verdict, count]) => (
                  <div key={verdict} style={{ border: '1px solid var(--line)', borderRadius: 8, padding: '7px 10px', minWidth: 112 }}>
                    <b style={{ fontSize: 17 }}>{count}</b><br />
                    <span className="muted" style={{ fontSize: 11 }}>{rolloutLabel[verdict] || verdict}</span>
                  </div>
                ))}
              </div>
              {pilotRows.length > 0 && <div style={{ overflowX: 'auto' }}>
                <table className="simple-table" style={{ width: '100%', fontSize: 12 }}>
                  <thead><tr><th>Criterion</th><th>Format</th><th>Recommended model</th><th>Evidence</th><th>Decision</th></tr></thead>
                  <tbody>{pilotRows.map((r) => {
                    const model = (r.enable_candidate || '').replace(/^anthropic:/, '')
                    const candidate = r.candidates?.find((c) => c.candidate === r.enable_candidate)
                    return <tr key={r.category}>
                      <td><b>{r.criterion}</b></td><td>{String(r.format || '').toUpperCase()}</td>
                      <td>{model || 'Not reported'}</td>
                      <td>{candidate ? `${candidate.safe_runs}/${candidate.runs} safe runs · ${r.cases} cases` : `${r.cases} cases`}</td>
                      <td>Assisted pilot—human approval required</td>
                    </tr>
                  })}</tbody>
                </table>
              </div>}
              <details style={{ marginTop: 9 }}>
                <summary style={{ cursor: 'pointer', fontSize: 12, fontWeight: 600 }}>Review all {rolloutRows.length} criterion-format decisions</summary>
                <div style={{ overflowX: 'auto', maxHeight: 320, marginTop: 8 }}>
                  <table className="simple-table" style={{ width: '100%', fontSize: 11.5 }}>
                    <thead><tr><th>Criterion</th><th>Format</th><th>Current lane</th><th>Gate</th><th>Why</th></tr></thead>
                    <tbody>{rolloutRows.map((r) => <tr key={r.category}>
                      <td>{r.criterion}</td><td>{String(r.format || '').toUpperCase()}</td><td>{r.current_lane}</td>
                      <td>{rolloutLabel[r.verdict] || r.verdict}</td><td>{r.why}</td>
                    </tr>)}</tbody>
                  </table>
                </div>
              </details>
              <p className="muted" style={{ fontSize: 11.5, margin: '8px 0 0' }}>
                The gate is evidence, not a deployment switch. Rule-code wins remain unchanged at
                zero model cost; inconsistent or under-sampled categories stay off.
              </p>
            </section>
          )}
        </>
      )}
      <hr style={{ border: 0, borderTop: '1px solid var(--line)', margin: '20px 0' }} />
      <h3 style={{ marginTop: 0 }}>Remediated-file storage</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        A remediated file's fixed copy is always written to Azure Blob first — the durable,
        must-succeed copy (ADR 0010). This controls whether it's <b>also</b> mirrored to Google
        Drive automatically right after, and which folder that mirror lands in.
      </p>
      <label style={{ display: 'flex', gap: 10, alignItems: 'center', cursor: busy ? 'default' : 'pointer', margin: '16px 0' }}>
        <input type="checkbox" checked={settings.drive_mirror_enabled} onChange={toggle} disabled={busy || !canEdit} />
        <span>
          <b>Auto-mirror to Drive</b><br />
          <span className="muted" style={{ fontSize: 12 }}>
            {settings.drive_mirror_enabled
              ? 'On — every successful Blob remediation also tries to write a copy to Drive.'
              : 'Off — remediated files stay Blob-only. No automatic Drive write.'}
          </span>
        </span>
      </label>
      <label style={{ fontSize: 13, display: 'block' }}>Drive folder name
        <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
          <input value={folder} onChange={(e) => setFolder(e.target.value)} placeholder="Remediated"
                 disabled={busy || !canEdit || !settings.drive_mirror_enabled}
                 style={{ padding: '4px 8px', border: '1px solid var(--line)', borderRadius: 6, flex: 1 }} />
          <button className="ghost small" onClick={saveFolder}
                  disabled={busy || !canEdit || !folder.trim() || !settings.drive_mirror_enabled || folder.trim() === settings.drive_mirror_folder}>
            Save
          </button>
        </div>
        <span className="muted" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
          Files already mirrored under a previous folder name aren't moved — only new remediations use the updated folder.
        </span>
      </label>
      <h3>AI draft auto-apply</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        A vision draft grounded in the image's own text always auto-applies. This controls the
        NEXT tier: an ungrounded draft that an <b>independent second AI reading</b> confirms
        (consistency cross-check — a measurement, never the model grading itself). Drafts the
        cross-check does not confirm always queue for one-click human approval, and every
        applied fix is still verified by re-scan before anything is certified.
      </p>
      <label style={{ display: 'flex', gap: 10, alignItems: 'center', cursor: busy ? 'default' : 'pointer', margin: '16px 0' }}>
        <input type="checkbox" checked={!!settings.auto_apply_validated} onChange={toggleAutoApply} disabled={busy || !canEdit} />
        <span>
          <b>Auto-apply cross-checked drafts</b><br />
          <span className="muted" style={{ fontSize: 12 }}>
            {settings.auto_apply_validated
              ? 'On — a draft confirmed by an independent second reading is applied without waiting for review (provenance says so on the fix).'
              : 'Off — every ungrounded draft waits for one-click human approval (the default).'}
          </span>
        </span>
      </label>
      <h3>AI endpoint <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>· GPU burst without a restart</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Point the platform at a different Ollama endpoint (e.g. a burst GPU pod) at runtime —
        it takes effect on every replica within ~30 seconds, with <b>no container restart</b>,
        so running scans are never disturbed. Empty = the deploy's default. Every switch is
        audited, and the 🟢 local / 🟡 cloud provenance badge follows the endpoint truthfully.
      </p>
      {!canEdit && <ReadOnlyNotice />}
      <label style={{ fontSize: 13, display: 'block' }}>Ollama base URL
        <input value={aiUrl} onChange={(e) => setAiUrl(e.target.value)} disabled={busy || !canEdit}
               placeholder="empty = deploy default"
               style={{ display: 'block', width: '100%', padding: '4px 8px', margin: '6px 0 10px', border: '1px solid var(--line)', borderRadius: 6 }} />
      </label>
      <label style={{ fontSize: 13, display: 'block' }}>Vision model
        <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
          <input value={aiVision} onChange={(e) => setAiVision(e.target.value)} disabled={busy || !canEdit}
                 placeholder="empty = deploy default (e.g. moondream on CPU, llava:13b on a GPU)"
                 style={{ padding: '4px 8px', border: '1px solid var(--line)', borderRadius: 6, flex: 1 }} />
          <button className="ghost small" onClick={saveEndpoint} disabled={busy || !canEdit}>Apply</button>
        </div>
      </label>
      {/* `=== false` on purpose, and the reason is NOT part of the gate. Both halves were wrong:
          a truthy test treats an endpoint that never reported the field as "vision is fine", and
          requiring the reason meant a server that said false without explaining itself rendered
          nothing at all. The warning follows the verdict; the reason only refines it. */}
      {aiStatus?.vision_available === false && (
        <p role="status" style={{ margin: '10px 0 0', fontSize: 13, color: 'var(--error-fg-strong)' }}>
          ⚠ <b>Genuine alt text is off</b>
          {aiStatus.vision_unavailable_reason ? ` — ${aiStatus.vision_unavailable_reason}` : ''}.
          Until this resolves, WCAG 1.1.1 findings get a fill-in template for a human to complete,
          not an image-derived description.
        </p>
      )}
      {canEdit && (aiUrl || aiVision) && (
        <button className="ghost small" style={{ marginTop: 10 }} onClick={useDeployDefault} disabled={busy}>
          Use deploy default (clears both)
        </button>
      )}
      {/* The outcome belongs next to the control that caused it. The copy at the foot of the
          panel is far below the providers section and was missed entirely. */}
      {msg && <p role="status" aria-live="polite"
                 style={{ margin: '10px 0 0', fontSize: 13, color: msgColor(msg) }}>{msg}</p>}
      <hr style={{ border: 0, borderTop: '1px solid var(--line)', margin: '20px 0' }} />
      <AIProvidersPanel onAccess={setCanEdit} />
      {msg && <p style={{ marginTop: 12, fontSize: 13, color: msgColor(msg) }}>{msg}</p>}
    </div>
  )
}
// Rubric / ControlPlane / Disposition / FileTypeConfig live in their own files and are no longer
// imported here: their tabs were removed (this panel is now access-only). Re-import + re-add a tab
// to surface one again — none of those files were deleted.
import OwnerDelegate from './OwnerDelegate.jsx'
import MyScanScope from './MyScanScope.jsx'
import { useDialog } from './a11y.js'

// Platform settings, behind the header cog — gated to the Platform Admin. Holds
// the scoring rules (Rubric), the validation coverage (WCAG 2.1 + 2.2 matrix), and
// the business ontology/taxonomy — i.e. the configuration an admin owns, kept out
// of the day-to-day workflow tabs.
export function AllowList() {
  const [emails, setEmails] = useState([])
  const [owner, setOwner] = useState('')
  const [domains, setDomains] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [loaded, setLoaded] = useState(false)
  // Guest-invite (ADR 0033) — hidden unless the backend reports the credential is configured.
  const [inviteEnabled, setInviteEnabled] = useState(false)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteBusy, setInviteBusy] = useState(false)
  const [inviteMsg, setInviteMsg] = useState('')
  // Google onboarding — no backend invite exists for Google (Drive access is per-user OAuth), so
  // "onboard" here is: whitelist the Gmail (persisted immediately, like the MS invite auto-adds),
  // then the guided OAuth-test-user + sign-in steps. Kept symmetric with the Microsoft card.
  const [gEmail, setGEmail] = useState('')
  const [gBusy, setGBusy] = useState(false)
  const [gMsg, setGMsg] = useState('')
  // Platform-admin management. `admins` = owner-managed set (promotable/demotable here); `envAdmins`
  // = permanent grants (ACP_ADMIN_EMAILS, set at deploy); `canManageAdmins` = this user is the owner
  // (only the owner may promote/demote — the API enforces it regardless of the UI).
  const [admins, setAdminsState] = useState([])
  const [envAdmins, setEnvAdmins] = useState([])
  const [canManageAdmins, setCanManageAdmins] = useState(false)

  useEffect(() => {
    getAllowlist()
      .then((d) => {
        setEmails(d.emails || []); setOwner(d.owner || ''); setDomains(d.domains || [])
        setInviteEnabled(!!d.invite_enabled)
      })
      .catch(() => setMsg('Could not load the test-user list.'))
      .finally(() => setLoaded(true))
    getAdmins()
      .then((d) => { setAdminsState(d.admins || []); setEnvAdmins(d.env_admins || []) })
      .catch(() => {})
    getMe().then((m) => { if (typeof m?.is_owner === 'boolean') setCanManageAdmins(m.is_owner) }).catch(() => {})
  }, [])

  // Promote/demote a test user to Platform Admin. Owner-only; optimistic-with-rollback so the
  // list can't assert a grant the server rejected (the PUT is owner-only and 403s otherwise).
  const toggleAdmin = (email) => {
    const next = admins.includes(email) ? admins.filter((a) => a !== email) : [...admins, email]
    const prev = admins
    setAdminsState(next)
    setAdmins(next).then((d) => setAdminsState(d.admins || next))
      .catch(() => { setAdminsState(prev); setMsg('Could not change admin — owner only.') })
  }

  const invite = () => {
    const e = inviteEmail.trim().toLowerCase()
    if (!e.includes('@')) { setInviteMsg('Enter a valid email.'); return }
    setInviteBusy(true); setInviteMsg('')
    inviteTester(e)
      .then((d) => {
        setEmails(d.emails || [])                 // reflect the auto-add to the list
        setInviteEmail('')
        setInviteMsg(`✓ Invited ${e} — a Microsoft invitation is on its way, and they’re now on the list.`)
      })
      .catch((err) => setInviteMsg(`Invite failed: ${err.message || err}`))
      .finally(() => setInviteBusy(false))
  }

  // Whitelist a Google tester and PERSIST in one step — the parallel to the Microsoft invite's
  // auto-add. The plain "+ Add user" below stages a change for the Save button; this commits it,
  // so onboarding one Google tester is a single action.
  const addGoogle = () => {
    const e = gEmail.trim().toLowerCase()
    if (!e.includes('@')) { setGMsg('Enter a valid email.'); return }
    if (emails.includes(e)) { setGMsg('Already on the list.'); setGEmail(''); return }
    const next = [...emails, e].sort()
    setGBusy(true); setGMsg('')
    setAllowlist(next)
      .then((d) => { setEmails(d.emails || next); setGEmail('')
                     setGMsg(`✓ Whitelisted ${e} — they sign in with Google and their Drive becomes a scannable source.`) })
      .catch((err) => setGMsg(`Could not save: ${err.message || err}`))
      .finally(() => setGBusy(false))
  }

  const add = () => {
    const e = input.trim().toLowerCase()
    if (!e.includes('@')) { setMsg('Enter a valid email.'); return }
    if (emails.includes(e)) { setMsg('Already added.'); setInput(''); return }
    setEmails((s) => [...s, e].sort()); setInput(''); setMsg('')
  }
  const remove = (e) => setEmails((s) => s.filter((x) => x !== e))
  const save = () => {
    setBusy(true); setMsg('')
    setAllowlist(emails)
      .then((d) => { setEmails(d.emails || []); setMsg('✓ Saved — applies on each user’s next sign-in.') })
      .catch((err) => setMsg(`Could not save: ${err.message || err}`))
      .finally(() => setBusy(false))
  }

  const avatar = (e) => (e.trim()[0] || '?').toUpperCase()
  const dirty = loaded   // Save is always available once loaded; cheap idempotent write

  return (
    <section style={{ maxWidth: 640 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
        <h3 style={{ margin: 0 }}>Users with access</h3>
        <span className="muted" style={{ fontSize: 13 }}>who can sign in &#38; scan</span>
      </div>

      {/* THE DOMAIN RULE, which this screen used to load and not render.
          `core.email_allowed()` admits three ways: the owner, this list, or ANY address at an
          allowed domain. The third was fetched into `domains` and never shown — so on a
          deployment with ACP_ALLOWED_DOMAINS set, an entire company could sign in while the
          screen that answers "who has access" listed a handful of names and looked complete.
          That is the failure this panel exists to prevent, on the panel itself. */}
      {domains.length > 0 && (
        <div role="note" style={{ marginTop: 12, padding: '12px 14px', borderRadius: 10, background: '#FBF1DF', border: '1px solid #EAD9BF' }}>
          <div style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--warn-fg)', marginBottom: 5 }}>
            ⚠ Anyone at {domains.length === 1 ? 'this domain' : 'these domains'} can sign in without being listed below
          </div>
          <div style={{ fontSize: 12.5, color: '#5C3D0B', lineHeight: 1.55 }}>
            {domains.map((d) => <b key={d} style={{ marginRight: 10 }}>@{d}</b>)}
            <div className="muted" style={{ marginTop: 4 }}>
              Set by <code>ACP_ALLOWED_DOMAINS</code> at deploy time — it cannot be changed here, and
              the people it admits do not appear in the list below.
            </div>
          </div>
        </div>
      )}

      {/* How access works — every gate email_allowed() actually checks, in its order */}
      <div style={{ marginTop: 12, padding: '12px 14px', borderRadius: 10, background: '#EEF2FB', border: '1px solid #D3DDF1' }}>
        <div style={{ fontSize: 12.5, fontWeight: 700, color: '#2B4A7E', marginBottom: 6 }}>What lets someone in:</div>
        <div style={{ fontSize: 12.5, color: '#33405C', lineHeight: 1.55 }}>
          <div><b>1.</b> They’re the <b>owner</b> <span className="muted">— set at deploy time, can never be removed.</span></div>
          <div><b>2.</b> They’re on this list <span className="muted">— manage it right here; applies on their next sign-in.</span></div>
          <div><b>3.</b> Their email is at an <b>allowed domain</b>{' '}
            <span className="muted">{domains.length > 0 ? '— see the notice above.' : '— none configured on this deployment.'}</span>
          </div>
          {/* Kept, but demoted: this is a Google OAuth prerequisite, not one of ACP's gates.
              Listing it as gate 2 of 2 implied the app checked it, and left the domain rule
              unmentioned entirely. */}
          <div className="muted" style={{ marginTop: 6 }}>
            Separately, until the app is Google-verified each person must also be a Google{' '}
            <b>test user</b> — added once in{' '}
            <a href="https://console.cloud.google.com/apis/credentials/consent" target="_blank" rel="noopener noreferrer" style={{ color: '#2B6CB0', fontWeight: 600 }}>Google Cloud → OAuth consent screen ↗</a>.
            That is a Google requirement, not an ACP one.
          </div>
        </div>
      </div>

      {/* Onboard a tester — two equal paths. Microsoft (SharePoint / OneDrive) and Google (Drive)
          both end at the same allowlist; each also carries the source-specific step that makes that
          person's own content scannable. The Microsoft card sends a real Entra B2B guest invite
          when configured (ADR 0033); otherwise it falls back to the guided manual invite. */}
      <div style={{ margin: '16px 0 0' }}>
        <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 8 }}>Onboard a tester</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: 10 }}>

          {/* Microsoft — SharePoint / OneDrive */}
          <div style={{ padding: '12px 14px', borderRadius: 10, background: 'var(--surface)', border: '1px solid var(--line)' }}>
            <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 4 }}>Microsoft <span className="muted" style={{ fontWeight: 400 }}>· SharePoint / OneDrive</span></div>
            {inviteEnabled ? (
              <>
                <div className="muted" style={{ fontSize: 12, marginBottom: 8, lineHeight: 1.5 }}>
                  Sends an Entra guest invitation and adds them to the list below in one step. They sign in
                  with their own Microsoft account — no tenant account is created.
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <input value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)}
                         onKeyDown={(e) => e.key === 'Enter' && invite()}
                         placeholder="tester@example.com" aria-label="Invite a Microsoft tester by email" type="email"
                         style={{ flex: 1, padding: '8px 11px', borderRadius: 8, border: '1px solid var(--line)', background: 'var(--surface)', color: 'inherit', fontSize: 14 }} />
                  <button onClick={invite} disabled={inviteBusy}>{inviteBusy ? 'Inviting…' : 'Invite'}</button>
                </div>
                {inviteMsg && <div role="status" aria-live="polite" className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>{inviteMsg}</div>}
              </>
            ) : (
              <div className="muted" style={{ fontSize: 12, lineHeight: 1.55 }}>
                One-click invite isn’t configured here (<code>ACP_INVITE_*</code> unset). To onboard a Microsoft
                tester, invite them as a guest in{' '}
                <a href="https://entra.microsoft.com/" target="_blank" rel="noopener noreferrer" style={{ color: '#2B6CB0', fontWeight: 600 }}>Entra admin center → Identity → Users → Invite external user ↗</a>,
                then add their email below. They can then scan the SharePoint / OneDrive sites they’re granted access to.
              </div>
            )}
          </div>

          {/* Google — Drive */}
          <div style={{ padding: '12px 14px', borderRadius: 10, background: 'var(--surface)', border: '1px solid var(--line)' }}>
            <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 4 }}>Google <span className="muted" style={{ fontWeight: 400 }}>· Drive</span></div>
            <div className="muted" style={{ fontSize: 12, marginBottom: 8, lineHeight: 1.5 }}>
              Whitelists a Gmail so they can sign in. After they sign in with Google, their own Drive becomes a
              scannable source — no copy is taken, ACP reads it read-only.
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input value={gEmail} onChange={(e) => setGEmail(e.target.value)}
                     onKeyDown={(e) => e.key === 'Enter' && addGoogle()}
                     placeholder="tester@gmail.com" aria-label="Whitelist a Google tester by email" type="email"
                     style={{ flex: 1, padding: '8px 11px', borderRadius: 8, border: '1px solid var(--line)', background: 'var(--surface)', color: 'inherit', fontSize: 14 }} />
              <button onClick={addGoogle} disabled={gBusy}>{gBusy ? 'Adding…' : 'Whitelist'}</button>
            </div>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8, lineHeight: 1.5 }}>
              Until the app is Google-verified, also add them once as a test user in{' '}
              <a href="https://console.cloud.google.com/apis/credentials/consent" target="_blank" rel="noopener noreferrer" style={{ color: '#2B6CB0', fontWeight: 600 }}>Google Cloud → OAuth consent screen ↗</a>.
            </div>
            {gMsg && <div role="status" aria-live="polite" className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>{gMsg}</div>}
          </div>

        </div>
      </div>

      {/* Add */}
      <div style={{ display: 'flex', gap: 8, margin: '14px 0 10px' }}>
        <input value={input} onChange={(e) => setInput(e.target.value)}
               onKeyDown={(e) => e.key === 'Enter' && add()}
               placeholder="name@gmail.com" aria-label="Add a test user email" type="email"
               style={{ flex: 1, padding: '8px 11px', borderRadius: 8, border: '1px solid var(--line)', background: 'var(--surface)', color: 'inherit', fontSize: 14 }} />
        <button onClick={add}>+ Add user</button>
      </div>

      {/* Editable list */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 7, marginBottom: 14 }}>
        {loaded && emails.length === 0 && (
          <div className="muted" style={{ fontSize: 13, padding: '14px', textAlign: 'center', border: '1px dashed var(--line)', borderRadius: 8 }}>
            No test users added yet — add a Gmail above to grant access.
          </div>
        )}
        {emails.map((e) => {
          const isOwner = e === owner
          const isEnvAdmin = envAdmins.includes(e)   // permanent, set at deploy — not editable here
          const isAdmin = isOwner || isEnvAdmin || admins.includes(e)
          const adminBadge = (label, title) => (
            <span title={title} style={{ fontSize: 11.5, fontWeight: 600, color: '#1D4ED8', background: '#E5EEF8', border: '1px solid #C7D7F0', borderRadius: 20, padding: '3px 9px', whiteSpace: 'nowrap' }}>🛡 {label}</span>
          )
          return (
            <div key={e} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '8px 11px', borderRadius: 9, background: 'var(--surface)', border: '1px solid var(--line)' }}>
              <span aria-hidden="true" style={{ width: 30, height: 30, borderRadius: '50%', background: isOwner ? 'var(--warn-fg)' : (isAdmin ? '#1D4ED8' : '#6D28D9'), color: '#fff', fontSize: 14, fontWeight: 700, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>{avatar(e)}</span>
              <span style={{ fontSize: 14, flex: 1, wordBreak: 'break-all' }}>{e}</span>
              {/* Admin badge: owner and env-admins are permanent; a promoted admin shows a plain badge. */}
              {!isOwner && isEnvAdmin && adminBadge('admin · set at deploy', 'Platform Admin via ACP_ADMIN_EMAILS — permanent, managed at deploy time')}
              {!isOwner && !isEnvAdmin && admins.includes(e) && adminBadge('admin', 'Platform Admin — full admin rights')}
              {/* Owner-only promote/demote, for ordinary test users (not the owner, not env-admins). */}
              {canManageAdmins && !isOwner && !isEnvAdmin && (
                <button className="ghost small" onClick={() => toggleAdmin(e)}
                        aria-label={`${admins.includes(e) ? 'Remove admin from' : 'Make admin'} ${e}`}>
                  {admins.includes(e) ? 'Remove admin' : 'Make admin'}
                </button>
              )}
              {isOwner
                ? <span title="The owner can’t be removed — anti-lockout safety" style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--warn-fg)', background: '#FBF1DF', border: '1px solid #EAD9BF', borderRadius: 20, padding: '3px 9px', whiteSpace: 'nowrap' }}>🔒 owner</span>
                : <button className="ghost small" onClick={() => remove(e)} aria-label={`Remove ${e}`}>Remove</button>}
            </div>
          )
        })}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <button onClick={save} disabled={busy || !dirty}>{busy ? 'Saving…' : 'Save changes'}</button>
        {msg && <span className="muted" role="status" aria-live="polite" style={{ fontSize: 13 }}>{msg}</span>}
      </div>

      {/* Always-allowed domains (read-only) */}
      {domains.length > 0 && (
        <div style={{ marginTop: 20, paddingTop: 14, borderTop: '1px solid var(--line)' }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: '#6B7280', marginBottom: 8 }}>🔒 ALWAYS ALLOWED <span style={{ fontWeight: 400 }}>· by domain, set at deploy</span></div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
            {domains.map((d) => (
              <span key={d} style={{ fontSize: 12.5, padding: '4px 10px', borderRadius: 20, background: '#EAF3EC', border: '1px solid #CFE5D6', color: '#2F6B43' }}>anyone @{d}</span>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

// Friendly labels for the provider catalogue.
const PROVIDER_LABELS = {
  azure_openai: 'Azure OpenAI', openai: 'OpenAI', anthropic: 'Anthropic',
  gemini: 'Google Gemini', bedrock: 'AWS Bedrock', huggingface: 'Hugging Face',
}
// Examples describe the adapter's expected fields; placeholders are never saved as values.
const PROVIDER_FIELDS = {
  azure_openai: { endpoint: 'https://your.openai.azure.com', deployment: 'your-deployment-name', model: 'gpt-4o', secret: 'AZURE_OPENAI_API_KEY' },
  openai: { endpoint: 'https://api.openai.com/v1', model: 'gpt-4o', secret: 'OPENAI_API_KEY', optionalEndpoint: true },
  anthropic: { endpoint: 'https://api.anthropic.com/v1', model: 'claude-sonnet-5', secret: 'ANTHROPIC_API_KEY', optionalEndpoint: true },
  gemini: { endpoint: 'https://generativelanguage.googleapis.com/v1beta/openai', model: 'gemini-2.5-flash', secret: 'GEMINI_API_KEY', optionalEndpoint: true },
  bedrock: { model: 'your-bedrock-model-id', secret: 'AWS_SECRET_ACCESS_KEY' },
  huggingface: { endpoint: 'https://your-endpoint.endpoints.huggingface.cloud', model: 'your-model-id', secret: 'HF_API_TOKEN' },
}
const validSecretReference = (value) => !value || (
  /^(?:[A-Za-z_][A-Za-z0-9_]*|keyvault:[A-Za-z0-9-]+)$/.test(value)
  && !/^(?:sk-|hf_|AIza)/.test(value)
)
// Providers with a real adapter behind them — the ones that can be ENABLED here, because enabling
// anything else would arm an escalation that silently never fires.
//
// This set said {azure_openai} while providers.py had shipped working OpenAI and Anthropic
// adapters (OpenAIVisionProvider / AnthropicVisionProvider), both wired into _adapter_for,
// cloud_vision_provider() and active_vision_provider(). The adapters were reachable from the
// escalation path and from the selector; only this gate stood between them and an admin. The
// backend agrees with this list rather than being told by it — providerActivation.test.js pins
// that the two cannot drift, and PUT /ai/providers refuses to enable a provider it cannot build.
const ADAPTER_READY = new Set(['azure_openai', 'openai', 'anthropic', 'gemini', 'bedrock', 'huggingface'])

// ADR 0019 §6 — the admin's AI provider governance page. The key VALUE never reaches the
// database: this stores only the secret's NAME (key_secret_ref), and the page shows whether the
// referenced secret is present, never its value.
//
// TWO WAYS to supply that secret, and the difference is who does the work:
//   * an ops team provisions it as an environment/container secret and the admin types its NAME
//     (the original design, and the only option where no vault is configured);
//   * where the deployment has a Key Vault, the admin pastes the key itself and it is written
//     STRAIGHT to the vault — `secretWrite.available` gates that field, the input is write-only
//     (its value comes from local state that starts empty and is cleared after every attempt,
//     never from the server), and the stored reference becomes `keyvault:acp-ai-<provider>-key`.
//
// The comment here used to say "the KEY is never entered here", and aiProviders.test.js asserted
// that sentence. Both were true until the vault path shipped; the test went on passing against
// the COMMENT after the UI gained the field, which is how a stale claim becomes the thing a test
// protects. The assertion now names the property that is actually load-bearing: no key value in
// the database, and none in the non-secret config PUT.
export function AIProvidersPanel({ onAccess }) {
  const [providers, setProviders] = useState(null)
  const [policy, setPolicy] = useState(null)
  const [policyDraft, setPolicyDraft] = useState(null)
  const [pilot, setPilot] = useState(null)
  const [draft, setDraft] = useState({})     // provider -> edited fields
  const [busy, setBusy] = useState('')
  const [note, setNote] = useState('')
  // Whether THIS deployment can accept a pasted key at all (a Key Vault is configured and its
  // SDK is present). Default false: without a vault the field must not appear, because the only
  // other way to learn it cannot work is to type a live credential into it.
  const [secretWrite, setSecretWrite] = useState({ available: false, reason: '', kind: '' })
  // Held in component state, never in `draft` — draft is what the Save button sends, and a key
  // must never ride along with the non-secret config PUT.
  const [keyDraft, setKeyDraft] = useState({})
  // A 403 here is the one reliable read-only signal the SPA has: /ai/providers and
  // PUT /settings share the same owner-only gate. Swallowing it (the old
  // `.catch(() => setProviders([]))`) left every admin field editable and every save
  // guaranteed to fail, which is how a non-owner spent a session typing into a form
  // that could not persist anything.
  const [denied, setDenied] = useState(false)
  useEffect(() => {
    Promise.all([getAiProviders(), getSecondOpinionPolicy(), getRemediationPilot()])
      .then(([d, p, remediationPilot]) => {
        setProviders(d.providers || [])
        setPolicy(p); setPolicyDraft(p)
        setPilot(remediationPilot)
        setSecretWrite(d.secret_write || { available: false, reason: '', kind: '' })
        onAccess?.(true)
      })
      .catch((e) => {
        const forbidden = /\b403\b|forbidden|owner/i.test(e?.message || '')
        setDenied(forbidden); setProviders([]); onAccess?.(!forbidden)
      })
  }, [])
  if (!providers || !policyDraft || !pilot) return null
  // Nothing to show read-only either — the GET that would have supplied the rows is what 403'd.
  if (denied) return <ReadOnlyNotice />
  const edit = (p, field, val) => setDraft((d) => ({ ...d, [p]: { ...(d[p] || {}), [field]: val } }))
  const field = (row, f) => (draft[row.provider]?.[f] ?? row[f] ?? '')
  const saveKey = (row) => {
    const value = (keyDraft[row.provider] || '').trim()
    if (!value) return
    setBusy(row.provider); setNote('')
    putAiProviderSecret(row.provider, value)
      .then((res) => {
        // Clear the field on success AND on failure below: a live credential must not sit in a
        // form for the rest of the session, and a retry is one paste away.
        setKeyDraft((k) => ({ ...k, [row.provider]: '' }))
        setProviders(res.providers)
        setDraft((d) => { const { key_secret_ref, ...rest } = d[row.provider] || {}; return { ...d, [row.provider]: rest } })
        setNote(`✓ ${PROVIDER_LABELS[row.provider] || row.provider} key stored in the vault`)
      })
      .catch((e) => { setKeyDraft((k) => ({ ...k, [row.provider]: '' }))
                      setNote(e.message || 'the key was not stored') })
      .finally(() => setBusy(''))
  }
  const save = (row) => {
    const d = draft[row.provider] || {}
    if (!validSecretReference(field(row, 'key_secret_ref').trim())) return
    setBusy(row.provider); setNote('')
    putAiProvider({
      provider: row.provider,
      enabled: d.enabled ?? row.enabled,
      endpoint: field(row, 'endpoint'),
      deployment: field(row, 'deployment'),
      model: field(row, 'model'),
      key_secret_ref: field(row, 'key_secret_ref').trim(),
    })
      .then((res) => { setProviders(res.providers); setDraft((x) => ({ ...x, [row.provider]: {} }))
                       setNote(wrote(res, `✓ ${PROVIDER_LABELS[row.provider] || row.provider} saved`)) })
      .catch((e) => setNote(e.message || 'save failed'))
      .finally(() => setBusy(''))
  }
  const savePolicy = () => {
    setBusy('second-opinion'); setNote('')
    const next = {
      enabled: !!policyDraft.enabled,
      criteria: policyDraft.criteria || [],
      confidence_threshold: policyDraft.confidence_threshold || 'low',
      max_requests_per_scan: Number(policyDraft.max_requests_per_scan),
      max_requests_per_day: Number(policyDraft.max_requests_per_day),
      max_daily_cost_usd: Number(policyDraft.max_daily_cost_usd),
      estimated_cost_per_request_usd: Number(policyDraft.estimated_cost_per_request_usd),
    }
    putSecondOpinionPolicy(next)
      .then((res) => {
        setPolicy(res); setPolicyDraft(res)
        setNote(wrote(res, '✓ Assessment second-opinion policy saved for future scans'))
      })
      .catch((e) => setNote(e.message || 'policy save failed'))
      .finally(() => setBusy(''))
  }
  const policyDirty = JSON.stringify(policyDraft) !== JSON.stringify(policy)
  const purposeEnabled = (policyDraft.criteria || []).includes('1.3.5')
  const togglePilot = () => {
    setBusy('remediation-pilot'); setNote('')
    const next = { ...pilot, enabled: !pilot.enabled }
    delete next.running; delete next.stop_reasons; delete next.metrics
    delete next.provider; delete next.model; delete next.window_days
    putRemediationPilot(next)
      .then((res) => { setPilot(res); setNote(wrote(res, `✓ Remediation pilot ${res.enabled ? 'armed' : 'stopped'}`)) })
      .catch((e) => setNote(e.message || 'pilot update failed'))
      .finally(() => setBusy(''))
  }
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>AI providers <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>· governance &amp; bring-your-own-key</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        The platform runs <b>local-first</b>: your on-box Ollama handles everything by default, at
        $0, with no document leaving your network. Enabling a governed cloud provider permits ACP
        to send a document image off-box when the local model cannot ground it, including dense
        charts and LOW-confidence assessment findings. Assessment second opinions send the first
        rendered page only; ACP records the provider, processing zone and estimated call cost on
        the finding. Disable every provider to keep all inference local.
      </p>
      <p className="muted" style={{ fontSize: 13, background: 'var(--card, #f7f4fb)', border: '1px solid var(--line)', borderRadius: 8, padding: '8px 12px' }}>
        🔐 <b>The key value never reaches the database.</b> Enter the secret’s <b>reference name</b>,
        such as <code>ANTHROPIC_API_KEY</code> or <code>keyvault:acp-ai-anthropic-key</code>, not the API key.
        The secret must be available to the running ACP app. A GitHub secret alone is not available
        here until your deployment provisions it. Where a vault is configured, use the separate
        “Store key in the vault” control below. Connection tests use your saved settings.
      </p>
      <section aria-labelledby="remediation-pilot-heading" style={{ border: `1px solid ${pilot.running ? '#6a9b3c' : 'var(--line)'}`, borderRadius: 10, padding: '12px 14px', margin: '14px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <b id="remediation-pilot-heading">Sonnet-assisted remediation pilot</b>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: pilot.running ? '#2C5209' : 'var(--muted)' }}>
            {pilot.running ? 'Running · human approval required' : pilot.enabled ? 'Automatically stopped' : 'Off'}
          </span>
        </div>
        <p className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
          Limited to WCAG 2.4.4 link-purpose drafts in DOCX and HTML. Every draft stays in Review &amp; Approve; deterministic fixes and all other criteria are unchanged.
        </p>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: 12, marginBottom: 9 }}>
          <span><b>{pilot.metrics?.calls || 0}</b> / {pilot.max_calls} calls</span>
          <span><b>${Number(pilot.metrics?.cost_usd || 0).toFixed(4)}</b> / ${Number(pilot.max_spend_usd).toFixed(2)}</span>
          <span><b>{pilot.metrics?.accepted || 0}</b> / {pilot.metrics?.decisions || 0} accepted</span>
          <span><b>{pilot.metrics?.cleared || 0}</b> / {pilot.metrics?.validated || 0} validated clear</span>
        </div>
        {pilot.stop_reasons?.length > 0 && pilot.enabled && <p role="alert" style={{ color: 'var(--error-fg-strong)', fontSize: 12, margin: '7px 0' }}>
          Stop gate: {pilot.stop_reasons.join(' · ')}
        </p>}
        <button className={pilot.enabled ? 'ghost small' : 'primary small'} disabled={busy === 'remediation-pilot'} onClick={togglePilot}>
          {pilot.enabled ? 'Stop pilot' : 'Start controlled pilot'}
        </button>
        <span className="muted" style={{ marginLeft: 9, fontSize: 11.5 }}>30-day gates: failures ≤10% · acceptance ≥90% · edits ≤20% · validation ≥95% · any regression stops immediately</span>
      </section>
      <section aria-labelledby="second-opinion-heading" style={{ border: '1px solid var(--line)', borderRadius: 10, padding: '12px 14px', margin: '14px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <b id="second-opinion-heading">Assessment second opinions</b>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: policyDraft.enabled ? '#2C5209' : 'var(--muted)' }}>
            {policyDraft.enabled ? 'Enabled for future scans' : 'Off · local-only'}
          </span>
        </div>
        <p className="muted" style={{ fontSize: 12 }}>
          This policy is copied into each new scan. Changing it never silently changes a scan that
          is already running. A provider must also be enabled below before any call can occur.
        </p>
        <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13 }}>
          <input type="checkbox" checked={!!policyDraft.enabled}
                 onChange={(e) => setPolicyDraft({ ...policyDraft, enabled: e.target.checked })} />
          Permit cloud second opinions for approved findings
        </label>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="muted" style={{ fontSize: 12, marginBottom: 6 }}>Eligible WCAG criteria</legend>
            <label style={{ display: 'flex', gap: 8, fontSize: 13 }}>
              <input type="checkbox" checked={purposeEnabled}
                     onChange={(e) => setPolicyDraft({ ...policyDraft,
                       criteria: e.target.checked ? ['1.3.5'] : [] })} />
              1.3.5 Identify Input Purpose
            </label>
          </fieldset>
          <L label="Maximum detector confidence">
            <select value={policyDraft.confidence_threshold || 'low'} style={INP}
                    onChange={(e) => setPolicyDraft({ ...policyDraft, confidence_threshold: e.target.value })}>
              <option value="low">Low only</option>
              <option value="medium">Low and medium</option>
              <option value="high">Low, medium and high</option>
            </select>
          </L>
          <L label="Maximum requests per scan"><input type="number" min="1" value={policyDraft.max_requests_per_scan}
            onChange={(e) => setPolicyDraft({ ...policyDraft, max_requests_per_scan: e.target.value })} style={INP} /></L>
          <L label="Maximum requests per day"><input type="number" min="1" value={policyDraft.max_requests_per_day}
            onChange={(e) => setPolicyDraft({ ...policyDraft, max_requests_per_day: e.target.value })} style={INP} /></L>
          <L label="Daily cloud-cost ceiling (USD)"><input type="number" min="0.01" step="0.01" value={policyDraft.max_daily_cost_usd}
            onChange={(e) => setPolicyDraft({ ...policyDraft, max_daily_cost_usd: e.target.value })} style={INP} /></L>
          <L label="Estimated cost reserved per request (USD)"><input type="number" min="0.000001" step="0.001" value={policyDraft.estimated_cost_per_request_usd}
            onChange={(e) => setPolicyDraft({ ...policyDraft, estimated_cost_per_request_usd: e.target.value })} style={INP} /></L>
        </div>
        <p className="muted" style={{ fontSize: 11 }}>
          Request limits are enforced atomically. Cost admission uses the labelled estimate above;
          measured provider cost is recorded after each call and shown in Live Operations.
        </p>
        {policyDraft.enabled && !purposeEnabled && (
          <p role="alert" style={{ color: 'var(--error-fg-strong)', fontSize: 12 }}>
            Select at least one criterion before enabling this policy.
          </p>
        )}
        <button className="ghost small" style={{ marginTop: 10 }} onClick={savePolicy}
                disabled={!policyDirty || busy === 'second-opinion' || (policyDraft.enabled && !purposeEnabled)}>
          {busy === 'second-opinion' ? 'Saving…' : 'Save second-opinion policy'}
        </button>
      </section>
      {providers.map((row) => {
        const ready = ADAPTER_READY.has(row.provider)
        const dirty = Object.entries(draft[row.provider] || {}).some(([f, value]) => value !== (row[f] ?? ''))
        const examples = PROVIDER_FIELDS[row.provider] || { model: 'your-model-id', secret: 'PROVIDER_API_KEY' }
        const invalidRef = !validSecretReference(field(row, 'key_secret_ref').trim())
        const pendingKey = !!(keyDraft[row.provider] || '').trim()
        const testBlocked = dirty ? 'Save changes before testing.'
          : pendingKey ? 'Store the key in the vault before testing.'
          : busy === row.provider ? 'Wait for saving to finish before testing.' : ''
        return (
          <div key={row.provider} style={{ border: '1px solid var(--line)', borderRadius: 10, padding: '12px 14px', margin: '10px 0', opacity: ready ? 1 : 0.7 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <b style={{ fontSize: 14 }}>{PROVIDER_LABELS[row.provider] || row.provider}</b>
              {!ready && <span className="muted" style={{ fontSize: 11 }}>· adapter coming (config saved)</span>}
              {ready && <TestConnection key={JSON.stringify([row, draft[row.provider], pendingKey])}
                provider={row.provider} blockedReason={testBlocked} />}
              <span style={{ marginLeft: 'auto', fontSize: 12 }}
                    title="Whether the saved secret reference is available to the running ACP app; unsaved edits are not checked">
                {row.key_present
                  ? <span style={{ color: '#2C5209' }}>🔵 key present · {row.credential_source}</span>
                  : <span className="muted">key not set</span>}
              </span>
            </div>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '10px 0', fontSize: 13 }}>
              <input type="checkbox" checked={draft[row.provider]?.enabled ?? row.enabled}
                     onChange={(e) => edit(row.provider, 'enabled', e.target.checked)} disabled={!ready || busy === row.provider} />
              <span>Enable as an escalation fallback {row.enabled ? '' : '(off — local-only)'}</span>
            </label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              {examples.endpoint && <L label={examples.optionalEndpoint ? 'Endpoint (optional override)' : 'Endpoint'}>
                <input value={field(row, 'endpoint')} placeholder={examples.endpoint} disabled={busy === row.provider}
                       onChange={(e) => edit(row.provider, 'endpoint', e.target.value)} style={INP} /></L>}
              {examples.deployment && <L label="Deployment name"><input value={field(row, 'deployment')}
                     placeholder={examples.deployment} disabled={busy === row.provider}
                     onChange={(e) => edit(row.provider, 'deployment', e.target.value)} style={INP} /></L>}
              <L label={row.provider === 'azure_openai' ? 'Model (for cost)' : 'Model ID (required)'}>
                <input value={field(row, 'model')} placeholder={examples.model} disabled={busy === row.provider}
                       onChange={(e) => edit(row.provider, 'model', e.target.value)} style={INP} /></L>
              <L label="Secret reference NAME (not the key)"><input value={field(row, 'key_secret_ref')}
                     placeholder={examples.secret} autoComplete="off" spellCheck={false} disabled={busy === row.provider}
                     aria-invalid={invalidRef} onChange={(e) => edit(row.provider, 'key_secret_ref', e.target.value)} style={INP} /></L>
            </div>
            <p className="muted" style={{ fontSize: 12, margin: '8px 0' }}>
              Examples are hints, not saved values.{examples.optionalEndpoint && ' Leave Endpoint blank to use the provider’s default API.'}
              {row.provider === 'bedrock' && ' AWS region and access key ID must also be provisioned in the provider configuration.'}
              {' '}The key indicator reflects the saved reference.
            </p>
            {invalidRef && <p role="alert">Enter a secret name, not an API key. Use an environment variable name or keyvault:secret-name.</p>}
            {/* The key field appears ONLY where the deployment can actually store one. It is
                write-only by construction: `value` comes from local state that starts empty and
                is cleared after every attempt, never from the server — no read path returns a key,
                so there is nothing to prefill and nothing to reveal. */}
            {secretWrite.available && (
              <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px dashed var(--line)' }}>
                <L label={`Or paste the key — stored in ${secretWrite.kind === 'azure_key_vault' ? 'Key Vault' : 'the secret store'}, never in the database`}>
                  <input type="password" autoComplete="off" value={keyDraft[row.provider] || ''} disabled={busy === row.provider}
                         placeholder="paste the provider API key"
                         onChange={(e) => setKeyDraft((k) => ({ ...k, [row.provider]: e.target.value }))}
                         style={INP} />
                </L>
                <button className="ghost small" style={{ marginTop: 8 }} onClick={() => saveKey(row)}
                        disabled={busy === row.provider || !(keyDraft[row.provider] || '').trim()}>
                  {busy === row.provider ? 'Storing…' : 'Store key in the vault'}
                </button>
                <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                  The value is written straight to the vault and this page never receives it back.
                  The reference above becomes <code>keyvault:acp-ai-{row.provider}-key</code>.
                </div>
              </div>
            )}
            <button className="ghost small" style={{ marginTop: 10 }} onClick={() => save(row)}
                    disabled={busy === row.provider || !dirty || invalidRef}>
              {busy === row.provider ? 'Saving…' : 'Save'}
            </button>
          </div>
        )
      })}
      {note && <p style={{ fontSize: 13, color: msgColor(note) }}>{note}</p>}
    </div>
  )
}

// "Test connection" — the one control on this page that makes an outbound call to a third party,
// and the reason it is safe to press before anyone has agreed to send documents anywhere: the
// backend sends a SYNTHETIC 64×64 probe image it generates in-process, never a customer document.
// The wording says so on the button's own title rather than in a paragraph elsewhere, because the
// question "what did I just send them?" is asked at the moment of pressing.
//
// Reports the outcome the adapter distinguished — a transport failure, an HTTP status, or a 200
// with nothing in it — because those need three different fixes. Never renders a key, and the
// backend does not return the model's caption of the probe either.
export function TestConnection({ provider, blockedReason = '' }) {
  const [state, setState] = useState(null)     // null | 'testing' | result object
  const run = () => {
    if (blockedReason) return
    setState('testing')
    testAiProvider(provider)
      .then((r) => setState(r))
      .catch((e) => setState({ ok: false, reason: 'request_failed', detail: String(e?.message || e) }))
  }
  const r = state && state !== 'testing' ? state : null
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <button className="ghost small" onClick={run} disabled={state === 'testing' || !!blockedReason}
              title="Tests the saved settings. Sends a small synthetic test image generated by the server — never one of your documents">
        {state === 'testing' ? 'Testing…' : 'Test connection'}
      </button>
      {blockedReason && <span role="status" style={{ fontSize: 11 }}>{blockedReason}</span>}
      {r && !blockedReason && (
        <span role="status" style={{ fontSize: 11, color: r.ok ? '#2C5209' : '#8A1C1C' }}>
          {r.ok
            ? `✓ reached ${r.model || provider} · ${r.zone} · ${r.latency_ms}ms`
            : `✗ ${r.detail || r.reason}`}
          {r.ok && r.cost_usd ? ` · $${Number(r.cost_usd).toFixed(6)}` : ''}
        </span>
      )}
    </span>
  )
}

// Shown instead of an editable admin form when the API has told us this user cannot write it.
// Saying so up front is the whole point: the alternative is a form that accepts input and
// then fails, which is indistinguishable from the platform being broken.
const ReadOnlyNotice = () => (
  <p role="status" style={{ fontSize: 13, background: '#FBF1DF', border: '1px solid #EAD9BF',
                            borderRadius: 8, padding: '10px 12px', color: '#6B4A0B' }}>
    🔒 <b>Read-only.</b> These settings are owner-only, and you are signed in as another user —
    the fields are disabled because a save would be rejected. Ask the platform owner to change them.
  </p>
)
// Badges the whole admin panel on a build whose writes cannot reach a platform. Gated on the
// BUILD flag, not on a response, so it is on screen before the first save rather than after: the
// fake "✓ endpoint switched" was believed on the Netlify site precisely because nothing on the
// panel said otherwise until the outcome line — and the outcome line was the thing that lied.
// Same shape as ReadOnlyNotice above; both answer "why won't my change stick?" before it is asked.
const SimNotice = () => (
  <p role="status" style={{ margin: '10px 16px 0', fontSize: 13, background: '#FBF1DF',
                            border: '1px solid #EAD9BF', borderRadius: 8, padding: '10px 12px', color: '#6B4A0B' }}>
    🎭 <b>Demo build — every setting here is simulated.</b> Nothing on this screen reaches a
    platform: changes live in this browser tab until you reload, and no production endpoint, vision
    model, storage setting or user list is affected. Use a build served by the real API to change
    them for real.
  </p>
)
const INP = { display: 'block', width: '100%', padding: '4px 8px', marginTop: 4, border: '1px solid var(--line)', borderRadius: 6, boxSizing: 'border-box' }
const L = ({ label, children }) => (<label style={{ fontSize: 12 }} className="muted">{label}{children}</label>)

function CopyToken() {
  const token = getToken()
  const [copied, setCopied] = useState(false)
  if (!token) return null
  const preview = token.length > 24 ? token.slice(0, 12) + '…' + token.slice(-8) : token
  const copy = () => {
    navigator.clipboard.writeText(token).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }
  return (
    <div style={{ maxWidth: 560, marginTop: 24, paddingTop: 20, borderTop: '1px solid var(--line)' }}>
      <h3 style={{ marginTop: 0 }}>API bearer token <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>· for local testing</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Your current session token — pass it as <code>--token</code> to the smoke test script or
        any direct API call. Expires with your session; re-copy after signing in again.
      </p>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <code style={{ flex: 1, padding: '6px 10px', borderRadius: 7, background: 'var(--surface)', border: '1px solid var(--line)', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {preview}
        </code>
        <button onClick={copy} style={{ flexShrink: 0, padding: '6px 14px', borderRadius: 7, border: '1px solid var(--line)', background: copied ? '#2F6B43' : 'var(--surface)', color: copied ? '#fff' : 'inherit', cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>
          {copied ? '✓ Copied' : 'Copy'}
        </button>
      </div>
    </div>
  )
}

// Settings' role is CONFIGURATION, not observation — "Settings lets an administrator change how
// workers operate" (the navigation this was steered toward, 2026-08-28). The live queue/job view
// (QueuePanel) belongs in Monitor → Workers & Queue instead, where every other live operational
// view already lives (Source drift, Scheduled re-scans, the Audit trail). This tab is deliberately
// just the one thing that IS configuration: the Azure Container Apps replica floor.
// Retired from Settings navigation; retained for restoration. Current controls live in Scheduling.
function WorkerConfiguration({ me }) {
  return (
    <div style={{ maxWidth: 560 }}>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        "Warm capacity" is the Azure Container Apps replica floor: raise it before a large batch to
        have workers already up rather than cold-starting on demand; lower it after to stop paying
        for idle containers. For what the workers are doing right now, see Monitor → Workers &amp; Queue.
      </p>
      {/* No wrapping label: WorkerReplicaControl already renders nothing when Azure replica
          control isn't configured (no AZURE_SUBSCRIPTION_ID), and its own trailing "Azure
          replicas (max N)" text self-describes what the control is when it does render — an
          external label here would dangle with nothing beside it in the common case where
          it's unconfigured (local dev, most demo deployments). */}
      <WorkerReplicaControl me={me} />
    </div>
  )
}


function ReleasePreferences() {
  const [zone, setZone] = useState('America/Chicago')
  const [saved, setSaved] = useState('America/Chicago')
  const [msg, setMsg] = useState('')
  useEffect(() => { getMyScope().then((r) => { setZone(r.release_timezone || 'America/Chicago'); setSaved(r.release_timezone || 'America/Chicago') }).catch(() => {}) }, [])
  const options = [
    ['UTC', 'UTC'], ['America/Los_Angeles', 'US Pacific'], ['America/Denver', 'US Mountain'],
    ['America/Chicago', 'US Central'], ['America/New_York', 'US Eastern'], ['Asia/Kolkata', 'India'],
  ]
  const save = () => putMyReleaseTimezone(zone).then((r) => {
    setSaved(r.release_timezone || zone); setMsg(r.simulated ? SIM_NOT_WRITTEN : '✓ Saved')
  }).catch((e) => setMsg(`⚠ ${e.message || 'Could not save'}`))
  return (
    <div style={{ maxWidth: 560 }}>
      <h3 style={{ marginTop: 0 }}>Release folder timestamps</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        New Remediated folders use your local timezone in their name. Audit timestamps remain UTC.
      </p>
      <label htmlFor="release-timezone" style={{ display: 'block', fontSize: 12, fontWeight: 700, marginBottom: 5 }}>TIMEZONE</label>
      <select id="release-timezone" value={zone} onChange={(e) => { setZone(e.target.value); setMsg('') }}
              style={{ minWidth: 230, padding: '7px 9px' }}>
        {options.map(([value, label]) => <option key={value} value={value}>{label} · {value}</option>)}
      </select>
      <div style={{ marginTop: 12 }}>
        <button className="primary" disabled={zone === saved} onClick={save}>Save timezone</button>
        {msg && <span role="status" style={{ marginLeft: 10, fontSize: 12 }}>{msg}</span>}
      </div>
    </div>
  )
}

export default function Settings({ onClose, files = [], onDelegationChange, me = null }) {
  const [tab, setTab] = useState('users')
  const panelRef = useRef(null)
  useDialog(panelRef, onClose)
  // ARIA tabs: one Tab stop (the selected tab), arrows/Home/End move and activate, and each tab
  // names the single panel below, which is labelled by whichever tab is selected.
  const sectionTab = (key) => ({
    role: 'tab', id: `settings-tab-${key}`, 'aria-selected': tab === key,
    'aria-controls': 'settings-panel', tabIndex: tab === key ? 0 : -1,
    onKeyDown: handleWorkflowTabKeyDown,
    className: tab === key ? 'fchip on' : 'fchip', onClick: () => setTab(key),
  })
  return (
    <div className="setoverlay" role="dialog" aria-modal="true" aria-label="Platform settings" onClick={onClose}>
      <div className="setpanel" ref={panelRef} tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <div className="sethead">
          <div><b>⚙ Platform settings</b><span className="muted"> · {me?.is_admin ? 'administrator' : 'view-only'} access</span></div>
          <button className="ghost small" aria-label="Close settings" onClick={onClose}>✕</button>
        </div>
        {/* Above the subtabs on purpose — the SIM badge is true of every write path in this panel
            (the Users allowlist included), not only the panels reachable from a tab. */}
        {SIM && <SimNotice />}
        {/* Access, worker and AI governance live together here. Operational telemetry remains in
            Live Operations; this surface controls whether off-box AI calls may happen at all. */}
        <div className="subtabs" role="tablist" aria-label="Settings sections">
          <button {...sectionTab('owners')}>Owners</button>
          <button {...sectionTab('users')}>Users</button>
          {/* Beside People, per PRD §8's "a dedicated Roles tab beside People". They are two
              halves of one job — a role is designed here and handed out there — and separating
              them across screens is how an administrator assigns a role they have not read. */}
          <button {...sectionTab('roles')}>Roles</button>
          <button {...sectionTab('mydata')}>My Data</button>
          <button {...sectionTab('myscope')}>My Scope</button>
          {/* Scheduling is the single capacity-management surface. */}
          <button {...sectionTab('scheduling')}>Scheduling</button>
          <button {...sectionTab('release')}>Release</button>
          <button {...sectionTab('ai')}>AI Governance</button>
          {/* ADR 0021's "Settings → Review Memory". The tab renders for everyone because GET
              /org-memory has no admin gate — seeing which house style shaped a draft is not an
              admin privilege — and ReviewMemory itself withholds every write control unless
              me?.is_admin, matching the backend's _require_admin on all three writes. */}
          <button {...sectionTab('memory')}>Review Memory</button>
          {/* Read-only product metadata for everyone who can open Settings: what each rule checks per
              criterion and format, its thresholds and its limits. No write controls — thresholds are
              shown, not edited (ruleExplanations.test.jsx). */}
          <button {...sectionTab('rules')}>Rule explanations</button>
        </div>
        <div className="setbody" id="settings-panel" role="tabpanel" aria-labelledby={`settings-tab-${tab}`}>
          {tab === 'owners' && <OwnerDelegate files={files} onChanged={onDelegationChange} />}
          {tab === 'users' && <PeopleAccess />}
          {tab === 'roles' && <WorkspaceRoles />}
          {tab === 'mydata' && <><ResetMyData /><CopyToken /></>}
          {tab === 'myscope' && <MyScanScope />}
          {tab === 'scheduling' && <CapacitySchedule me={me} />}
          {tab === 'release' && <ReleasePreferences />}
          {tab === 'ai' && <AIProvidersPanel />}
          {tab === 'memory' && <ReviewMemory me={me} />}
          {tab === 'rules' && <RuleExplanations me={me} />}
        </div>
      </div>
    </div>
  )
}
