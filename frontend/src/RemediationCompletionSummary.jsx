import { integrityAffects } from './remediationSnapshot.js'

const valid = value => Number.isSafeInteger(value) && value >= 0
const number = value => valid(value) ? value.toLocaleString() : 'Unavailable'
const dollars = value => valid(value) ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(value / 1000000) : 'Unavailable'

// A terminal document run is not a claim that all findings were fixed or delivered.
export default function RemediationCompletionSummary({ snapshot, view, exact = false, reviewHref, releaseHref, onInspect }) {
  if (!snapshot?.terminal) return null
  const rec = snapshot.finding_reconciliation || {}
  const verified = exact ? rec.resolved_verified : integrityAffects(snapshot, 'fixes') ? undefined : snapshot.fixes?.verified
  const review = exact ? rec.awaiting_review : integrityAffects(snapshot, 'review') ? undefined : snapshot.review?.items
  const failed = integrityAffects(snapshot, 'documents') ? undefined : snapshot.documents?.failed
  const awaitingRelease = integrityAffects(snapshot, 'delivery') ? undefined : snapshot.delivery?.awaiting_release
  const docs = snapshot.documents || {}
  const cancelled = snapshot.outcome_reasons?.cancelled ?? 0
  const processed = !integrityAffects(snapshot, 'documents') && [snapshot.total_documents, docs.waiting, docs.processing, cancelled].every(valid) && docs.waiting + docs.processing + cancelled <= snapshot.total_documents ? snapshot.total_documents - docs.waiting - docs.processing - cancelled : undefined
  const published = integrityAffects(snapshot, 'delivery') ? undefined : snapshot.delivery?.delivered
  const remaining = exact && !integrityAffects(snapshot, 'finding_reconciliation') && valid(rec.assessed) && valid(rec.resolved_verified) && rec.resolved_verified <= rec.assessed ? rec.assessed - rec.resolved_verified : undefined
  const spending = view?.available === true ? view.spending : null
  const stopped = ['failed', 'cancelled'].includes(snapshot.state)
  const next = stopped || failed > 0
    ? 'Inspect unsuccessful work before deciding what to retry.'
    : review > 0 ? 'Open the review workspace; use Status checks for recovery and Needs your input for decisions or manual edits.'
      : awaitingRelease > 0 ? 'Track corrected copies in Release. Covered copies publish automatically when Q3 approved publication.'
        : 'Inspect the recorded outcomes for any remaining or unassessed work.'
  return <section className="wf-completion" aria-label="Remediation run summary">
    <div className="wf-section-head"><h4>{stopped ? 'Run stopped · results retained' : 'Automatic processing finished'}</h4><span>Recorded results</span></div>
    <div className="wf-metrics">
      <div><span>Files processed · attempt finished</span><strong>{number(processed)}</strong></div>
      <div><span>{exact ? 'Fixes verified · findings' : 'Verified changes · all origins'}</span><strong>{number(verified)}</strong></div>
      <div><span>{exact ? 'Findings awaiting resolution' : 'Review queue items · not findings'}</span><strong>{number(review)}</strong></div>
      <div><span>Copies published · delivery confirmed</span><strong>{number(published)}</strong></div>
      <div><span>Remaining findings · not verified</span><strong>{number(remaining)}</strong></div>
      <div><span>Settled AI charges</span><strong>{dollars(spending?.spent_units)}</strong></div>
    </div>
    <p>{valid(failed) && failed > 0 && <span>{number(failed)} failed documents. </span>}{valid(awaitingRelease) && awaitingRelease > 0 && <span>{number(awaitingRelease)} corrected copies awaiting Release. </span>}Processing finished counts attempts. Verified fixes count checked changes. Published copies count confirmed delivery; none of these means every accessibility issue was resolved.</p>
    {(spending?.held_units > 0 || spending?.blocked) && <p className="wf-note">{spending?.held_units > 0 && <>{dollars(spending.held_units)} remains reserved; the final charge is not settled. </>}{spending?.blocked && 'Further AI spending is on hold pending reconciliation.'}</p>}
    <div className="wf-completion-next"><div><strong>Next action</strong><p>{next}</p></div><div className="wf-completion-actions">
      {(stopped || failed > 0) && onInspect ? <button type="button" onClick={onInspect}>Inspect unsuccessful work</button>
        : review > 0 && reviewHref ? <a href={reviewHref}>Open review workspace</a>
          : awaitingRelease > 0 && releaseHref ? <a href={releaseHref}>Track Release</a>
            : onInspect && <button type="button" onClick={onInspect}>Inspect outcomes</button>}
    </div></div>
  </section>
}
