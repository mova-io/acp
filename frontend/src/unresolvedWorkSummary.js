import { pendingReviewRows } from './remediationCountSummary.js'
import { reviewWorkCategory } from './reviewWorkBreakdown.js'
import { automaticReviewResponsibility } from './automaticReviewResponsibility.js'
import { approvedWriteUnconfirmed } from './remediationInboxModel.js'
import { exclusionReason } from './batchReviewSelection.js'
import { requiresPdfSourceEditing } from './pdfStructuralProposal.js'

const RECOVERABLE_DRAFT = new Set(['Missing proposal', 'Version unavailable — review individually',
  'Stale — refresh and review', 'Invalid structural proposal — refresh suggestions'])
const GROUPS = [
  { key: 'recovery', label: 'Potential recovery', nextStep: 'Check AI activity for the blocker, then refresh or retry when eligible.' },
  { key: 'unsupported', label: 'Automatic repair unavailable', nextStep: 'Repair the structure in the source document or a PDF accessibility editor, then reassess the corrected copy.' },
  { key: 'human', label: 'Human decisions or manual edits', nextStep: 'Review the suggestion or edit the source document.' },
  { key: 'unknown', label: 'Status needs investigation', nextStep: 'Inspect the saved evidence or Live Operations to identify the next step.' },
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
    // An approval that was recorded and never written is recoverable work, not a human decision.
    // It was being counted under "Human decisions or manual edits" while the same screen told the
    // reviewer no further approval was needed — the contradiction the summary photographed.
    else if (approvedWriteUnconfirmed(row, decisions)) key = 'recovery'
    else if (explicitHuman) key = 'human'
    else if (['failed-checks', 'blocked-ai', 'missing-proposals'].includes(category)
      || RECOVERABLE_DRAFT.has(exclusionReason(row, decisions))) key = 'recovery'
    else if (automaticReviewResponsibility(row, decisions) === 'human') key = 'human'
    else key = 'unknown'
    groups.find(group => group.key === key).count += 1
  }
  return { total: pending.length, groups }
}
