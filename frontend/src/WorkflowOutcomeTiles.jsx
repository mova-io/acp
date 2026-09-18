import InfoTip from './InfoTip.jsx'
import BidirectionalKpiCounter from './BidirectionalKpiCounter.jsx'
import './workflow-outcome-tiles.css'

const valid = value => Number.isSafeInteger(value) && value >= 0
const FINDING = [
  ['queued', 'Awaiting outcome', 'blue', ['awaiting_recorded_outcome', 'not_in_remediation_breakdown']],
  ['processing', 'Applying & checking', 'purple', ['approved_pending_verification', 'approved_awaiting_verification']],
  ['verified', 'Verified fixes', 'green', ['resolved_verified']],
  ['attention', 'Unresolved findings', 'amber', ['awaiting_review', 'unchanged_no_fix', 'failed']],
  // One gray tile (its key is the server's queue-drawer and baseline bucket), but two different
  // facts inside it, and the tile names both. `excluded` is excluded_by_policy — a saved scope
  // decision. `superseded` is superseded_by_reassessment — the finding's target was replaced or
  // removed (e.g. an image replaced by real text in a verified 1.4.5 change), or a re-assessment
  // retired it. Labelling the whole tile "Excluded" told a reviewer a policy had set aside a finding
  // that a verified change had in fact made moot.
  ['excluded', 'Excluded or replaced', 'gray', ['excluded', 'superseded']],
]
const PARTS = { excluded: [['superseded', 'replaced or superseded'], ['excluded', 'excluded by policy']] }
const PUBLICATION = [
  ['queued', 'Waiting for delivery', 'blue', ['waiting', 'queued']],
  ['processing', 'Publishing', 'purple', ['processing']],
  ['published', 'Published', 'green', ['published', 'completed_unverified']],
  ['attention', 'Delivery issues', 'amber', ['failed', 'cancelled']],
  ['skipped', 'Skipped', 'gray', ['skipped']],
  ['unclassified', 'Classification unavailable', 'gray', ['unclassified']],
]

// Group the recorded partition, never operational job totals or approval counts.
export function outcomeTileModel(stage, domain) {
  const configuration = stage === 'remediate' ? FINDING : stage === 'release' ? PUBLICATION : null
  if (!configuration || !domain?.buckets || domain.available === false) return null
  const buckets = domain.buckets
  const keys = Object.keys(buckets)
  const known = new Set(configuration.flatMap(item => item[3]))
  const balanced = valid(domain.total) && Object.values(buckets).every(valid)
    && Object.values(buckets).reduce((sum, value) => sum + value, 0) === domain.total
  return { balanced, total: domain.total, tiles: configuration.map(([key, label, tone, group]) => ({
    key, label, tone, value: balanced ? group.reduce((sum, name) => sum + (buckets[name] || 0), 0)
      + ((stage === 'remediate' && key === 'attention') || (stage === 'release' && key === 'unclassified') ? keys.filter(name => !known.has(name)).reduce((sum, name) => sum + buckets[name], 0) : 0) : null,
    parts: stage === 'remediate' && balanced && PARTS[key] ? PARTS[key].map(([name, text]) => ({ key: name, label: text, value: buckets[name] || 0 })) : null,
  })) }
}

const descriptions = {
  remediate: {
    queued: ['No saved result yet', 'Findings without a recorded outcome, including queued work or results still syncing.'],
    processing: ['Changes awaiting verification', 'Approved work awaiting application or independent checking. Each item shows its precise step.'],
    verified: ['Saved changes passed checks', 'Fixes independently verified against corrected files and durably recorded. Approval alone never counts as a verified fix.'],
    attention: ['Not yet verified as fixed', 'Includes review-required, unchanged, failed and unclassified outcomes. Check each item for automatic recovery, your review or manual repair. This count can rise as remaining issues are recorded; it does not mean new issues were introduced.'],
    excluded: ['Not counted as fixes', 'Replaced or superseded: the finding\'s target no longer exists in the corrected copy — for example an image replaced by real text in a verified change — or a re-assessment retired it; no change was written for that finding and no approval is needed. Excluded by policy: the saved policy deliberately left the finding out of remediation. Neither counts as a verified fix.'],
  },
  release: {
    queued: ['Delivery has not started', 'Authorized copies waiting for publication. Blocked or unclassified copies are identified separately.'],
    processing: ['Delivery underway', 'Upload or existing-copy confirmation is in progress.'],
    published: ['Confirmed at destination', 'A corrected copy has a confirmed destination receipt. Publication does not certify accessibility.'],
    attention: ['Delivery needs recovery', 'Check each copy for automatic recovery status or the concrete action required. Outstanding copies prevent release completion.'],
    unclassified: ['Delivery classification unavailable', 'The saved plan includes these copies but their delivery classification is unavailable. They are not classified as failed or waiting and prevent a complete delivery result.'],
    skipped: ['Not delivered', 'Check the reason for each skipped copy. Skipped copies do not count as published.'],
  },
}

