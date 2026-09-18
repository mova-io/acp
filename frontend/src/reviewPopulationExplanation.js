// One explanation of the review population, so every sentence about "what is left" reads the
// same numbers.
//
// THE BUG THIS EXISTS TO PREVENT, seen in the production case (synthetic reproduction
// in reviewPopulationExplanation.test.jsx). One DOCX, five review tasks, one unresolved finding.
// The same screen said, at once:
//
//     Unresolved findings 1 · Review workspace 0 · Needs your review 0
//     "All clear — nothing needs your review."
//     4 of 5 reviewed  ·  3 of 5 actions complete / 2 awaiting outcome
//     Needs your input 0 · Processing 0 · Status checks 2 · Results 3
//
// Every number was true of something. "All clear" was gated on ONE of them — human-pending rows —
// while two tasks sat in Status checks and the server's finding ledger still listed 1.1.1 as
// awaiting review. Nothing on screen named that finding, said why it was left, or opened it. And
// the two progress lines used two definitions of "done" over the same five rows.
//
// So this module owns four rules, and nothing else may state them differently:
//   1. "All clear" only when NO task remains in ANY tab, no saved change is awaiting its outcome,
//      the server reports no unresolved finding, and no document was unreadable.
//   2. Findings and tasks are different units. A task (one queue row) can cover several findings,
//      and a finding can have no task. The two are never added together.
//   3. Every remaining task and every unresolved finding is NAMED — criterion, name, exact reason,
//      next action, and which tab holds it. A server finding with no queue row is listed as such,
//      never silently dropped.
//   4. Progress has ONE denominator (deduplicated tasks) and one definition, printed with it.
//
// Classification is not decided here. Tabs, terminal results, reasons and progress come from the
// shared model (remediationInboxModel / reviewQueueAction / automaticReviewResponsibility), so the
// explanation cannot disagree with the pills it describes.
import { dedupeReviewTasks, issueLabel, normSc, reviewProgressOf } from './remediationInboxModel.js'
import { matchesAutomaticReview } from './automaticReviewResponsibility.js'
import { remainingReasonOf } from './reviewQueueAction.js'
import { unanalysableCount, unreadableCaveat } from './reviewQueueCopy.js'
import { PLAIN_NAMES } from './rules/index.js'

