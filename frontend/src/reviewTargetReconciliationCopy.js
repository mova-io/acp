// Plain-language wording for POST /hitl/queue/{scan}/reconcile-targets (contract C7/C8).
//
// The server answers with machine reason codes (api/review_target_reconciliation.py). A reviewer
// needs to know WHY an item stayed open, not the code. Several codes share one meaning, so they
// map to the same sentence and are counted together. An unknown code is shown as-is rather than
// guessed at, so a new server reason is never reworded into a claim it does not make.
const SAVED_CHECK = 'the saved copy has no complete recorded check to rely on'
const REASONS = {
  nothing_to_reconcile: 'no open item in this scan is one this re-check applies to',
  no_current_batch: 'there is no current remediation run for this document',
  format_unsupported: 'this re-check covers Word documents only',
  bytes_unavailable: 'the saved corrected copy is not available',
  artifact_not_current: 'the saved corrected copy changed since it was checked',
  artifact_changed_before_commit: 'the saved corrected copy changed while this re-check ran',
  unreadable_document: 'the saved corrected copy could not be read',
  source_digest_mismatch: 'the original document changed since it was assessed',
  verification_missing: SAVED_CHECK,
  verification_kind_unknown: SAVED_CHECK,
  verification_digest_mismatch: SAVED_CHECK,
  verification_failed: SAVED_CHECK,
  verification_incomplete: SAVED_CHECK,
  verification_partial: SAVED_CHECK,
  verification_skipped_rules_unknown: SAVED_CHECK,
  verification_errors_unknown: SAVED_CHECK,
  verification_errors: SAVED_CHECK,
  target_remains: 'its target is still in the saved copy',
  no_verified_removing_fix: 'no verified change removed its target',
  remaining_issue_locates_target: 'the saved copy’s check still reports an issue that may be this item',
  unlocated_remaining_issue: 'the saved copy’s check still reports an issue that may be this item',
  criterion_unknown: 'the saved copy’s check did not cover this item’s criterion',
  criterion_not_assessed: 'the saved copy’s check did not cover this item’s criterion',
  target_unknown: 'ACP could not tell exactly which part of the document the item refers to',
  target_unresolved: 'ACP could not tell exactly which part of the document the item refers to',
  finding_not_located: 'ACP could not tell exactly which part of the document the item refers to',
  finding_identity_mismatch: 'ACP could not tell exactly which part of the document the item refers to',
  row_changed_before_commit: 'the item changed while this re-check ran — try again',
  scope_changed: 'the item changed while this re-check ran — try again',
  findings_moved: 'the item changed while this re-check ran — try again',
  finding_already_terminal: 'it already has a recorded outcome',
  deferred_bounded: 'this re-check stopped at its per-request limit — run it again to check the rest',
}

export const reasonText = (code) => REASONS[code] || `recorded reason “${String(code || 'not given').replace(/_/g, ' ')}”`

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

// `unchanged` in the server's answer lists items an EARLIER re-check already closed on the same
// evidence (api/review_target_reconciliation.py returns retired ids there, and a replayed
// retirement lands there too). It never means "still open": items that stay open arrive in
// `skipped`, each with its reason.
const alreadyClosed = (n) => `${plural(n, 'item')} ${n === 1 ? 'was' : 'were'} already closed by an earlier re-check`

/** The one status sentence for a reconcile-targets response. Claims only what the response says. */
export function reconciliationSummary(result) {
  const files = Array.isArray(result?.files) ? result.files : []
  const closed = Number.isSafeInteger(result?.superseded_count) ? result.superseded_count
    : files.reduce((sum, f) => sum + (Array.isArray(f?.superseded) ? f.superseded.length : 0), 0)
  const unchanged = files.reduce((sum, f) => sum + (Array.isArray(f?.unchanged) ? f.unchanged.length : 0), 0)
  const counts = new Map()
  for (const f of files) for (const s of (Array.isArray(f?.skipped) ? f.skipped : [])) {
    const text = reasonText(s?.reason)
    counts.set(text, (counts.get(text) || 0) + (s?.item_id != null ? 1 : 0))
  }
  const reasons = [...counts].map(([text, n]) => (n > 1 ? `${text} (${n} items)` : text)).join('; ')
  const lead = closed > 0
    ? `${plural(closed, 'item')} closed: another verified change replaced ${closed === 1 ? 'its' : 'their'} target.`
    : 'Nothing changed'
  if (closed > 0) {
    const rest = reasons ? ` Not changed: ${reasons}.` : ''
    return lead + rest + (unchanged ? ` ${alreadyClosed(unchanged)}.` : '')
  }
  if (unchanged) return `${lead}: ${alreadyClosed(unchanged)}.${reasons ? ` Not changed: ${reasons}.` : ''}`
  if (reasons) return `${lead}: ${reasons}.`
  return `${lead}: no open item in this scan is one this re-check applies to.`
}
