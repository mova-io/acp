import { useEffect, useState } from 'react'
import { getFileReportFacts } from './api.js'
import PagePreview, { PreviewUnavailable } from './PagePreview.jsx'
import { criterionName, fmtOfFile, locationLabel, locationOf, scOfValue } from './reportEvidence.js'
import { attachEvidenceLinks, fileEvidenceHref, isFileTarget } from './evidenceLink.js'

// The target of a report link (evidenceLink.js, Contract 2): ONE recorded finding or saved change,
// resolved by the server's own id against the owner-scoped facts endpoint — or, for a link that
// names a whole document (fileEvidenceHref), that document's recorded evidence, each record
// linking on to its exact view.
//
// What this screen will not do, each of which was the easy way to make a link "work":
//   - Pick a nearby record. If the id is not in the current evidence it says so — the document
//     may have been re-assessed, which gives its findings new ids — and shows the identity the
//     link named. It never shows the first finding of the same criterion instead.
//   - Show a page it did not get. A preview is fetched only through the hash-bound artifact route
//     (strict: an out-of-range page is a 404 with the reason), and only for a location that IS a
//     page: a PDF page or a PowerPoint slide. Word paragraphs and Excel cells have no page, and
//     the screen says that rather than showing page 1.
//   - Present an old record as current. A saved change records the copy it was written into; when
//     ACP's current copy is a different one, the record is shown as made AND its verification and
//     review are said to describe the earlier copy (parent review item 8).
//   - Claim a check it did not make. The source version is the checksum ACP recorded when it read
//     the source; nothing here re-reads the provider's copy, and the screen says so. Nor does it
//     claim a recheck was started — it says one is needed.
//   - Claim a capability it does not have. It shows evidence; it does not open or edit the
//     document in its native application.
//
// Auth: App.jsx renders this only after sign-in, and the facts request is owner-scoped on the
// server, so a link opened by another account reads "not available to your account".

const short = (sha) => (sha ? String(sha).slice(0, 12) : null)
const HEX64 = /^[0-9a-f]{64}$/
const sha = (v) => (typeof v === 'string' && HEX64.test(v) ? v : null)
const isObj = (v) => v != null && typeof v === 'object' && !Array.isArray(v)

export const RECORD_MISSING =
  'The exact record this link names is not in the current evidence for this document. The document may have been re-assessed since the report was generated, which gives its findings new identities. ACP does not substitute a similar record.'
export const VERIFICATION_NOT_RECORDED = 'Verification not recorded'
export const NOT_RECORDED = 'Not recorded'
export const SOURCE_SNAPSHOT_CAVEAT =
  'That sha-256 is the checksum ACP recorded when it read the source for this scan. ACP has not re-read the copy in the source system for this view, so an edit made there since would not show here.'
export const LEGACY_NOTE_LOCATION = 'from the change note — no structured location was stored for this change'

// R1: whether a saved change's location was reconstructed from the writer's note rather than
// recorded. Read from both facts shapes: the record's `locationSource` (first R1 shape) and the
// location's own `source` (report_location.saved_change_location).
export const isLegacyNoteLocation = (rec) =>
  !!rec && (rec.locationSource === 'legacy_note' || (isObj(rec.location) && rec.location.source === 'legacy_note'))
// The server may already say so in the label ("… (from the saving step's note, not a recorded
// location)"); the qualifier is then not repeated.
const labelNamesNote = (label) => typeof label === 'string' && /\bnote\b/i.test(label)
export function locationText(loc, rec) {
  const label = locationLabel(loc)
  return isLegacyNoteLocation(rec) && loc?.label && !labelNamesNote(loc.label) ? `${label} (${LEGACY_NOTE_LOCATION})` : label
}

// Why no page preview can be shown for a location, or null when a page can be requested.
export function previewPlan(fmt, loc, { locationSource = null } = {}) {
  if (fmt === 'docx') return { page: null, unit: 'page', reason: 'Word paragraphs do not map to rendered pages' }
  if (fmt === 'xlsx') return { page: null, unit: 'page', reason: 'Excel cells are not rendered as pages' }
  if (fmt !== 'pdf' && fmt !== 'pptx') return { page: null, unit: 'page', reason: 'this file type has no page preview' }
  const unit = fmt === 'pptx' ? 'slide' : 'page'
  // R1: a location the store reconstructed from the writer's note records no page — the owner
  // contract says `page` is always None there — so nothing is requested on its strength.
  if (locationSource === 'legacy_note') return { page: null, unit, reason: `this location was read from the change note, which records no ${unit}` }
  const page = fmt === 'pptx' ? (loc?.slide ?? loc?.page ?? null) : (loc?.page ?? null)
  if (page == null) return { page: null, unit, reason: `no ${unit} was recorded for this ${loc ? 'location' : 'record'}` }
  return { page, unit, reason: null }
}

