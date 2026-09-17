import { pendingReviewRows } from './remediationCountSummary.js'
import { reviewWorkCategory } from './reviewWorkBreakdown.js'
import { automaticReviewResponsibility } from './automaticReviewResponsibility.js'
import { exclusionReason } from './batchReviewSelection.js'
import { requiresPdfSourceEditing } from './pdfStructuralProposal.js'

const RECOVERABLE_DRAFT = new Set(['Missing proposal', 'Version unavailable — review individually',
  'Stale — refresh and review', 'Invalid structural proposal — refresh suggestions'])
const GROUPS = [
  { key: 'recovery', label: 'Recovery checks', nextStep: 'Check AI activity or the saved failure reason, then refresh the suggestion or retry an eligible operation. Recovery is not guaranteed; no automatic retry is implied, and saved permissions and spending limits still apply.' },
  { key: 'unsupported', label: 'Automatic repair unavailable', nextStep: 'Use the source document or a PDF accessibility editor to repair the structure, then reassess the corrected copy. The recorded suggestion is not an automatic PDF tagging operation.' },
  { key: 'human', label: 'Human decisions or manual edits', nextStep: 'Open Needs your input to review an available suggestion or complete the requested document edit. Check the result against the source before approving.' },
  { key: 'unknown', label: 'Status needs investigation', nextStep: 'Open Status checks and inspect the saved evidence or Live Operations. The current record does not establish an available recovery action or a human decision.' },
]

// Partition pending review items, not scanner findings or documents. Reuse the
// inbox scope so jobs, accepted changes awaiting verification, and results stay out.
export function unresolvedWorkSummary(rows = [], decisions = {}) {
  const pending = pendingReviewRows(rows, decisions)
  const groups = GROUPS.map(group => ({ ...group, count: 0 }))
  for (const row of pending) {
    const decision = decisions[row.id] || decisions[row.file]
    const explicitHuman = row.manual === true || row.rejectedFix || ['assigned', 'deferred'].includes(decision?.state)
    const category = reviewWorkCategory(row, decisions)
    let key
    if (requiresPdfSourceEditing(row)) key = 'unsupported'
    else if (explicitHuman) key = 'human'
    else if (['failed-checks', 'blocked-ai', 'missing-proposals'].includes(category)
      || RECOVERABLE_DRAFT.has(exclusionReason(row, decisions))) key = 'recovery'
    else if (automaticReviewResponsibility(row, decisions) === 'human') key = 'human'
    else key = 'unknown'
    groups.find(group => group.key === key).count += 1
  }
  return { total: pending.length, groups }
}
