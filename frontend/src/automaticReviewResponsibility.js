import { approvalRecordedOn, workflowStatusOf, matchesWorkflow } from './remediationInboxModel.js'
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
  //
  // `approvalRecordedOn` is the same fact read from the row rather than from the workflow stage,
  // and it is needed because a jobless or refused write makes `workflowStatusOf` answer 'blocked'
  // BEFORE its approved branch runs. Without it an already-approved row fell past this line into
  // the rule-list fallback below and was billed to the reviewer as "Needs your input" — a request
  // for an approval that is already recorded and will never be asked for again (scan 6f07d85b39b8,
  // 1.4.5). The finding stays blocked and visible; only its OWNER changes.
  if (status === 'awaiting-validation' || approvalRecordedOn(row, decisions)) return 'check'
  if (row.manual === true || (row.automaticDisposition?.responsibility === 'human'
    && row.automaticDisposition?.reason === 'Manual work or no supported proposal writer')) return 'human'
  // Incomplete drafts need recovery before a person can make a decision.
  // This is not evidence of a queued job or permission to auto-approve.
  if (row.aiDraftable === true && !row.hasProposal && !row.after) return 'check'
  const exclusion = exclusionReason(row, decisions)
  if (['Missing proposal', 'Version unavailable — review individually',
    'Stale — refresh and review', 'Invalid structural proposal — refresh suggestions',
    'Blocked or unavailable'].includes(exclusion)) return 'check'
  // The backend's own classification of THIS row wins over the rule list, which is only a fallback
  // for rows the backend did not classify. AUTO_RULES mirrors api/ai_standing_approval.py RULES and
  // governs ADMISSION, not display: reading it as "not auto-approvable, therefore a person owns it"
  // is what overrode an explicit `responsibility: 'check'` from the backend and put a 1.4.5 row the
  // backend had already called ACP's into the human queue.
  const assigned = row.automaticDisposition?.responsibility
  if (assigned === 'human') return 'human'
  if (assigned === 'check') return 'check'
  const rule=String(row.rule_id || row.ruleId || '').replace(/^(WCAG_?|SC_)/,'').replace(/_/g,'.')
  if (rule && !AUTO_RULES.has(rule)) return 'human'
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
