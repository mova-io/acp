import { activeAutomaticStateOf, approvalRecordedOn, approvalSuperseded, resultKindOf, workflowStatusOf, matchesWorkflow } from './remediationInboxModel.js'
import { exclusionReason } from './batchReviewSelection.js'
export const AUTO_RULES = new Set(['1.1.1','2.4.4','2.4.9','4.1.2','1.3.3','3.1.2','2.4.6'])
// Batch-exclusion reasons that mean "not decidable yet" rather than "a person's to decide".
export const CHECK_EXCLUSIONS = ['Missing proposal', 'Version unavailable — review individually',
  'Stale — refresh and review', 'Invalid structural proposal — refresh suggestions', 'Blocked or unavailable']
export const normalizedRuleOf =row => String(row?.rule_id || row?.ruleId || '').replace(/^(WCAG_?|SC_)/,'').replace(/_/g,'.')
export function automaticReviewResponsibility(row, decisions = {}) {
  const status=workflowStatusOf(row, decisions)
  // Every recorded result (verified, rejected, decided, inspection, target replaced by a verified
  // fix) is a Result and nothing else — never a status check, never a request for input.
  if (status==='completed' || resultKindOf(row, decisions) != null) return 'results'
  // A queued or running writer / retry / eligibility job is ACP's, and ONLY ACP's: counted once, as
  // Processing, never also as a pending human task. `activeAutomaticStateOf` reads only the
  // exact-scope disposition, so a marker from another run cannot claim a job. A pending proposal
  // needs the projection's admission (`automaticQueued`); an approved row just needs the job.
  const active = activeAutomaticStateOf(row)
  const retrying = ['failed', 'apply_failed', 'verification_failed'].includes(String(row.status || '').toLowerCase())
  if (row.automaticQueued || (active && (status === 'awaiting-validation' || retrying || approvalRecordedOn(row, decisions)))) return 'acp'
  const decision=decisions[row.id] || decisions[row.file]
  if (['assigned','deferred','rejected'].includes(decision?.state) || row.rejectedFix) return 'human'
  // An approval that no longer binds is a person's again. It has to be decided BEFORE every branch
  // below that reads "an approval is recorded" — the awaiting-validation line, the backend's own
  // 'check', and `approvalRecordedOn` — because each of those hands the row to ACP and then says,
  // on screen, that no further approval will be requested. That sentence is true of an exact
  // current approval and false of a superseded one: the document or the suggestion moved under it,
  // the backend's retry gate refuses it ("The document has changed since this was approved"), and
  // a fresh decision really may be needed. Only unwritten rows reach here — `approvalSuperseded`
  // answers false once a write has landed, so an applied fix is not re-opened by a later revision.
  if (approvalSuperseded(row, decisions)) return 'human'
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
  // A draft nobody can decide on yet (missing or stale proposal, no version lineage, no approval
  // route) is a status check even when the server marker says a person will decide it: it has to be
  // repaired first (actionableReviewInput.test.jsx pins this). The status-check reason names the gap
  // (reviewQueueAction.statusReasonOf), never a generic sentence.
  const exclusion = exclusionReason(row, decisions)
  if (CHECK_EXCLUSIONS.includes(exclusion)) return 'check'
  // The backend's own classification of THIS row wins over the rule list, which is only a fallback
  // for rows the backend did not classify. AUTO_RULES mirrors api/ai_standing_approval.py RULES and
  // governs ADMISSION, not display: reading it as "not auto-approvable, therefore a person owns it"
  // is what overrode an explicit `responsibility: 'check'` from the backend and put a 1.4.5 row the
  // backend had already called ACP's into the human queue. A decidable row the server says a person
  // must decide is the person's, with the server's reason.
  const assigned = row.automaticDisposition?.responsibility
  if (assigned === 'human') return 'human'
  if (assigned === 'check') return 'check'
  const rule=normalizedRuleOf(row)
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
