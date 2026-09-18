import { useEffect, useState } from 'react'
import { getFileReportFacts } from './api.js'
import PagePreview, { PreviewUnavailable } from './PagePreview.jsx'
import { criterionName, fmtOfFile, locationLabel, locationOf, scOfValue } from './reportEvidence.js'

// The target of a report's finding link (evidenceLink.js, Contract 2): ONE recorded finding or
// saved change, resolved by the server's own id against the owner-scoped facts endpoint.
//
// What this screen will not do, each of which was the easy way to make a link "work":
//   - Pick a nearby record. If the id is not in the current evidence it says so — the document
//     may have been re-assessed, which gives its findings new ids — and shows the identity the
//     link named. It never shows the first finding of the same criterion instead.
//   - Show a page it did not get. A preview is fetched only through the hash-bound artifact route
//     (strict: an out-of-range page is a 404 with the reason), and only for a location that IS a
//     page: a PDF page or a PowerPoint slide. Word paragraphs and Excel cells have no page, and
//     the screen says that rather than showing page 1.
//   - Claim a capability it does not have. It shows evidence; it does not open or edit the
//     document in its native application.
//
// Auth: App.jsx renders this only after sign-in, and the facts request is owner-scoped on the
// server, so a link opened by another account reads "not available to your account".

const short = (sha) => (sha ? String(sha).slice(0, 12) : null)
const HEX64 = /^[0-9a-f]{64}$/
const sha = (v) => (typeof v === 'string' && HEX64.test(v) ? v : null)

export const RECORD_MISSING =
  'The exact record this link names is not in the current evidence for this document. The document may have been re-assessed since the report was generated, which gives its findings new identities. ACP does not substitute a similar record.'
export const VERIFICATION_NOT_RECORDED = 'Verification not recorded'

// Why no page preview can be shown for a location, or null when a page can be requested.
export function previewPlan(fmt, loc) {
  if (fmt === 'docx') return { page: null, unit: 'page', reason: 'Word paragraphs do not map to rendered pages' }
  if (fmt === 'xlsx') return { page: null, unit: 'page', reason: 'Excel cells are not rendered as pages' }
  if (fmt !== 'pdf' && fmt !== 'pptx') return { page: null, unit: 'page', reason: 'this file type has no page preview' }
  const unit = fmt === 'pptx' ? 'slide' : 'page'
  const page = fmt === 'pptx' ? (loc?.slide ?? loc?.page ?? null) : (loc?.page ?? null)
  if (page == null) return { page: null, unit, reason: `no ${unit} was recorded for this ${loc ? 'location' : 'record'}` }
  return { page, unit, reason: null }
}

// The version sentence: does the document ACP holds now match the one the report described?
export function versionStatement({ linkSha, currentSha, version }) {
  const copy = version === 'corrected' ? 'corrected copy' : 'source document'
  if (!linkSha) {
    return { tone: 'muted', text: 'This link does not name a document version, so ACP cannot tell whether the document changed since the report was generated.' }
  }
  if (!currentSha) {
    return { tone: 'warn', text: `No sha-256 is recorded for the current ${copy}, so ACP cannot confirm it is the version the report described (sha ${short(linkSha)}).` }
  }
  if (currentSha !== linkSha) {
    return { tone: 'warn', changed: true, text: `The document changed since the report was generated: the report described version sha ${short(linkSha)}, and ACP's current record of the ${copy} is sha ${short(currentSha)}. What is shown below is the current evidence.` }
  }
  return { tone: 'ok', text: `This is the version the report described (${copy}, sha ${short(currentSha)}).` }
}

function findingVerification(f) {
  if (f?.state === 'resolved_verified') return { label: 'Verified resolved', detail: f.stateReason || null }
  if (f?.state === 'awaiting_review') return { label: 'Awaiting human review', detail: f.stateReason || null }
  return { label: VERIFICATION_NOT_RECORDED, detail: f?.stateReason || null }
}
function changeVerification(c) {
  if (c?.verification === 'verified') return { label: 'Verified', detail: c.verificationDetail || null }
  if (c?.verification === 'not_verified') return { label: 'Not verified', detail: c.verificationDetail || null }
  return { label: VERIFICATION_NOT_RECORDED, detail: null }
}

function Row({ label, children }) {
  return (
    <div className="evview-row" style={{ display: 'grid', gridTemplateColumns: '11rem 1fr', gap: 8, padding: '6px 0', borderTop: '1px solid var(--line)' }}>
      <dt className="muted" style={{ margin: 0 }}>{label}</dt>
      <dd style={{ margin: 0, minWidth: 0, overflowWrap: 'anywhere' }}>{children}</dd>
    </div>
  )
}

function TargetIdentity({ target }) {
  return (
    <dl className="evview-identity" aria-label="What this link points at" style={{ margin: '8px 0 0' }}>
      <Row label="Scan">{target.scanId}</Row>
      <Row label="File">{target.file}</Row>
      <Row label={target.findingId ? 'Finding id' : 'Saved change id'}>{target.findingId || target.changeId}</Row>
      {target.sha256 && <Row label="Version in link">sha {short(target.sha256)}{target.version ? ` (${target.version})` : ''}</Row>}
    </dl>
  )
}