// The link-vs-current sentence: does the document ACP holds now match the one the report described?
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

/**
 * Which bytes a record is about, kept apart from which bytes ACP holds NOW (parent item 8).
 *
 *   recordSha  — the copy THIS record was made against, only where it is recorded per record: an
 *                applied-but-unverified change's own `artifactSha256`. A verified change's
 *                `artifactSha256` is the file's current corrected digest copied onto every row
 *                (report_facts.build_saved_changes), not a record of its own, so it is not used as
 *                one. A finding describes the assessed source, which has one recorded checksum.
 *   currentSha — ACP's current record of the copy the link means (source or corrected).
 *   recordStale — true: the current copy is a different one; null: cannot be established.
 *
 * `previewSha` is the record's own copy when it has one: a page of the current copy captioned as
 * the record's page is exactly the substitution this screen refuses.
 */
export function versionFacts({ target, record = null, identity = null, isChange = false }) {
  const id = isObj(identity) ? identity : {}
  const version = target?.version || (isChange ? 'corrected' : 'source')
  const copy = version === 'corrected' ? 'corrected copy' : 'source document'
  const currentSha = version === 'corrected' ? sha(id.correctedSha256) : sha(id.sourceSha256)
  const recordSha = isChange && record && record.source !== 'remediation_diff' ? sha(record.artifactSha256) : null
  const linkSha = sha(target?.sha256)
  const recordStale = recordSha ? (currentSha ? recordSha !== currentSha : null) : false
  const parts = []
  let tone
  let changed = false
  if (recordStale === true) {
    tone = 'warn'; changed = true
    parts.push(`The current document changed since this record was made: this change was saved into the ${copy} at sha ${short(recordSha)}, and ACP's current ${copy} is sha ${short(currentSha)}. The record below is shown exactly as it was made, against sha ${short(recordSha)}.`)
    if (!linkSha) parts.push('The link does not name a document version.')
    else if (linkSha === recordSha) parts.push(`The report link named sha ${short(linkSha)}, this record's version — not the current one.`)
    else if (linkSha === currentSha) parts.push(`The report link named sha ${short(linkSha)}, the current copy.`)
    else parts.push(`The report link named sha ${short(linkSha)}, which is neither this record's copy nor the current one.`)
  } else if (recordStale === null) {
    tone = 'warn'
    parts.push(`This change was saved into the ${copy} at sha ${short(recordSha)}. No sha-256 is recorded for the current ${copy}, so ACP cannot confirm this record still describes the document as it is now.`)
  } else {
    const st = versionStatement({ linkSha, currentSha, version })
    tone = st.tone; changed = !!st.changed
    parts.push(st.text)
  }
  if (version === 'source' && currentSha) parts.push(SOURCE_SNAPSHOT_CAVEAT)
  const cur = isObj(id.currentArtifact) ? id.currentArtifact : null
  if (!isChange && version === 'source' && cur?.kind === 'corrected' && sha(cur.sha256)) {
    parts.push(`A corrected copy (sha ${short(cur.sha256)}) has been saved since; this finding was recorded against the source document.`)
  }
  return {
    version, copy, linkSha, recordSha, currentSha, recordStale, tone, changed,
    text: parts.join(' '),
    previewSha: recordSha || currentSha,
  }
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

// What the verification of an old record still says about the current copy. Wording states a
// recheck is NEEDED; nothing here queues one, so nothing here says one was queued.
function staleQualifier(v) {
  if (v.recordStale === true) {
    return `This describes sha ${short(v.recordSha)}, not the current copy (sha ${short(v.currentSha)}); it has not been rechecked against the current copy and needs a recheck.`
  }
  if (v.recordStale === null) return 'Whether it still holds for the current copy cannot be established: no sha-256 is recorded for the current copy.'
  return null
}

// A reviewer's decision on a saved change, with its freshness as the server evaluated it
// (report_facts.evaluate_stale) — and never "current" for a record whose copy is not.
function reviewLine(review, v) {
  if (!isObj(review)) return { label: 'No reviewer decision recorded', detail: null, current: null }
  const label = review.verdictLabel || review.verdict || 'unknown'
  const bound = sha(review.artifact_sha256)
  const on = bound ? ` (recorded against sha ${short(bound)})` : ''
  if (v.recordStale === true) {
    return { label: `${label}${on}`, current: false, detail: `not current — the document changed since this record was made${review.staleReason && review.stale === true ? `; ${review.staleReason}` : ''}. It needs a recheck against the current copy.` }
  }
  if (review.stale === true) return { label: `${label}${on}`, current: false, detail: `not current — ${review.staleReason || 'the decision no longer matches this change or copy'}` }
  if (review.stale === false) return { label: `${label}${on}`, current: true, detail: review.staleReason || 'bound to the current copy and the current change' }
  return { label: `${label}${on}`, current: null, detail: `freshness unknown — ${review.staleReason || 'the reviewed copy is not recorded'}` }
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
      {target.findingId && <Row label="Finding id">{target.findingId}</Row>}
      {target.changeId && <Row label="Saved change id">{target.changeId}</Row>}
      {target.sha256 && <Row label="Version in link">sha {short(target.sha256)}{target.version ? ` (${target.version})` : ''}</Row>}
    </dl>
  )
}