const positiveDirections = { queued: 'decrease', processing: 'neutral', attention: 'decrease', verified: 'increase', published: 'increase', excluded: 'neutral', skipped: 'neutral' }

export default function WorkflowOutcomeTiles({ stage, domain, baseline = null, executionId, onFilter, queueMode = false, scopeLabel = null, reviewHostId = null, reviewScanId = null, reviewWorkspace = null }) {
  const model = outcomeTileModel(stage, domain)
  if (!model) return null
  const beforeValues = baseline?.available === true && baseline.run_id === executionId
    ? (stage === 'remediate' ? baseline.findings : baseline.publication ? { unclassified: 0, ...baseline.publication } : null) : null
  const baselineBalanced = beforeValues && model.tiles.every(tile => valid(beforeValues[tile.key]))
    && model.tiles.reduce((sum, tile) => sum + beforeValues[tile.key], 0) === model.total
  const title = stage === 'remediate' ? 'Finding results' : 'Publication progress'
  return <section className="workflow-outcome-tiles" aria-label={title}>
    <h4>{title}</h4>
    <p className="workflow-outcome-tiles__scope">{scopeLabel ?? (stage === 'remediate'
      ? `${model.total ?? '—'} assessed findings · this remediation run`
      : `${model.total ?? '—'} requested files · this release only`)}</p>
    {[model.tiles.slice(0, 3), model.tiles.slice(3).filter(tile => tile.key !== 'unclassified' || tile.value !== 0)].map((tiles, index) => <div key={index}>
      {index === 1 && <p className="workflow-outcome-tiles__separate">Separate outcomes</p>}
      <div className={`workflow-outcome-tiles__grid ${index === 1 ? 'workflow-outcome-tiles__grid--separate' : ''}`} role="group" aria-label={index === 1 ? 'Separate outcomes' : stage === 'remediate' ? 'Automatic repair progress' : 'Delivery progress'}>
      {tiles.map((tile) => {
        const old = baselineBalanced ? beforeValues[tile.key] : null
        const delta = old != null && tile.value != null ? tile.value - old : null
        const content = <>
          <span className="workflow-outcome-tiles__label">{tile.label}</span>
          <strong aria-label={`${tile.label}: ${tile.value ?? 'unavailable'}`}>{tile.value == null ? '—' : <BidirectionalKpiCounter value={tile.value} positiveDirection={positiveDirections[tile.key]} />}</strong>
          <span className="workflow-outcome-tiles__definition">{tile.parts ? tile.parts.map(part => `${part.value.toLocaleString()} ${part.label}`).join(' · ') : descriptions[stage][tile.key][0]}</span>
          {old != null && <span className="workflow-outcome-tiles__before">Before: {old.toLocaleString()}</span>}
          {delta != null && <span className="workflow-outcome-tiles__net">{delta > 0 ? '+' : delta < 0 ? '−' : ''}{Math.abs(delta).toLocaleString()} since starting</span>}
          <span className="workflow-outcome-tiles__fill" aria-hidden="true" style={{ transform: `scaleX(${model.total > 0 && tile.value != null ? tile.value / model.total : 0})` }} />
        </>
        const key = `${executionId}:${tile.key}`
        return <div key={key} className={`workflow-outcome-tiles__cell tone-${tile.tone} ${tile.key === 'verified' ? 'finding-outcome-kpis__verified' : ''}`}>
          {onFilter ? <button className="workflow-outcome-tiles__tile" type="button" aria-haspopup={queueMode ? 'dialog' : undefined} onClick={() => onFilter(tile.key)}>{content}</button> : <div className="workflow-outcome-tiles__tile">{content}</div>}
          <InfoTip label={tile.label}>{descriptions[stage][tile.key][1]}</InfoTip>
        </div>
      })}
      {index === 1 && reviewWorkspace}
      {index === 1 && stage === 'remediate' && reviewHostId && <div id={reviewHostId} data-scan-id={reviewScanId} data-batch-id={executionId} className="workflow-outcome-tiles__cell" aria-label="Review workspace" />}
      </div>
    </div>)}
    <p className="workflow-outcome-tiles__note">{!model.balanced ? 'Updating: outcome totals are being reconciled.' : stage === 'remediate'
      ? 'These tiles count findings. Review workspace counts review tasks — a different unit: one task can cover several findings and a finding can have no task, so the two are never added. Unresolved findings may need your review, manual repair or recovery; only checked fixes count as verified.'
      : 'Published includes delivered copies with remaining issues. Publication does not certify accessibility; see Outcome details.'}</p>
  </section>
}
