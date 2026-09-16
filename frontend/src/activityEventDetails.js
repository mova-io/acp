const text = value => typeof value === 'string' && value.trim() ? value.trim() : null
const sha = value => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value) ? value.toLowerCase() : null
const criterion = value => typeof value === 'string' && /^\d\.\d\.\d{1,2}$/.test(value) ? value : null
const actions = {
  vision_response_empty: 'Provide the missing description or check local AI before retrying.',
  vision_generated_output_unusable: 'Review an available suggestion or provide the missing content.',
  vision_local_endpoint_required: 'Check the private AI endpoint before retrying local generation.',
  vision_spending_reconciliation_required: 'Wait for recorded usage confirmation before another paid request.',
  vision_permission_or_budget_blocked: 'Check the saved AI permission and readiness before retrying.',
  vision_provider_access_denied: 'Check access to the saved AI provider before retrying.',
  vision_budget_admission_denied: 'Check the recorded spending decision before retrying.',
  vision_budget_exhausted: 'Create a new approved plan with sufficient allowance before retrying.',
  vision_run_permission_unavailable: 'Review or replace the saved plan before retrying.',
  vision_ai_disabled_or_budget_zero: 'Enable AI through an approved plan with a spending allowance before retrying.',
  vision_pricing_not_verified: 'Select a model with verified pricing before retrying.',
  vision_provider_limit_exceeded: 'Check provider capacity or limits before retrying.',
  vision_provider_request_rejected: 'Check AI activity for the recorded rejection before retrying.',
}

export function activityEventSummary(event = {}) {
  const details = event.activityDetails || {}
  const criteria = (Array.isArray(details.criterion) ? details.criterion : typeof details.criterion === 'string' ? details.criterion.split(/\s*,\s*/) : []).map(criterion).filter(Boolean)
  const nextAction = text(details.nextAction) || actions[event.reasonCode] || null
  const bound = event.evidenceAvailable === true || Array.isArray(event.evidenceIds) && event.evidenceIds.length > 0
  return { visible: event.documentSuppressed !== true && (bound || criteria.length > 0 || !!nextAction),
    bound, criteria, location: text(details.location), nextAction }
}

export function activityEvidenceModel(payload = {}, event = {}) {
  payload ??= {}
  const expected = sha(event.snapshotSha256), artifact = sha(payload.artifact_sha256)
  const exact = payload.available === true && !!expected && artifact === expected
  const verification = payload.verification || {}
  const sameVerification = exact && sha(verification.artifact_sha256) === artifact
  const status = sameVerification ? verification.status : 'unavailable'
  const verificationLabel = status === 'verified' ? 'Recorded Check Passed'
    : status === 'failed' ? 'Recorded Check Failed' : 'Verification Unavailable For This Saved Version'
  return { available: exact, artifact, verificationLabel, changesTruncated: payload.changes_truncated === true,
    changeCount: Number.isSafeInteger(payload.change_count) && payload.change_count >= 0 ? payload.change_count : null,
    savedCopyAvailable: exact && payload.saved_copy?.matches_event === true && !!text(payload.saved_copy.download_url),
    changes: exact && Array.isArray(payload.changes) ? payload.changes.filter(change => change && typeof change === 'object').map(change => ({
      criterion: criterion(change.criterion), location: text(change.location),
      before: text(change.before), after: text(change.after),
      textTruncated: change.text_truncated === true,
      verified: sameVerification && status === 'verified' && change.verification === 'verified',
    })) : [] }
}
