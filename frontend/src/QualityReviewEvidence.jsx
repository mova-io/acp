import ProposalThumb, { isSafeThumb } from './ProposalThumb.jsx'
import { proposalsFor, pdfStructuralSummary } from './pdfStructuralProposal.js'
import './qualityReviewEvidence.css'

// Evidence is the recorded source, never an image generated from the proposed text.
export default function QualityReviewEvidence({ finding, editedValue }) {
  const proposals = proposalsFor(finding)
  const saved = finding?.autoApplied === true
  const hasRecordedChange = saved && (typeof finding.before === 'string' || typeof finding.after === 'string')
  if (!proposals.length && !hasRecordedChange) return null
  const criterion = String(finding.rule_id || finding.ruleId || '')
  const guidance = criterion === '1.3.2'
    ? 'Reading order: compare the recorded sequence with the original page. A text excerpt does not demonstrate screen-reader order.'
    : criterion === '1.3.1' || proposals.some(p => /table/i.test(p.kind || ''))
      ? 'Tables and structure: check header relationships and reading order in the saved document. A text preview does not render its accessibility tags.'
      : 'Check labels, years, values, signs and units against the source. If anything is unclear, edit the fix or leave it for review.'
  const verified = saved && finding.validated === true

  return <section className="quality-review-evidence" aria-label="Source evidence and proposed fixes">
    <h4>Compare with the source</h4>
    <p>{guidance}</p>
    <div className="quality-review-verification" role="status">
      <strong>{saved ? verified ? 'Saved change · recorded checks passed' : 'Saved change · verification not confirmed' : 'Proposed change · not a saved result'}</strong>
      <p>{saved ? verified ? 'The recorded checks passed for this change. This does not certify the whole document or verify every aspect of its meaning.' : 'A change was recorded, but this record does not confirm it resolved the finding.' : 'The preview shows a suggestion. Approval, writing the corrected copy, and verification are separate steps.'}</p>
    </div>
    {hasRecordedChange && <article aria-label="Recorded before and after">
      <div><strong>Before · recorded source</strong><p>{finding.before || 'Source excerpt unavailable. Open the original document to compare.'}</p></div>
      <div><strong>After · recorded saved change</strong><p>{finding.after || 'Saved change excerpt unavailable. Open the corrected document to inspect it.'}</p></div>
    </article>}

    {proposals.map((proposal, index) => {
      const needsReview = proposal.review_status === 'needs_review' || proposal.chart_review?.status === 'needs_review'
      const imageFinding = !!proposal.thumb || /image|chart|figure|picture/i.test(proposal.kind || '') || /1[.]1[.]1/.test(finding.rule_id || finding.ruleId || '')
      const aiSourceReviewed = proposal.quality_source_review?.status === 'ai_reviewed' && !needsReview && !proposal.automatic_write_blocked
      const sourceValidated = proposal.caption_validation?.approved === true && proposal.caption_validation?.status === 'validated' && !needsReview && !proposal.automatic_write_blocked
      const disagreement = proposal.agreement && proposal.agreement.verdict !== 'consistent'
      const value = index === 0 && editedValue != null ? editedValue : proposal.proposed_value
      return <article key={`${proposal.locator || 'proposal'}-${index}`} aria-label={`Source comparison ${index + 1}`}>
        <div>
          <strong>Source {proposals.length > 1 ? index + 1 : ''}</strong>
          {proposal.locator && <p className="muted">{proposal.locator}</p>}
          {isSafeThumb(proposal.thumb) ? <>
            <ProposalThumb thumb={proposal.thumb} size={240} alt={`Recorded source image${proposal.locator ? ` at ${proposal.locator}` : ''}`} />
            <p className="muted">Recorded thumbnail. Check the original document if fine details are unclear.</p>
          </> : imageFinding ? <p className="muted">Image preview unavailable. Check the original document.</p> : !proposal.before && !proposal.subject_text ? <p className="muted">Source excerpt unavailable. Check the original document.</p> : null}
          {typeof proposal.before === 'string' && proposal.before && <p>{proposal.before}</p>}
          {typeof proposal.subject_text === 'string' && proposal.subject_text && <p>{proposal.subject_text}</p>}
        </div>
        <div><strong>Proposed fix {proposals.length > 1 ? index + 1 : ''}</strong>
          <p className="muted">{aiSourceReviewed ? 'Source meaning: AI reviewed against the source with supported claims. This is an approval judgment, not proof of semantic correctness or full accessibility.' : sourceValidated ? 'Source meaning: independently checked against exact image pixels. Saved document verification is tracked separately.' : 'Source meaning: not independently verified. Model agreement or saved text alone does not establish accuracy.'}</p>
          <p>{pdfStructuralSummary(proposal) || (typeof value === 'string' && value ? value : 'No proposed value recorded.')}</p>
          {needsReview && <p className="quality-review-notice"><strong>Needs review:</strong> {typeof proposal.why_review === 'string' && proposal.why_review ? proposal.why_review : 'Check the chart’s series, years, signs, values and units before approving. These relationships have not been verified.'}</p>}
          {disagreement && <p className="quality-review-notice"><strong>Needs review:</strong> A second model did not confirm this description.{typeof proposal.agreement.second_opinion === 'string' && proposal.agreement.second_opinion ? ` Second opinion: ${proposal.agreement.second_opinion}` : ''}</p>}
        </div>
      </article>
    })}
  </section>
}
