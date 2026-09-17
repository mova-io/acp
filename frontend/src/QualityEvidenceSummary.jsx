const count = value => Number.isSafeInteger(value) && value >= 0
const money = (units, currency) => count(units) && currency === 'USD'
  ? new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:6}).format(units / 1000000)
  : 'Unavailable'
export function qualityEvidenceSummary({ view, metrics, verifiedFindings } = {}) {
  const policy = view?.available === true ? view.saved_ai_policy : null
  const mode = policy?.level === 0 ? 'Rules only'
    : policy?.quality_first === true && policy?.level > 0 ? 'Quality-first cloud'
    : policy?.zone === 'local' && policy?.level > 0 ? 'Local AI only'
    : policy?.zone === 'any' && policy?.level > 0 && policy?.quality_first === false ? 'Local and cloud AI permitted'
    : 'Saved mode unavailable'
  const spending = view?.available === true ? view.spending : null
  const models = (metrics?.models?.rows || []).filter(model => typeof model.label === 'string' && count(model.value) && model.value > 0)
  const notices = []
  if (policy?.level === 0) notices.push('AI is disabled in the saved plan; rule-based repairs can continue.')
  else if (policy?.zone === 'local') notices.push('Cloud requests are excluded by the saved local-only plan.')
  else if (spending?.cap_units === 0) notices.push('Cloud requests cannot start: the saved spending allowance is zero.')
  if (spending?.blocked === true) notices.push('Further AI spending is on hold until recorded charges are reconciled.')
  for (const step of view?.run_graph?.steps || []) {
    if (step.state === 'not_needed') notices.push('A configured fallback was skipped: earlier attempts produced usable suggestions and the saved run explicitly recorded the skip.')
  }
  return { mode, models, notices: [...new Set(notices)], verified: count(verifiedFindings) ? verifiedFindings.toLocaleString() : 'Unavailable',
    settled: money(spending?.spent_units, spending?.currency), reserved: money(spending?.held_units, spending?.currency),
    partial: metrics?.complete !== true }
}
export default function QualityEvidenceSummary(props) {
  const summary = qualityEvidenceSummary(props)
  return <section className="wd-chart" aria-label="Quality and spending evidence">
    <h3>Quality and spending · this run</h3>
    <p><strong>Selected mode:</strong> {summary.mode}</p>
    <p><strong>Verified fixes:</strong> {summary.verified} findings. This includes all repair methods; model calls alone do not establish improvement.</p>
    <p><strong>Provider spending:</strong> {summary.settled} settled · {summary.reserved} reserved, may still be charged.</p>
    <p><strong>Recorded model attempts:</strong> {summary.models.length
      ? summary.models.map(model => `${model.label} (${model.value.toLocaleString()})`).join('; ')
      : 'No linked model attempts available. This does not establish that cloud AI was skipped.'}</p>
    {summary.models.length > 0 && <p>Attempts are not proof of successful responses or verified repairs. {summary.partial ? 'Coverage is partial; these are not whole-run totals.' : 'Counts cover retained linked attempts; unlinked activity is excluded.'}</p>}
    {summary.notices.map(notice => <p key={notice}>{notice}</p>)}
  </section>
}
