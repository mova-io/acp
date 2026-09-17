import { workflowStatusOf, matchesWorkflow } from './remediationInboxModel.js'
import { exclusionReason } from './batchReviewSelection.js'
const AUTO_RULES = new Set(['1.1.1','2.4.4','2.4.9','4.1.2','1.3.3','3.1.2','2.4.6'])
export function automaticReviewResponsibility(row, decisions = {}) {
  const status=workflowStatusOf(row, decisions)
  if (status==='completed') return 'results'
  if (row.automaticQueued || (status === 'awaiting-validation' && ['queued','checking','applying','verifying','processing'].includes(row.automaticDisposition?.state))) return 'acp'
  const decision=decisions[row.id] || decisions[row.file]
  if (['assigned','deferred','rejected'].includes(decision?.state) || row.rejectedFix) return 'human'
  // A saved/accepted fix has no approval left to request. Earlier admission reasons
  // must not put it back in HITL; retain verification as a separate status check.
  if (status === 'awaiting-validation') return 'check'
  if (row.manual === true || (row.automaticDisposition?.responsibility === 'human'
    && row.automaticDisposition?.reason === 'Manual work or no supported proposal writer')) return 'human'
  // Incomplete drafts need recovery before a person can make a decision.
  // This is not evidence of a queued job or permission to auto-approve.
  if (row.aiDraftable === true && !row.hasProposal && !row.after) return 'check'
  const exclusion = exclusionReason(row, decisions)
  if (['Missing proposal', 'Version unavailable — review individually',
    'Stale — refresh and review', 'Invalid structural proposal — refresh suggestions',
    'Blocked or unavailable'].includes(exclusion)) return 'check'
  const rule=String(row.rule_id || row.ruleId || '').replace(/^(WCAG_?|SC_)/,'').replace(/_/g,'.')
  if (row.automaticDisposition?.responsibility==='human' || (rule && !AUTO_RULES.has(rule))) return 'human'
  // No admitted job is unknown, not automatic processing or a verified fix.
  return 'check'
}
export function matchesAutomaticReview(row, tab, decisions={}, enabled=false) {
  if (!enabled) return matchesWorkflow(row,tab,decisions)
  if (tab==='review') return automaticReviewResponsibility(row,decisions)==='human'
  if (tab==='status-check') return automaticReviewResponsibility(row,decisions)==='check'
  if (tab==='awaiting-validation') return automaticReviewResponsibility(row,decisions)==='acp'
  return matchesWorkflow(row,tab,decisions)
}
