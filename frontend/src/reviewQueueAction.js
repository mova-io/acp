import { approvedWriteUnconfirmed, laneOf, workflowStatusOf } from './remediationInboxModel.js'
import { automaticReviewResponsibility } from './automaticReviewResponsibility.js'

// Action ownership is independent of severity, which remains available for priority filters.
export function reviewQueueAction(row, decisions = {}, automatic = false) {
  const status = workflowStatusOf(row, decisions)
  const owner = automaticReviewResponsibility(row, decisions)
  if (status === 'completed') return { key: 'results', label: 'Result recorded' }
  if (owner === 'acp') return { key: 'processing', label: 'Processing' }
  // Approved, not written, not verified. "Review needed" was the wrong ask — the approval is
  // recorded and no further one will be requested — and the row had no control at all behind it.
  // Naming the state as recoverable is what routes the reviewer to the recovery pane.
  if (approvedWriteUnconfirmed(row, decisions)) return { key: 'recover', label: 'Recovery needed' }
  if (status === 'awaiting-validation' || (automatic && owner === 'check')) return { key: 'check', label: 'Status check' }
  const lane = laneOf(row).key
  const manualReason = ['Manual work or no supported proposal writer',
    'This PDF needs headings or table structure added in the original document. ACP cannot apply this draft automatically.']
    .includes(row.automaticDisposition?.reason)
  if (owner === 'human' && manualReason) return { key: 'edit', label: 'Edit needed' }
  if (['manual', 'handoff'].includes(lane)) return { key: 'edit', label: 'Edit needed' }
  if (lane === 'blocked' && owner !== 'human') return { key: 'check', label: 'Status check' }
  return { key: 'review', label: 'Review needed' }
}

export function reviewQueueActions(rows, decisions = {}, automatic = false) {
  const counts = new Map()
  for (const row of rows) {
    const action = reviewQueueAction(row, decisions, automatic)
    const existing = counts.get(action.key)
    counts.set(action.key, { ...action, count: (existing?.count || 0) + 1 })
  }
  return [...counts.values()]
}
