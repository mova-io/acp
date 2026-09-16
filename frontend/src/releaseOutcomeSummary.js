import { releaseBatchDomain, releaseBatchProgress } from './releaseBatchProgress.js'
import { findingMath, originalFindingMetrics } from './RemediationAssessmentProgress.jsx'

const count = value => Number.isSafeInteger(value) && value >= 0
const digest = value => /^[a-f0-9]{64}$/i.test(value || '')
export function savedDestinationLinks(folders = []) {
  const seen = new Set()
  return folders.flatMap(folder => {
    try {
      const url = new URL(folder?.url)
      if (url.protocol !== 'https:' || url.username || url.password || seen.has(url.href)) return []
      seen.add(url.href)
      return [{ url: url.href, name: folder.name || 'published folder' }]
    } catch { return [] }
  })
}

// Scope totals come from the saved authorization, never the most recent one-file request.
// A published bucket alone cannot confirm the currently displayed corrected bytes.
export function releaseOutcomeSummary({ scanId, authorization, pending = false, error, files = [], results = {}, snapshot } = {}) {
  const batch = releaseBatchProgress({ stage: 'release', release_batch_progress: authorization?.batch_progress })
  const domain = releaseBatchDomain(batch)
  const names = authorization?.files
  const membership = batch?.file_membership
  const authorizationScope = !!authorization?.id && !!authorization?.run_id
    && Array.isArray(names) && names.length > 0 && new Set(names).size === names.length
    && names.length === files.length
    && names.every(name => files.filter(file => file.file === name).length === 1)
  const deliveryScoped = authorizationScope && !!domain
    && batch.authorization_id === authorization.id && batch.run_id === authorization.run_id
    && names.length === domain.total
    && names.every(name => Object.hasOwn(membership, name))
  const deliveryAvailable = !pending && !error && deliveryScoped
  const currentReceipt = file => digest(file.corrected_sha256)
    && results[file.file]?.status === 'published'
    && results[file.file]?.artifact_digest === `sha256:${file.corrected_sha256}`
  const delivered = deliveryAvailable ? names.filter(name => membership[name] === 'published'
    && currentReceipt(files.find(file => file.file === name))).length : null
  const confirmed = deliveryAvailable && delivered === batch.delivered
  const complete = confirmed && batch.state === 'succeeded' && delivered === domain.total
  const authorizationBlocked = authorizationScope && (authorization.requires_reconnect === true
    || authorization.needs_attention === true || ['blocked', 'failed', 'stopped'].includes(authorization.status))
  const blocked = authorizationBlocked || (deliveryAvailable
    && (batch.state === 'failed' || domain.buckets.failed > 0 || domain.buckets.skipped > 0))
  const original = snapshot?.finding_reconciliation?.original_assessment
  const originalMatches = authorizationScope && Array.isArray(original)
    && !!originalFindingMetrics(original, snapshot.finding_reconciliation.assessed)
    && original.every(group => names.includes(group.file))
  const findingBound = originalMatches && snapshot.total_documents === names.length
    && (snapshot.scan_id || snapshot.run_id) === scanId
    && !!snapshot.batch_id && snapshot.batch_id === authorization.run_id
  const findings = findingBound ? findingMath(snapshot) : { exact: false }
  // Release uses the same equation as Remediate: every assessed finding that is not
  // resolved with verification remains open. Exclusions and superseded findings stay
  // named separately below, but must not disappear from the open total.
  const open = findings.exact ? findings.remaining : null
  const deliveryReason = confirmed ? null
    : pending ? 'Automatic publication evidence is still loading.'
      : error ? 'Automatic publication evidence could not be loaded.'
        : !authorization ? 'No saved automatic publication scope is available.'
          : !authorizationScope ? 'The saved authorization scope does not match this Release view.'
            : !deliveryScoped ? 'Saved delivery evidence is incomplete for this authorization.'
              : 'Saved delivery totals do not match current corrected-copy receipts.'
  return {
    state: complete ? 'complete' : blocked ? 'attention' : deliveryAvailable && confirmed ? 'processing' : 'unavailable',
    title: complete ? 'Delivery complete' : blocked ? 'Delivery needs attention'
      : deliveryAvailable && confirmed ? 'Delivery in progress' : 'Checking delivery confirmation',
    total: deliveryAvailable ? domain.total : null,
    delivered: confirmed ? delivered : null,
    remainingCopies: confirmed ? domain.total - delivered : null,
    fixed: findings.exact && count(findings.fixed) ? findings.fixed : null,
    open,
    excluded: findings.exact ? findings.rec.excluded : null,
    superseded: findings.exact ? findings.rec.superseded : null,
    deliveryReason,
    complete,
  }
}