// An in-app link to another evidence view. A plain click stays in the app (App.jsx pushes the URL
// and swaps the target); a modified click, or no handler, is an ordinary link.
function EvidenceLink({ href, onOpen, children }) {
  if (!href) return null
  const click = (e) => {
    if (!onOpen || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    onOpen(href)
  }
  return <a href={href} onClick={click}>{children}</a>
}

const STATE_LABELS = {
  assessed: 'Assessed',
  partial: 'Partially assessed',
  error: 'Assessment failed',
  not_assessed: 'Not assessed',
}
const count = (v) => (typeof v === 'number' && Number.isFinite(v) ? String(v) : NOT_RECORDED)
const criterionText = (r) => {
  const sc = scOfValue(r?.sc) || scOfValue(r?.ruleId)
  return sc ? `WCAG ${sc}${criterionName(sc) ? ` · ${criterionName(sc)}` : ''}` : (r?.ruleId || 'Criterion not recorded')
}

// ── the whole document (fileEvidenceHref) ─────────────────────────────────────────────────────
function FileEvidence({ facts, target, onOpen }) {
  const linked = attachEvidenceLinks(facts)
  const id = isObj(facts.identity) ? facts.identity : {}
  const a = isObj(facts.assessment) ? facts.assessment : {}
  const acc = isObj(facts.accounting) ? facts.accounting : {}
  const findings = Array.isArray(linked.findings) ? linked.findings.filter(isObj) : null
  const changes = Array.isArray(linked.savedChanges) ? linked.savedChanges.filter(isObj) : null
  const counted = a.state === 'assessed' || a.state === 'partial'
  const cur = isObj(id.currentArtifact) ? id.currentArtifact : null
  const curSha = sha(cur?.sha256)
  const version = target.version || (cur?.kind === 'corrected' ? 'corrected' : 'source')
  const currentForLink = version === 'corrected' ? sha(id.correctedSha256) : sha(id.sourceSha256)
  const ver = target.sha256 ? versionStatement({ linkSha: target.sha256, currentSha: currentForLink, version }) : null
  const fmt = fmtOfFile(target.file)

  let artifact
  if (cur?.kind === 'corrected' && curSha) artifact = `Corrected copy, sha ${short(curSha)}`
  else if (cur?.kind === 'source' && curSha) artifact = `Source document as ACP last read it, sha ${short(curSha)}`
  else if (cur?.kind === 'source' && id.sourceChecksum) {
    const kind = ['md5', 'quickxorhash'].includes(id.sourceChecksumKind) ? id.sourceChecksumKind : 'checksum'
    artifact = `Source document as ACP last read it, ${kind} ${short(id.sourceChecksum)} (no sha-256 recorded)`
  }
  else if (id.remediatedAt) artifact = `${NOT_RECORDED} — the document was changed by remediation, and the saved copy's sha-256 was not recorded`
  else artifact = NOT_RECORDED

  return (
    <article className="evview-file" style={{ marginTop: 12 }}>
      <dl style={{ margin: 0 }}>
        <Row label="File">{target.file}</Row>
        <Row label="Scan">{target.scanId}</Row>
        <Row label="Assessment">
          <b>{STATE_LABELS[a.state] || (a.state ? String(a.state) : NOT_RECORDED)}</b>
          {a.stateReason ? <span className="muted"> — {a.stateReason}</span> : null}
        </Row>
        <Row label="Assessed at">{a.assessedAt || NOT_RECORDED}</Row>
        <Row label="Findings recorded">
          {counted ? count(a.findingsTotal) : `Not known — ${a.stateReason || 'the document was not assessed'}`}
          {counted && a.findingsComplete === false && findings ? <span className="muted"> (the list below shows {findings.length})</span> : null}
        </Row>
        <Row label="Open findings">
          {count(acc.findingsOpen)}{acc.accountingReason ? <span className="muted"> — {acc.accountingReason}</span> : null}
        </Row>
        <Row label="Saved changes">
          {count(facts.savedChangesTotal)}
          {typeof facts.savedChangesTotal === 'number'
            ? <span className="muted"> ({count(acc.savedChangesVerified)} verified, {count(acc.savedChangesUnverified)} not verified)</span>
            : null}
          {facts.savedChangesComplete === false ? <span className="muted"> — the saved-change list is incomplete</span> : null}
        </Row>
        <Row label="Current document">{artifact}</Row>
        {cur?.kind !== 'corrected' && (id.sourceSha256 || id.sourceChecksum) ? (
          <Row label="Source version"><span className="muted">{SOURCE_SNAPSHOT_CAVEAT}</span></Row>
        ) : null}
        {ver && (
          <Row label="Version">
            <span className={ver.tone === 'warn' ? 'evview-version-warn' : undefined}
                  style={ver.tone === 'warn' ? { color: 'var(--warn-fg, #8a4b00)' } : undefined}>{ver.text}</span>
          </Row>
        )}
      </dl>

      <section aria-label="Recorded findings" style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>Findings</h2>
        {!counted
          ? <p className="muted">No findings are listed: {a.stateReason || 'the document was not assessed'}. That is not the same as having none.</p>
          : !findings ? <p className="muted">The finding list was not returned.</p>
            : findings.length === 0 ? <p className="muted">The assessment recorded no findings for this document.</p>
              : (
                <ul className="evview-list" style={{ margin: 0, paddingLeft: 18 }}>
                  {findings.map((f, i) => (
                    <li key={f.id || `f${i}`} style={{ margin: '4px 0' }}>
                      <span>{criterionText(f)}</span>
                      {f.detail ? <span> — {f.detail}</span> : null}
                      <span className="muted"> · {locationLabel(locationOf({ location: f.location }, { fmt }))}</span>
                      {' '}<EvidenceLink href={f.location?.href} onOpen={onOpen}>Open this finding</EvidenceLink>
                    </li>
                  ))}
                </ul>
              )}
      </section>

      <section aria-label="Saved changes" style={{ marginTop: 16 }}>
        <h2 style={{ fontSize: 16, margin: '0 0 6px' }}>Saved changes</h2>
        {!changes ? <p className="muted">The saved-change list was not returned.</p>
          : changes.length === 0
            ? <p className="muted">{facts.savedChangesComplete === false ? 'None were returned, and the list is incomplete.' : 'No saved changes are recorded for this document.'}</p>
            : (
              <ul className="evview-list" style={{ margin: 0, paddingLeft: 18 }}>
                {changes.map((ch, i) => (
                  <li key={ch.id || `c${i}`} style={{ margin: '4px 0' }}>
                    <span>{criterionText(ch)}</span>
                    {ch.after ? <span> — <q>{ch.after}</q></span> : null}
                    <span className="muted"> · {changeVerification(ch).label} · {locationText(locationOf({ location: ch.location }, { fmt }), ch)}</span>
                    {' '}<EvidenceLink href={ch.location?.href} onOpen={onOpen}>Open this change</EvidenceLink>
                  </li>
                ))}
              </ul>
            )}
      </section>
    </article>
  )
}

export default function FindingEvidenceViewer({ target, onClose, onOpen = null }) {
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

  const wholeFile = isFileTarget(target)
  const back = (
    <button type="button" className="btn" onClick={onClose}>← Back to ACP</button>
  )
  const frame = (body) => (
    <main className="evview" aria-labelledby="evview-title" style={{ maxWidth: 880, margin: '24px auto', padding: '0 16px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
        <h1 id="evview-title" style={{ fontSize: 20, margin: 0 }}>{wholeFile ? 'Document evidence' : target?.changeId ? 'Saved change evidence' : 'Finding evidence'}</h1>
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
  const id = isObj(facts.identity) ? facts.identity : {}
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
  if (wholeFile) return frame(<FileEvidence facts={facts} target={target} onOpen={onOpen} />)

  const isChange = !!target.changeId
  const list = isChange ? facts.savedChanges : facts.findings
  const record = Array.isArray(list) ? list.find((r) => r && r.id === (isChange ? target.changeId : target.findingId)) : null
  const docHref = fileEvidenceHref({ scanId, file })
  const toDocument = (
    <p style={{ marginTop: 14 }}>
      <EvidenceLink href={docHref} onOpen={onOpen}>All recorded evidence for this document</EvidenceLink>
    </p>
  )
  if (!record) {
    return frame(
      <section role="alert" className="evview-missing" style={{ marginTop: 12 }}>
        <p>{RECORD_MISSING}</p>
        <TargetIdentity target={target} />
        {isChange && facts.savedChangesComplete === false && (
          <p className="muted">The saved-change list for this document is itself incomplete, so the record may exist and not have been returned.</p>
        )}
        {toDocument}
      </section>,
    )
  }

  const fmt = fmtOfFile(file)
  const legacyNote = isChange && isLegacyNoteLocation(record)
  const loc = locationOf({ location: record.location }, { fmt })
    || (isChange && record.locator ? locationOf({ locator: record.locator }, { fmt }) : null)
  const sc = scOfValue(record.sc) || scOfValue(record.ruleId)
  const cname = sc ? criterionName(sc) : null
  const verification = isChange ? changeVerification(record) : findingVerification(record)
  const v = versionFacts({ target, record, identity: id, isChange })
  const qualifier = isChange ? staleQualifier(v) : null
  const review = isChange ? reviewLine(isObj(facts.reviews) ? facts.reviews[record.id] : null, v) : null
  const plan = previewPlan(fmt, loc, { locationSource: legacyNote ? 'legacy_note' : null })
  const recordCopyCaption = v.recordStale === true
    ? `Corrected copy this change was saved into — not the current copy — ${plan.unit} ${plan.page} (sha ${short(v.previewSha)})`
    : `${v.version === 'corrected' ? 'Corrected copy' : 'Original document'}, ${plan.unit} ${plan.page} (sha ${short(v.previewSha)})`

  return frame(
    <article className="evview-record" style={{ marginTop: 12 }}>
      <dl style={{ margin: 0 }}>
        <Row label="File">{file}</Row>
        <Row label="Scan">{scanId}</Row>
        <Row label="Criterion">{sc ? `WCAG ${sc}${cname ? ` · ${cname}` : ''}` : (record.ruleId || NOT_RECORDED)}{record.ruleId && sc && record.ruleId !== sc ? <span className="muted"> · rule {record.ruleId}</span> : null}</Row>
        {!isChange && <Row label="Severity">{record.severity ? String(record.severity).toLowerCase() : NOT_RECORDED}</Row>}
        {!isChange && <Row label="Detail">{record.detail || NOT_RECORDED}</Row>}
        {isChange && <Row label="Before">{record.before ? <q>{record.before}</q> : <span className="muted">empty</span>}</Row>}
        {isChange && <Row label="After">{record.after ? <q>{record.after}</q> : <span className="muted">empty</span>}</Row>}
        {isChange && record.note && <Row label="Note">{record.note}</Row>}
        <Row label="Location">
          {locationLabel(loc)}
          {legacyNote && loc?.label && !labelNamesNote(loc.label) ? <span className="muted evview-legacy-location"> ({LEGACY_NOTE_LOCATION})</span> : null}
        </Row>
        {!isChange && <Row label="Recommended action">{record.recommendedAction || NOT_RECORDED}</Row>}
        <Row label="Verification">
          <b>{verification.label}</b>{verification.detail ? <span className="muted"> — {verification.detail}</span> : null}
          {qualifier ? <span className="evview-version-warn" style={{ color: 'var(--warn-fg, #8a4b00)' }}> {qualifier}</span> : null}
        </Row>
        {isChange && (
          <Row label="Reviewer decision">
            <b>{review.label}</b>{review.detail ? <span className={review.current === true ? 'muted' : 'evview-version-warn'}> — {review.detail}</span> : null}
          </Row>
        )}
        <Row label="Version">
          <span className={v.tone === 'warn' ? 'evview-version-warn' : undefined}
                style={v.tone === 'warn' ? { color: 'var(--warn-fg, #8a4b00)' } : undefined}>{v.text}</span>
        </Row>
      </dl>
      <section aria-label="Page preview" style={{ marginTop: 14 }}>
        {plan.reason
          ? <PreviewUnavailable reason={plan.reason} />
          : !v.previewSha
            ? <PreviewUnavailable reason={`no sha-256 is recorded for this version of the document, so its exact ${plan.unit} cannot be requested`} />
            : (
              <>
                <p className="muted" style={{ fontSize: 12, margin: '0 0 6px' }}>{recordCopyCaption}</p>
                <PagePreview scanId={scanId} file={file} page={plan.page} sha256={v.previewSha} unit={plan.unit} maxHeight={520} />
              </>
            )}
      </section>
      {toDocument}
    </article>,
  )
}