const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`
const isCount = (n) => Number.isSafeInteger(n) && n >= 0

// Contract tab vocabulary (C3 remainingReasonOf) ↔ the keys ReviewQueueTabs / matchesAutomaticReview use.
export const QUEUE_TAB_KEY = { review: 'review', processing: 'awaiting-validation', 'status-check': 'status-check', results: 'completed' }
const CONTRACT_TAB = Object.fromEntries(Object.entries(QUEUE_TAB_KEY).map(([k, v]) => [v, k]))

export function tabLabelOf(tab, automatic = false) {
  const key = CONTRACT_TAB[tab] || tab
  if (key === 'review') return automatic ? 'Needs your input' : 'Needs review'
  if (key === 'processing') return 'Processing'
  if (key === 'status-check') return 'Status checks'
  if (key === 'results') return 'Results'
  return null
}

// The tab a row is LISTED in — the pill's own predicate, so a count here is the pill's count.
function tabOfRow(row, decisions, automatic) {
  const order = automatic ? ['review', 'awaiting-validation', 'status-check', 'completed'] : ['review', 'awaiting-validation', 'completed']
  const key = order.find(k => matchesAutomaticReview(row, k, decisions, automatic))
  return key ? CONTRACT_TAB[key] : null
}

const idOf = (row) => row?.id ?? row?._raw?.id
const sameId = (a, b) => a != null && b != null && String(a) === String(b)
const criterionOf = (row) => normSc(row?.rule_id ?? row?.ruleId ?? row?._raw?.rule_id ?? row?.sc ?? '') || null
const nameOf = (criterion, explicit, row) => explicit || row?._raw?.rule_name || row?.ruleName
  || (criterion && PLAIN_NAMES[criterion]) || (row ? issueLabel(row) : null) || null

// The amber "Unresolved findings" tile's buckets (WorkflowOutcomeTiles FINDING 'attention'). Kept
// identical so the explanation's finding count is the number the tile prints.
const ATTENTION_BUCKETS = ['awaiting_review', 'unchanged_no_fix', 'failed']
const KNOWN_BUCKETS = new Set(['awaiting_recorded_outcome', 'not_in_remediation_breakdown', 'approved_pending_verification',
  'approved_awaiting_verification', 'resolved_verified', ...ATTENTION_BUCKETS, 'excluded', 'excluded_by_policy', 'superseded'])

/**
 * The server-side inputs from a stage snapshot's `domain_reconciliation` (C2). `findingTotal` is
 * the unresolved-finding count the tile shows — null when the partition does not balance, because
 * an unbalanced count is not one to reason from.
 *
 * `findingLedger` says whether the ledger as a WHOLE can be trusted to prove that nothing remains:
 * 'consistent' only when the buckets add up to the total (WorkflowOutcomeTiles' `balanced`) and no
 * integrity signal says otherwise — the same signals stageAccountingModel treats as inconsistent
 * (domain.exact === false, recorded violations, and, when the snapshot is passed, integrity.ok ===
 * false / reconciliation.exact === false / reconciliation_status 'inconsistent'). An unresolved
 * subset of 0 inside an unbalanced ledger (total 9, buckets summing to 4) proves nothing about the
 * other 5, so it cannot support "All clear". 'unknown' when there is no ledger at all.
 */
export function findingInputsFrom(domain, snapshot = null) {
  if (!domain || domain.available === false || !domain.buckets) {
    return { unresolvedFindings: null, unresolvedFindingsTruncated: false, unresolvedFindingsTotal: null, findingTotal: null,
      findingLedger: 'unknown' }
  }
  const buckets = domain.buckets
  const values = Object.values(buckets)
  const balanced = isCount(domain.total) && values.every(isCount) && values.reduce((a, b) => a + b, 0) === domain.total
  const integrityBroken = domain.exact === false || (Array.isArray(domain.violations) && domain.violations.length > 0)
    || snapshot?.integrity?.ok === false || snapshot?.reconciliation?.exact === false
    || snapshot?.reconciliation_status === 'inconsistent'
  const findingTotal = balanced
    ? ATTENTION_BUCKETS.reduce((sum, key) => sum + (buckets[key] || 0), 0)
      + Object.keys(buckets).filter(key => !KNOWN_BUCKETS.has(key)).reduce((sum, key) => sum + buckets[key], 0)
    : null
  return {
    unresolvedFindings: Array.isArray(domain.unresolved_findings) ? domain.unresolved_findings : null,
    unresolvedFindingsTruncated: domain.unresolved_findings_truncated === true,
    // The uncapped length of the unresolved_findings list (C2). It can exceed the tile, because the
    // list also carries approved-pending-verification findings, which the tile files under Processing.
    unresolvedFindingsTotal: isCount(domain.unresolved_findings_total) ? domain.unresolved_findings_total : null,
    findingTotal,
    findingLedger: balanced && !integrityBroken ? 'consistent' : 'inconsistent',
  }
}

export const FINDING_NOTE = 'Findings and review tasks are different units. One task can cover several findings, '
  + 'and a finding can remain without a task. The two counts are never added together.'

export const PROGRESS_DEFINITION = 'Same tasks in both counts. Final outcome = confirmed by a fresh scan of the corrected copy, '
  + 'rejected or closed by a decision, or target replaced by another confirmed change; a saved change not yet re-checked is awaiting outcome.'

/**
 * @param rows              the review queue rows (the inbox queue; deduplicated here, idempotently)
 * @param decisions         this session's decisions, as everywhere else
 * @param automatic         whether automatic approval is on (selects the four-tab layout)
 * @param unresolvedFindings  server domain_reconciliation.unresolved_findings, or null when not provided
 * @param unresolvedFindingsTruncated  server said the list was capped
 * @param unresolvedFindingsTotal  server's uncapped count of the unresolved_findings list, or null
 * @param findingTotal      server unresolved-finding count (the "Unresolved findings" tile), or null
 * @param findingLedger     'consistent' | 'inconsistent' | 'unknown' (findingInputsFrom), or null when not supplied
 * @param files             scan file records, for the unreadable-document caveat
 */
export function explainReviewPopulation({ rows = [], decisions = {}, automatic = false, unresolvedFindings = null,
  unresolvedFindingsTruncated = false, unresolvedFindingsTotal = null, findingTotal = null, findingLedger = null, filter = null, files = [] } = {}) {
  const tasks = dedupeReviewTasks(rows || [])
  const tabs = new Map()
  const counts = { review: 0, processing: 0, 'status-check': 0, results: 0 }
  for (const row of tasks) {
    const tab = tabOfRow(row, decisions, automatic)
    tabs.set(row, tab)
    if (tab) counts[tab] += 1
  }

  // Server findings, matched to a task only by the id the server itself recorded (review_item_id).
  // A shared file + criterion is NOT treated as the same object — two tasks can share both — so it
  // is offered as a related item, never as a match.
  const listed = Array.isArray(unresolvedFindings) ? unresolvedFindings.filter(Boolean) : null
  const findingsByItem = new Map()
  const unmatchedFindings = []
  for (const finding of listed || []) {
    const criterion = normSc(finding.rule_id ?? finding.criterion ?? '') || null
    const row = finding.review_item_id != null ? tasks.find(r => sameId(idOf(r), finding.review_item_id)) : null
    const tab = row ? tabs.get(row) : null
    if (row && tab && tab !== 'results') {
      const list = findingsByItem.get(row) || []
      list.push(finding.finding_id ?? null)
      findingsByItem.set(row, list)
      continue
    }
    const entry = { findingId: finding.finding_id ?? null, file: finding.file || row?.file || '', criterion,
      name: nameOf(criterion, finding.rule_name, row), disposition: finding.disposition || null }
    if (row) {
      // The ledger and the queue disagree: the task has a final result, the finding does not.
      Object.assign(entry, { matchedItemId: idOf(row), matchedTab: tab || null,
        reason: `The server still lists this finding as ${dispositionText(entry.disposition)}, while its review task already has a recorded result. The two records disagree.`,
        nextAction: 'Open the task to check its saved evidence. A fresh assessment of the corrected copy settles which record is current.' })
    } else {
      const related = tasks.filter(r => criterion && criterionOf(r) === criterion && (!entry.file || r.file === entry.file))
      if (related.length === 1) Object.assign(entry, { relatedItemId: idOf(related[0]), relatedTab: tabs.get(related[0]) || null })
      Object.assign(entry, {
        reason: `The server lists this finding as ${dispositionText(entry.disposition)}, and no review task in this queue is linked to it.`,
        nextAction: related.length === 1
          ? 'A task for the same criterion in this document may relate to it; open that task to check. Otherwise the finding needs a manual repair or a new assessment.'
          : 'No task can resolve it from here. Repair it in the source document or re-run the assessment, then check the result.',
      })
    }
    unmatchedFindings.push(entry)
  }

  let unlistedFindings = null
  // The best count of the whole list: the server's uncapped total, else the tile's figure.
  const listTotal = isCount(unresolvedFindingsTotal) ? unresolvedFindingsTotal : isCount(findingTotal) ? findingTotal : null
  if (listed === null && listTotal > 0) {
    unlistedFindings = { count: listTotal, reason: 'not-provided',
      text: `The server reports ${plural(listTotal, 'unresolved finding')} but did not itemise ${listTotal === 1 ? 'it' : 'them'}. ${listTotal === 1 ? 'It is' : 'They are'} not shown individually here.` }
  } else if (listed !== null && unresolvedFindingsTruncated) {
    const extra = listTotal > listed.length ? listTotal - listed.length : null
    unlistedFindings = { count: extra, reason: 'truncated',
      text: `The server itemised only the first ${plural(listed.length, 'unresolved finding')}${extra ? `; ${plural(extra, 'more is', 'more are')} not listed here` : '; the rest are not listed here'}.` }
  } else if (listed !== null && listTotal > listed.length) {
    const extra = listTotal - listed.length
    unlistedFindings = { count: extra, reason: 'count-mismatch',
      text: `The server counts ${plural(listTotal, 'unresolved finding')} but itemised ${listed.length.toLocaleString()}; ${plural(extra, 'is', 'are')} not listed here.` }
  }

  const remaining = []
  for (const row of tasks) {
    const tab = tabs.get(row)
    if (tab === 'results') continue
    const explained = remainingReasonOf(row, decisions, automatic) || {}
    const criterion = criterionOf(row)
    remaining.push({
      itemId: idOf(row), file: row.file || '', criterion, name: nameOf(criterion, null, row),
      tab: tab || explained.tab || null, queueTab: QUEUE_TAB_KEY[tab || explained.tab] || null,
      tabLabel: tabLabelOf(tab || explained.tab, automatic) || explained.tabLabel || null,
      reason: explained.reason || 'ACP has not recorded why this task is still open.',
      nextAction: explained.nextAction || 'Open the task to check its saved evidence.',
      findingIds: findingsByItem.get(row) || [],
    })
  }

  const progress = progressOf(tasks, decisions)
  const unreadable = unanalysableCount(files)
  // An empty list is not a count of zero on its own (see findingTotalsKnown below); only a non-empty
  // list proves at least that many.
  const serverFindings = isCount(findingTotal) ? findingTotal : isCount(unresolvedFindingsTotal) ? unresolvedFindingsTotal
    : listed?.length ? listed.length : null
  // "All clear" needs KNOWN-zero finding evidence, not merely no evidence of findings. Known zero is
  // the tile's balanced count at 0 (findingInputsFrom returns null when the buckets do not add up) or
  // the server's uncapped list total at 0. A missing domain, a missing list or unbalanced buckets
  // leave the totals UNKNOWN — and an unknown is not a zero, however empty the queue looks.
  // An inconsistent ledger can never prove zero. The tile's count is only non-null when its buckets
  // balance, so findingTotal === 0 suffices unless an integrity signal contradicts it; the list total
  // alone needs the ledger to be affirmatively consistent.
  const findingTotalsState = findingLedger === 'inconsistent' ? 'inconsistent'
    : findingTotal === 0 || (unresolvedFindingsTotal === 0 && findingLedger === 'consistent') ? 'known' : 'unknown'
  const findingTotalsKnown = findingTotalsState === 'known'
  const allClear = remaining.length === 0 && counts.review === 0 && counts.processing === 0 && counts['status-check'] === 0
    && progress.awaitingOutcome === 0 && unmatchedFindings.length === 0 && !unlistedFindings
    && !(serverFindings > 0) && !(unresolvedFindingsTotal > 0) && unreadable === 0 && findingTotalsKnown

  const explanation = {
    taskTotal: tasks.length, humanCount: counts.review, processingCount: counts.processing,
    statusCheckCount: counts['status-check'], resultsCount: counts.results,
    findingTotal: serverFindings, findingTotalsKnown, findingTotalsState, findingNote: FINDING_NOTE,
    remaining, unmatchedFindings, unlistedFindings, allClear, automatic, progress,
  }
  explanation.headline = headlineOf(explanation, files)
  explanation.emptyFilterLine = (f = filter) => emptyFilterLineOf(explanation, f)
  return explanation
}

function dispositionText(disposition) {
  return ({ awaiting_review: 'awaiting review', approved_pending_verification: 'approved and awaiting verification',
    unchanged_no_fix: 'unchanged with no fix available', remediation_failed: 'failed to remediate' })[disposition]
    || (disposition ? `"${String(disposition).replace(/_/g, ' ')}"` : 'unresolved')
}

/**
 * The progress wording, from reviewProgressOf's counts. Shared by every progress line (the
 * explainer, WorkspaceProgress, and the Review queue header) so "done" has one definition: both
 * labels are out of the same N, and the second line's three parts add up to that N.
 */
export function progressLabelsOf({ total = 0, decided = 0, finished = 0, awaitingOutcome = Math.max(0, decided - finished),
  open = Math.max(0, total - decided) } = {}) {
  const task = total === 1 ? 'task' : 'tasks'
  const has = n => (n === 1 || total === 1 ? 'has' : 'have')
  return { total, decided, finished, awaitingOutcome, open,
    decidedLabel: `${decided} of ${total} ${task} ${has(decided)} a recorded decision`,
    finishedLabel: `${finished} of ${total} ${task} ${has(finished)} a final outcome · ${awaitingOutcome} awaiting outcome · ${open} without a decision`,
    definition: PROGRESS_DEFINITION }
}

const progressOf = (tasks, decisions) => progressLabelsOf(reviewProgressOf(tasks, decisions) || { total: tasks.length })

// Remaining work outside the given tab, as a clause: "2 status checks and 1 unresolved finding".
function otherWork(e, except = null) {
  const parts = []
  if (except !== 'review' && e.humanCount) parts.push(`${plural(e.humanCount, 'task')} needing your ${e.automatic ? 'input' : 'review'}`)
  if (except !== 'processing' && e.processingCount) parts.push(`${plural(e.processingCount, 'task')} in Processing`)
  if (except !== 'status-check' && e.statusCheckCount) parts.push(plural(e.statusCheckCount, 'status check'))
  if (except !== 'results' && e.progress.awaitingOutcome && !e.processingCount && !e.statusCheckCount) {
    parts.push(`${plural(e.progress.awaitingOutcome, 'saved change')} awaiting verification`)
  }
  const unlinked = e.unmatchedFindings.length + (e.unlistedFindings?.count || 0)
  if (unlinked) parts.push(`${plural(unlinked, 'unresolved finding')} without a task in this queue`)
  else if (e.unlistedFindings) parts.push('unresolved findings the server did not itemise')
  return parts
}
export const UNKNOWN_TOTALS = 'No open review tasks; current finding totals unavailable.'
export const INCONSISTENT_TOTALS = 'No open review tasks; current finding totals are inconsistent, so ACP cannot confirm nothing remains.'
// Nothing left in the QUEUE and nothing the server itemised — the only open question is the totals.
const tasksSettled = (e) => e.remaining.length === 0 && !e.humanCount && !e.processingCount && !e.statusCheckCount
  && !e.progress.awaitingOutcome && !e.unmatchedFindings.length && !e.unlistedFindings && !(e.findingTotal > 0)
const joinParts = (parts) => parts.length <= 1 ? parts.join('') : `${parts.slice(0, -1).join(', ')} and ${parts.at(-1)}`

function headlineOf(e, files) {
  const caveat = unreadableCaveat(files)
  if (e.allClear) return 'All clear — nothing needs your review.'
  // Every task settled, but the finding totals cannot be confirmed: say exactly that, never "clear".
  if (tasksSettled(e) && !e.findingTotalsKnown) {
    const line = e.findingTotalsState === 'inconsistent' ? INCONSISTENT_TOTALS : UNKNOWN_TOTALS
    return caveat ? `${line} ${caveat}` : line
  }
  const sentences = []
  if (e.humanCount) {
    sentences.push(`${plural(e.humanCount, 'task needs', 'tasks need')} your ${e.automatic ? 'input' : 'review'} — see ${tabLabelOf('review', e.automatic)}.`)
  } else {
    sentences.push('Nothing needs your decision.')
  }
  if (e.statusCheckCount) sentences.push(`${plural(e.statusCheckCount, 'item is a status check', 'items are status checks')} ACP is tracking — see Status checks.`)
  if (e.processingCount) sentences.push(`${plural(e.processingCount, 'item is', 'items are')} being processed by ACP — see Processing.`)
  // Awaiting outcome that no tab above already carries: a saved change filed under Results.
  if (e.progress.awaitingOutcome && !e.statusCheckCount && !e.processingCount) {
    sentences.push(`${plural(e.progress.awaitingOutcome, 'saved change is', 'saved changes are')} awaiting verification — see Results.`)
  }
  const unlinked = e.unmatchedFindings.length
  if (unlinked) sentences.push(`The server still reports ${plural(unlinked, 'unresolved finding')} that no open task covers — see Remaining findings.`)
  if (e.unlistedFindings) sentences.push(e.unlistedFindings.text)
  else if (!unlinked && e.findingTotal > 0 && !e.humanCount && !e.statusCheckCount && !e.processingCount) {
    sentences.push(`The server still reports ${plural(e.findingTotal, 'unresolved finding')} — see Remaining findings.`)
  }
  if (e.findingTotalsState === 'inconsistent') sentences.push('Current finding totals are inconsistent.')
  else if (!e.findingTotalsKnown && !e.unlistedFindings && !unlinked && !(e.findingTotal > 0)) {
    sentences.push('Current finding totals are unavailable.')
  }
  if (caveat) sentences.push(caveat)
  return sentences.join(' ')
}

// The line for a FILTERED view that shows nothing. It describes the filter, and says what remains
// elsewhere; it never describes an empty filter as an empty queue.
function emptyFilterLineOf(e, filter) {
  if (!filter || filter === 'all') return null
  const tab = CONTRACT_TAB[filter] || (filter in QUEUE_TAB_KEY ? filter : null)
  // A known tab that holds tasks is not empty; there is nothing for this line to say about it.
  const held = { review: e.humanCount, processing: e.processingCount, 'status-check': e.statusCheckCount, results: e.resultsCount }
  if (tab && held[tab] > 0) return null
  const label = tab ? tabLabelOf(tab, e.automatic) : null
  const lead = label ? `No tasks in ${label}.` : 'No tasks match this filter.'
  const others = otherWork(e, tab)
  if (others.length) return `${lead} This is a filtered view — ${joinParts(others)} still ${others.length === 1 && /^1 /.test(others[0]) ? 'remains' : 'remain'} in other tabs.`
  if (e.allClear) return `${lead} Nothing else remains in this queue.`
  if (tasksSettled(e) && !e.findingTotalsKnown) {
    return `${lead} No other open review tasks; ${e.findingTotalsState === 'inconsistent'
      ? 'current finding totals are inconsistent, so ACP cannot confirm nothing remains.' : 'current finding totals unavailable.'}`
  }
  return `${lead} This is a filtered view; other tasks may remain.`
}

