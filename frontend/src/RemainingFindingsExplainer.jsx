import './RemainingFindingsExplainer.css'

// Names what is left, one entry per task or finding, with the reason and a way to open it.
//
// The explanation is computed once (reviewPopulationExplanation.js) and only rendered here, so the
// words on this panel cannot disagree with the pills, the progress line or the "All clear" lead.
// Rendered nothing when the explanation is all clear: the lead line already says so, and a second
// panel repeating it would be a second place for the claim to drift.
const itemLabel = (criterion) => criterion ? `Open ${criterion} item` : 'Open item'

// `showHeadline={false}` when the host already prints explanation.headline as its lead line, so the
// same sentence is not on screen twice.
export default function RemainingFindingsExplainer({ explanation, onOpenItem, showHeadline = true }) {
  if (!explanation || explanation.allClear) return null
  const { remaining = [], unmatchedFindings = [], unlistedFindings = null, progress = null } = explanation
  const open = (id, tab) => () => onOpenItem?.(id, tab)
  return <section className="remaining-findings-explainer" aria-label="Remaining findings and tasks">
    <h3>What is still open</h3>
    {showHeadline && <p className="remaining-findings-explainer__headline" role="status">{explanation.headline}</p>}
    <p className="remaining-findings-explainer__units muted">
      {explanation.taskTotal.toLocaleString()} review task{explanation.taskTotal === 1 ? '' : 's'}
      {explanation.findingTotal != null
        ? <> · {explanation.findingTotal.toLocaleString()} unresolved finding{explanation.findingTotal === 1 ? '' : 's'} reported by the server</>
        : <> · current finding totals unavailable</>}
      . {explanation.findingNote}
    </p>
    {remaining.length > 0 && <>
      <h4>Open tasks · {remaining.length.toLocaleString()}</h4>
      <ul className="remaining-findings-explainer__list">
        {remaining.map((item) => {
          const headingId = `remaining-task-${String(item.itemId).replace(/[^A-Za-z0-9_-]/g, '_')}`
          return <li key={`task:${item.itemId}`} data-item-id={item.itemId} data-tab={item.tab}>
            <strong id={headingId}>{item.criterion ? `${item.criterion} ` : ''}{item.name}</strong>
            <span className="remaining-findings-explainer__where">{item.file}{item.tabLabel ? ` · ${item.tabLabel}` : ''}</span>
            <span><b>Why it is open:</b> {item.reason}</span>
            <span><b>Next:</b> {item.nextAction}</span>
            {item.findingIds?.length > 0 && <span className="muted">Linked to {item.findingIds.length} unresolved finding{item.findingIds.length === 1 ? '' : 's'} reported by the server.</span>}
            <button type="button" className="ghost small" aria-describedby={headingId} onClick={open(item.itemId, item.tab)} disabled={!onOpenItem}>{itemLabel(item.criterion)}</button>
          </li>
        })}
      </ul>
    </>}
    {(unmatchedFindings.length > 0 || unlistedFindings) && <>
      <h4>Unresolved findings without an open task{unmatchedFindings.length ? ` · ${unmatchedFindings.length.toLocaleString()}` : ''}</h4>
      <ul className="remaining-findings-explainer__list">
        {unmatchedFindings.map((finding, index) => {
          const target = finding.matchedItemId ?? finding.relatedItemId ?? null
          const tab = finding.matchedItemId != null ? finding.matchedTab : finding.relatedTab
          const headingId = `remaining-finding-${index}`
          return <li key={`finding:${finding.findingId ?? index}`} data-finding-id={finding.findingId ?? undefined}>
            <strong id={headingId}>{finding.criterion ? `${finding.criterion} ` : ''}{finding.name || 'Unnamed criterion'}</strong>
            <span className="remaining-findings-explainer__where">{finding.file || 'Document not recorded'} · finding{finding.findingId ? ` ${finding.findingId}` : ''}</span>
            <span><b>Why it is open:</b> {finding.reason}</span>
            <span><b>Next:</b> {finding.nextAction}</span>
            {target != null && <button type="button" className="ghost small" aria-describedby={headingId} onClick={open(target, tab)} disabled={!onOpenItem}>{itemLabel(finding.criterion)}</button>}
          </li>
        })}
        {unlistedFindings && <li className="remaining-findings-explainer__unlisted"><span>{unlistedFindings.text}</span></li>}
      </ul>
    </>}
    {progress && progress.total > 0 && <div className="remaining-findings-explainer__progress" aria-label="Review task progress">
      <span>{progress.decidedLabel}</span>
      <span>{progress.finishedLabel}</span>
      <small className="muted">{progress.definition}</small>
    </div>}
  </section>
}
