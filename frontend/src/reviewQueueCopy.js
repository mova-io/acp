// What the Review queue says when it has nothing in it.
//
// THE BUG THIS EXISTS TO PREVENT, seen in production on 2026-08-19. A 22-document SharePoint scan
// left one file unreadable — the drawer said "Could not analyse — file unreadable" — while the
// Review queue on the same screen said:
//
//     All clear — nothing needs your review.
//     Nothing needs your review — every fix was applied automatically.
//
// Every count behind those sentences was correct; the HITL queue really was empty. The CLAIM was
// false. A file that could not be read had no fix applied to it, automatically or otherwise: it
// was skipped, and the screen reported skipped as done.
//
// That is this codebase's recurring failure with the numbers swapped out — `_fill_run_aggregate`
// (api/store.py) turning NULL counters into a confident "0% audit-ready", and the 2026-07-30 scope
// incident rendering a count without its boundary. A true number, a false sentence, and the
// reassuring reading is the wrong one.
//
// It is a separate module rather than three strings inside a 1,200-line component because the
// defect was precisely a hardcoded sentence in a ternary: one branch got the caveat and the others
// did not. Here the caveat is appended once, to whatever the base sentence is, so a new branch
// cannot silently ship without it.
//
// NOT fixed here, and deliberately: WHY those files are unreadable. That is an ingest failure in
// the worker, diagnosable only from its logs. Making the label honest is not the same as making
// the failure go away, and shipping the label alone must not be mistaken for shipping the fix.
import { statusOf } from './docStatus.js'

/** Documents that were opened and could not be read. */
export const unanalysableCount = (files) =>
  (files || []).filter((f) => f && statusOf(f) === 'unanalysable').length

const docs = (n) => `${n} document${n === 1 ? '' : 's'}`

/**
 * The caveat, or '' when there is nothing to caveat.
 *
 * Gated on the COUNT, not on the possibility — the same rule the file-type warning follows. A
 * caveat that appears on every scan is one nobody reads, which would cost more than it buys on the
 * scans where it matters.
 */
export function unreadableCaveat(files) {
  const n = unanalysableCount(files)
  if (!n) return ''
  return `${docs(n)} could not be analysed and ${n === 1 ? 'was' : 'were'} not remediated.`
}

/**
 * The sentence shown when the inbox is empty.
 *
 * `totalHitl === 0` means nothing was ever queued; anything else means the queue was worked
 * through. Both are true statements about the QUEUE and both are incomplete about the ESTATE,
 * so the caveat is appended to either.
 */
export function reviewEmptyLine(files, { totalHitl = 0, acted = {} } = {}) {
  const caveat = unreadableCaveat(files)
  const base = totalHitl === 0
    // With unreadable files present the original wording is simply untrue, so it is replaced
    // rather than decorated: "every fix was applied automatically" cannot be walked back by a
    // sentence after it.
    ? (caveat
      ? 'Nothing is queued for review.'
      : 'Nothing needs your review — every fix was applied automatically. '
        + 'Items needing an AI-assisted fix or human sign-off will appear here.')
    : `All reviewed — ${acted.approved || 0} approved, ${acted.rejected || 0} rejected`
      + `${acted.deferred ? `, ${acted.deferred} deferred` : ''}. `
      + 'Verification runs on the approved fixes.'
  return caveat ? `${base} ${caveat}` : base
}

/**
 * The lead under the "Review queue" heading in the zero-findings state.
 *
 * Returns null when there ARE findings — the caller renders its own count sentence then, and a
 * helper that answered anyway would give the page two competing leads.
 */
export function reviewLeadLine(files, reviewCount = 0, explanation = undefined) {
  if (reviewCount > 0) return null
  // THE SECOND BUG, seen in the production case: this line was gated on human-pending rows alone, so
  // it said "All clear" while two tasks sat in Status checks and the server still listed an
  // unresolved finding. With the population explanation (reviewPopulationExplanation.js) the lead
  // is its headline, and "All clear" appears only when that explanation is all clear — no task in
  // any tab, nothing awaiting an outcome, no unresolved finding reported, no unreadable document.
  // The headline carries the unreadable caveat itself.
  if (explanation) return explanation.headline
  // "All clear" is withheld, not qualified. A reader who sees it stops reading, so appending a
  // caveat after it would be read by nobody who needed it.
  return unreadableCaveat(files) || 'All clear — nothing needs your review.'
}
