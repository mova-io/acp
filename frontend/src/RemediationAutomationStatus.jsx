import './remediation-automation-status.css'

// Consent describes permission, never admission, a successful repair, or delivery.
export default function RemediationAutomationStatus({ policy, error, saving, reviewCount = 0, onOpenReview, onRetry, combined = false }) {
  const enabled = policy?.enabled === true && policy.supported !== false
  const unavailable = !!error || policy?.supported === false
  const label = unavailable ? 'Setting unavailable' : saving ? 'Saving permission…' : !policy ? 'Checking permission…' : enabled ? 'Automatic approval is on' : 'Individual review is on'
  if (combined) return <div className="remediation-automation-combined-setting"><h3>AI fixes · {label}</h3><p>{enabled ? 'ACP automatically approves eligible proposals, saves changes, and checks corrected copies. Items needing your input remain in Review workspace.' : 'Eligible AI proposals need approval before ACP saves and checks them.'}</p>{policy?.explanation && <p>{policy.explanation}</p>}{unavailable && <><p role="status">{error || policy?.reason}</p>{onRetry && <button type="button" className="ghost small" onClick={onRetry}>Refresh setting</button>}</>}</div>
  return <section className="remediation-automation-status" aria-label="Remediation automation">
    <details><summary><span className="remediation-automation-status__label">AI fixes</span><span className="remediation-automation-status__summary">{label}</span></summary>
      <p>{unavailable ? 'ACP could not confirm the saved approval permission. Refresh the setting before changing it.' : !policy ? 'ACP is checking the saved approval permission for this run.' : enabled ? 'ACP automatically approves eligible proposals, saves the changes, and checks the corrected copy. Exceptions remain available for review.' : 'Eligible AI proposals need approval before ACP saves and checks them.'}</p>
      {policy?.explanation && <p>{policy.explanation}</p>}{unavailable && <><p role="status">{error || policy.reason}</p>{onRetry && <button className="ghost small" onClick={onRetry}>Refresh setting</button>}</>}
    </details>
    <details><summary><span className="remediation-automation-status__label">Review workspace</span><span className="remediation-automation-status__summary">{reviewCount.toLocaleString()} {enabled ? 'need your input' : 'review tasks'}</span></summary><p>Review tasks still open. This is a task count, not a count of findings or verified fixes; the two are never added.</p>
      {onOpenReview && <button className="ghost small" onClick={onOpenReview}>Open review items</button>}
    </details>
  </section>
}
