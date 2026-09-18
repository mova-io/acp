import { isTargetReplaced } from './remediationInboxModel.js'

// Assessment findings can be draftable without any proposal having been generated.
// Keep those in Plan until remediation produces a result or an exception to handle.
export function reviewableRemediationItems(rows = [], { files = [], exceptions = null } = {}) {
  const finishedFiles = new Set(files.filter(file => file.remediated_at || file.drive_write_url).map(file => file.file))
  // The run can cover only a subset of the inventory. Use each document's actual
  // outcome, not a global terminal flag; no-copy/manual failures must remain visible.
  for (const group of exceptions?.groups || []) {
    for (const item of group.items || []) {
      if (['review', 'failed', 'completed', 'skipped'].includes(item.outcome)) finishedFiles.add(item.file)
    }
  }
  return rows.filter(row => {
    const status = String(row.status || '').toLowerCase()
    if (status && status !== 'pending') return true
    // Replaced by a verified fix: a recorded result that belongs in Results, whatever its proposal.
    if (isTargetReplaced(row)) return true
    if (row.hasProposal || (row.after != null && row.after !== '') || row.autoApplied || row.applied || row.rejectedFix) return true
    return finishedFiles.has(row.file)
  })
}