export default function FindingEvidenceViewer({ target, onClose }) {
  const [state, setState] = useState({ status: 'loading', facts: null, error: null })
  const scanId = target?.scanId
  const file = target?.file

  useEffect(() => {
    setState({ status: 'loading', facts: null, error: null })
    if (!scanId || !file) return
    let live = true
    Promise.resolve()
      .then(() => getFileReportFacts(scanId, file))
      .then((facts) => {
        if (!live) return
        if (!facts || typeof facts !== 'object') {
          setState({ status: 'error', facts: null, error: 'Demo mode has no report server, so the recorded evidence cannot be shown here.' })
          return
        }
        setState({ status: 'loaded', facts, error: null })
      })
      .catch((e) => {
        if (!live) return
        const notYours = e?.status === 404 || e?.scanUnavailable
        setState({
          status: 'error', facts: null,
          error: notYours
            ? 'This evidence is not available to your account: the scan or the file does not exist, or it belongs to another account.'
            : `The recorded evidence could not be read (${e?.message || e}). Nothing is shown in its place.`,
        })
      })
    return () => { live = false }
  }, [scanId, file])

  const back = (
    <button type="button" className="btn" onClick={onClose}>← Back to ACP</button>
  )
  const frame = (body) => (
    <main className="evview" aria-labelledby="evview-title" style={{ maxWidth: 880, margin: '24px auto', padding: '0 16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
        <h1 id="evview-title" style={{ fontSize: 20, margin: 0 }}>{target?.changeId ? 'Saved change evidence' : 'Finding evidence'}</h1>
        {back}
      </div>
      {body}
    </main>
  )

  if (!target) return null
  if (state.status === 'loading') {
    return frame(<p role="status" className="muted">Loading the recorded evidence for {file}…</p>)
  }
  if (state.status === 'error') {
    return frame(
      <section role="alert" className="evview-missing" style={{ marginTop: 12 }}>
        <p>{state.error}</p>
        <TargetIdentity target={target} />
      </section>,
    )
  }

  const facts = state.facts
  const id = facts.identity || {}
  // The server built these facts for exactly (scan, file). If it answered for anything else, the
  // record below would belong to another document — refuse rather than show it.
  if ((id.scanId && id.scanId !== scanId) || (id.file && id.file !== file)) {
    return frame(
      <section role="alert" className="evview-missing" style={{ marginTop: 12 }}>
        <p>The evidence returned is for a different document than the one this link names, so it is not shown.</p>
        <TargetIdentity target={target} />
      </section>,
    )
  }
  const isChange = !!target.changeId
  const list = isChange ? facts.savedChanges : facts.findings
  const record = Array.isArray(list) ? list.find((r) => r && r.id === (isChange ? target.changeId : target.findingId)) : null
  if (!record) {
    return frame(
      <section role="alert" className="evview-missing" style={{ marginTop: 12 }}>
        <p>{RECORD_MISSING}</p>
        <TargetIdentity target={target} />
        {isChange && facts.savedChangesComplete === false && (
          <p className="muted">The saved-change list for this document is itself incomplete, so the record may exist and not have been returned.</p>
        )}
      </section>,
    )
  }

  const fmt = fmtOfFile(file)
  const loc = locationOf({ location: record.location }, { fmt })
    || (isChange && record.locator ? locationOf({ locator: record.locator }, { fmt }) : null)
  const sc = scOfValue(record.sc) || scOfValue(record.ruleId)
  const cname = sc ? criterionName(sc) : null
  const verification = isChange ? changeVerification(record) : findingVerification(record)
  const version = target.version || (isChange ? 'corrected' : 'source')
  const currentSha = version === 'corrected'
    ? (sha(record.artifactSha256) || sha(id.correctedSha256))
    : sha(id.sourceSha256)
  const ver = versionStatement({ linkSha: target.sha256, currentSha, version })
  const plan = previewPlan(fmt, loc)

  return frame(
    <article className="evview-record" style={{ marginTop: 12 }}>
      <dl style={{ margin: 0 }}>
        <Row label="File">{file}</Row>
        <Row label="Scan">{scanId}</Row>
        <Row label="Criterion">{sc ? `WCAG ${sc}${cname ? ` · ${cname}` : ''}` : (record.ruleId || 'Not recorded')}{record.ruleId && sc && record.ruleId !== sc ? <span className="muted"> · rule {record.ruleId}</span> : null}</Row>
        {!isChange && <Row label="Severity">{record.severity ? String(record.severity).toLowerCase() : 'Not recorded'}</Row>}
        {!isChange && <Row label="Detail">{record.detail || 'Not recorded'}</Row>}
        {isChange && <Row label="Before">{record.before ? <q>{record.before}</q> : <span className="muted">empty</span>}</Row>}
        {isChange && <Row label="After">{record.after ? <q>{record.after}</q> : <span className="muted">empty</span>}</Row>}
        {isChange && record.note && <Row label="Note">{record.note}</Row>}
        <Row label="Location">{locationLabel(loc)}</Row>
        {!isChange && <Row label="Recommended action">{record.recommendedAction || 'Not recorded'}</Row>}
        <Row label="Verification">
          <b>{verification.label}</b>{verification.detail ? <span className="muted"> — {verification.detail}</span> : null}
        </Row>
        <Row label="Version">
          <span className={ver.tone === 'warn' ? 'evview-version-warn' : undefined}
                style={ver.tone === 'warn' ? { color: 'var(--warn-fg, #8a4b00)' } : undefined}>{ver.text}</span>
        </Row>
      </dl>
      <section aria-label="Page preview" style={{ marginTop: 14 }}>
        {plan.reason
          ? <PreviewUnavailable reason={plan.reason} />
          : !currentSha
            ? <PreviewUnavailable reason={`no sha-256 is recorded for this version of the document, so its exact ${plan.unit} cannot be requested`} />
            : (
              <>
                <p className="muted" style={{ fontSize: 12, margin: '0 0 6px' }}>
                  {version === 'corrected' ? 'Corrected copy' : 'Original document'}, {plan.unit} {plan.page} (sha {short(currentSha)})
                </p>
                <PagePreview scanId={scanId} file={file} page={plan.page} sha256={currentSha} unit={plan.unit} maxHeight={520} />
              </>
            )}
      </section>
    </article>,
  )
}
