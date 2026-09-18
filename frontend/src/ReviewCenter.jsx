import { useState, useMemo, useEffect } from 'react'
import { SEV, sevOf, reasonOf, priorityScore, groupLabel } from './hitlMeta.js'
import { confidenceForFinding, confClass } from './confidence.js'
import { openTraceUrl } from './api.js'
import EvidenceCard from './EvidenceCard.jsx'
import { HELD_LABEL, REAPPROVE_ACTION, heldExplanation, reapprovalValues, viewedBindingKey } from './viewedApprovalBinding.js'
// proposalMeta / firstProposed live in reviewCard.js — the single source of truth for how a
// hitl_queue.proposals row is read. EvidenceCard uses them too; don't fork the logic.
import { VALUE_FIX, firstProposed, proposalMeta, reviewType, REVIEW_TYPES } from './reviewCard.js'
import RiskChip from './RiskChip.jsx'
import { riskComparator, reviewRisk, fmtEst } from './reviewRisk.js'

// Rules whose fix IS a value a human writes/edits (alt text, link text, title, label) —
// these get an editable "approved value" box (the AI draft, if any, prefilled). Judgement
// rules (contrast) have no value to type, just approve/reject.
const scOf = (r) => String(r || '').replace(/^SC[_ ]?/i, '').replace(/_/g, '.').match(/^\d+\.\d+\.\d+/)?.[0] || ''
const isToday = (iso) => { if (!iso) return false; const d = new Date(iso), n = new Date(); return d.toDateString() === n.toDateString() }

// The full-screen "Review queue" — GitHub-PRs-meets-Gmail. Items are grouped by issue
// type, priority-sorted, each showing the REAL reason it escalated to a human, with
// one-click approve / reject / skip (and inline edit of the AI-drafted value).
export default function ReviewCenter({ items, onAct, onClose, onRefresh, error }) {
  const [expanded, setExpanded] = useState(null)
  const [busy, setBusy] = useState(null)        // itemId currently acting
  const [sortMode, setSortMode] = useState('critical')   // 'critical' = triage | 'quick' = clear easy work first
  // A decision the server refused. A silent failure here means the reviewer thinks they signed
  // something off that was never recorded — so we must always surface it.
  const [actError, setActError] = useState(null)

  const pending = useMemo(() => items.filter((i) => i.status === 'pending'), [items])
  const resolvedToday = items.filter((i) => i.status !== 'pending' && isToday(i.reviewed_at))
  const approvedN = items.filter((i) => i.status === 'approved').length
  const rejectedN = items.filter((i) => i.status === 'rejected').length
  const acceptance = approvedN + rejectedN ? Math.round((approvedN / (approvedN + rejectedN)) * 100) : null
  const highN = pending.filter((i) => sevOf(i) === 'high').length
  const reviewedN = items.filter((i) => i.status !== 'pending').length
  const totalN = items.length
  // Certification impact (#10): clearing this queue unblocks documents for certification, and the
  // estimated review effort tells the reviewer how far off "done" is — gamifies completion. Both
  // are real: the est is the sum of per-item risk-tier estimates; the doc count is the distinct
  // files these approvals clear. (The estate % projection needs the scan totals, which the inbox
  // doesn't hold — surfaced here as the document count instead, which is honest either way.)
  const estToClearS = pending.reduce((s, it) => s + reviewRisk(it).estSeconds, 0)
  const docsUnblocked = new Set(pending.map((i) => i.file || i.id)).size

  // Three review types (canonical HITL vision): AI-proposal validation, deterministic
  // confirmation, and manual authoring are DIFFERENT JOBS with different promises — mixing
  // them in one list forces the reviewer to decode what kind of work each row is. Partition
  // by reviewType (per-item, from real data), then keep the issue-type grouping within each.
  // Order = effort: drafted one-click approvals first, applied-fix confirmations next, the
  // real authoring work last.
  const sections = useMemo(() => {
    const byType = { proposal: [], confirm: [], author: [] }
    for (const it of pending) byType[reviewType(it)].push(it)
    return ['proposal', 'confirm', 'author']
      .filter((t) => byType[t].length)
      .map((t) => {
        const m = new Map()
        for (const it of byType[t]) {
          const k = groupLabel(it)
          if (!m.has(k)) m.set(k, [])
          m.get(k).push(it)
        }
        const cmp = riskComparator(sortMode)
        const groups = [...m.entries()]
          .map(([label, its]) => ({ label, items: its.sort(cmp) }))
          .sort((a, b) => cmp(a.items[0], b.items[0]))
        return { type: REVIEW_TYPES[t], count: byType[t].length, groups }
      })
  }, [pending, sortMode])

  // Flat render-order list + a cursor, for keyboard-driven review (j/k, a/r/s, Enter).
  const ordered = useMemo(
    () => sections.flatMap((s) => s.groups.flatMap((g) => g.items)), [sections])
  const [cursor, setCursor] = useState(0)

  // Bulk path only. A single item is decided inside its EvidenceCard, which carries the
  // reviewer's note, the (possibly edited) value, and the review telemetry.
  const doAct = async (it, status) => {
    setBusy(it.id)
    setActError(null)
    // An item carrying an AI proposal takes a value even if its SC isn't in VALUE_FIX, and
    // the proposed value is what a bulk approve accepts (there is no per-item edit here).
    const takesValue = VALUE_FIX.has(scOf(it.rule_id)) || !!(it.proposals && it.proposals.length)
    const val = takesValue ? (firstProposed(it) ?? it.approved_value ?? null) : null
    // Bulk/keyboard rejections carry reason 'unspecified' — recorded honestly as "no reason
    // asked", never dropped, so the feedback rollup separates them from chip-picked reasons.
    const opts = status === 'rejected' ? { rejectReason: 'unspecified' } : {}
    try {
      await onAct(it.id, status, null, status === 'approved' ? val : null, opts)
    } catch (e) {
      // HitlBell rolls back the optimistic state and rethrows. An unrecorded approval must
      // never look like a recorded one — surface the failure so the reviewer can retry.
      setActError(`Not saved: ${e?.message || e}. Nothing was recorded — try again.`)
    } finally {
      setBusy(null); setExpanded(null)
    }
  }

  // "Review and approve again" for a HELD approval (HitlBell marks it `reapprove`): one approval of exactly
  // what the writer would write, through the same onAct (HitlBell binds it to the row it holds and sends
  // approval_scope 'single'). The flag clears on the server, so the row leaves this list — no loop.
  const reapprove = async (it) => {
    setBusy(it.id)
    setActError(null)
    const values = reapprovalValues(it)
    try {
      await onAct(it.id, 'approved', null, values.find(Boolean) || it.approved_value || null,
        { approvedValues: values.length ? values : null, resolution: it.resolution || null })
    } catch (e) {
      setActError(`Not saved: ${e?.message || e}. Nothing was recorded — try again.`)
    } finally {
      setBusy(null)
    }
  }

  // Keyboard-driven review — power reviewers never touch the mouse. Typing in a note /
  // value box is never hijacked; Escape closes.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') { onClose(); return }
      const tag = (e.target?.tagName || '').toUpperCase()
      if (tag === 'INPUT' || tag === 'TEXTAREA') return
      if (!ordered.length) return
      const cur = ordered[Math.min(cursor, ordered.length - 1)]
      if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); setCursor((c) => Math.min(ordered.length - 1, c + 1)) }
      else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); setCursor((c) => Math.max(0, c - 1)) }
      else if (e.key === 'Enter') { e.preventDefault(); setExpanded((x) => (x === cur.id ? null : cur.id)) }
      else if (e.key === 'a') { e.preventDefault(); doAct(cur, 'approved') }
      else if (e.key === 'r') { e.preventDefault(); doAct(cur, 'rejected') }
      else if (e.key === 's') { e.preventDefault(); doAct(cur, 'skipped') }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [ordered, cursor, onClose])

  // A "judgement" item has no value to type (no VALUE_FIX rule and no AI proposal) — only
  // those are safe to bulk-approve; a proposal must be reviewed individually.
  const isJudgement = (it) => !VALUE_FIX.has(scOf(it.rule_id)) && !(it.proposals && it.proposals.length)
  const approveGroup = (grp) => grp.items.forEach((it) => {
    if (isJudgement(it)) onAct(it.id, 'approved').catch((e) => {
      setActError(`Not saved: ${e?.message || e}. Nothing was recorded — try again.`)
    })
  })
  // Bulk "Confirm all" for the deterministic-confirmation tier (PRD HITL 2.0 bulk approval):
  // those items are ACP-applied rule-based fixes already re-validated, so a rubber-stamp of
  // the whole tier is the intended one-click — never offered for the proposal or authoring
  // tiers, which need per-item review.
  const confirmSection = (sec) => sec.groups.flatMap((g) => g.items).forEach((it) => doAct(it, 'approved'))

  return (
    <div className="rc-overlay" role="dialog" aria-modal="true" aria-label="Review queue">
      <div className="rc-panel">
        <div className="rc-head">
          <div>
            <h2 className="rc-title">🔔 Review queue</h2>
            <p className="muted rc-sub">AI-drafted and low-confidence fixes awaiting your approval before they can be certified.</p>
          </div>
          <button className="rc-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="rc-metrics">
          <div className="rc-metric"><span>Pending</span><b>{pending.length}</b></div>
          <div className="rc-metric"><span>High priority</span><b style={{ color: highN ? SEV.high.color : undefined }}>{highN}</b></div>
          <div className="rc-metric"><span>Resolved today</span><b>{resolvedToday.length}</b></div>
          <div className="rc-metric"><span>AI draft acceptance</span><b>{acceptance == null ? '—' : `${acceptance}%`}</b></div>
        </div>
        {/* Labels must match the handler exactly: a→approved, r→rejected, s→skipped ("I'll fix
            it"). They were swapped — a reviewer pressing r expecting "I'll fix it" REJECTED. */}
        <div className="rc-kbd-hint" aria-hidden="true">↑↓ / j k navigate · <b>a</b> approve · <b>r</b> reject · <b>s</b> I’ll fix it · Enter expand · Esc close</div>
        {/* Risk-tier sort (#6): triage the criticals, or clear the quick wins first. */}
        <div className="rc-sort" style={{ display: 'flex', gap: 6, alignItems: 'center', margin: '2px 0 8px', fontSize: 12 }}>
          <span className="muted">Sort:</span>
          <button className={`ghost small${sortMode === 'critical' ? ' on' : ''}`} aria-pressed={sortMode === 'critical'}
                  onClick={() => setSortMode('critical')} title="Highest-risk findings first">Most critical</button>
          <button className={`ghost small${sortMode === 'quick' ? ' on' : ''}`} aria-pressed={sortMode === 'quick'}
                  onClick={() => setSortMode('quick')} title="Lowest estimated effort first — clear quick wins">Quickest first</button>
        </div>
        {totalN > 0 && (
          <div className="rc-progress">
            <span className="track"><i style={{ width: `${Math.round((reviewedN / totalN) * 100)}%` }} /></span>
            <span className="muted">{reviewedN} of {totalN} reviewed</span>
          </div>
        )}
        {/* Certification impact (#10): what clearing this queue achieves + how far off "done" is —
            real counts, honest estimate, gamifies completion. */}
        {pending.length > 0 && (
          <div className="rc-impact" style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap',
               margin: '2px 0 10px', padding: '8px 12px', borderRadius: 8, background: '#F4F8F0', border: '1px solid #CFE3BB' }}>
            <span style={{ fontWeight: 700, color: '#2C5209' }}>🎯 Approving these clears {docsUnblocked} document{docsUnblocked === 1 ? '' : 's'} for certification</span>
            <span className="muted">·</span>
            <span className="muted">about <b>{fmtEst(estToClearS)}</b> of review left ({pending.length} item{pending.length === 1 ? '' : 's'})</span>
          </div>
        )}

        <div className="rc-body">
          {error && <div className="rc-empty">The review queue is unavailable right now. <button className="ghost small" onClick={onRefresh}>Retry</button></div>}
          {actError && (
            <div role="alert" style={{ background: '#FEE2E2', border: '1px solid #F87171', borderRadius: 8, padding: '10px 14px', marginBottom: 10, color: '#991B1B', fontSize: 14 }}>
              ⚠ {actError}{' '}
              <button className="ghost small" onClick={() => setActError(null)} style={{ marginLeft: 8 }}>Dismiss</button>
            </div>
          )}
          {!error && pending.length === 0 && <div className="rc-empty">All caught up — nothing awaiting review. ✓</div>}

          {sections.map((sec) => (
            <section className="rc-type" key={sec.type.key} aria-label={sec.type.label}>
              {/* The review-type header is the contract for everything under it: what kind of
                  work this is, and what clicking approve actually does. Three different jobs
                  must not read as one undifferentiated queue. */}
              <div className={`rc-type-head rc-type-${sec.type.key}`}>
                <span className="rc-type-title">{sec.type.icon} {sec.type.label} <b>· {sec.count}</b></span>
                <span className="muted rc-type-promise">{sec.type.promise}</span>
                {/* Bulk one-click only for the deterministic-confirmation tier: those fixes are
                    already applied + re-validated, so confirming the whole tier is a rubber-stamp.
                    The proposal + authoring tiers deliberately have no bulk action. */}
                {sec.type.key === 'confirm' && sec.count > 1 && (
                  <button className="rc-type-bulk" onClick={() => confirmSection(sec)}
                          title="Confirm every already-applied fix in this section">✓ Confirm all {sec.count}</button>
                )}
              </div>
          {sec.groups.map((grp) => (
            <section className="rc-group" key={grp.label}>
              <div className="rc-group-head">
                <span className="rc-group-title">{grp.label} <span className="muted">· {grp.items.length}</span></span>
                {grp.items.some((it) => isJudgement(it)) && (
                  <button className="ghost small" onClick={() => approveGroup(grp)}>✓ Approve all judgement items</button>
                )}
              </div>
              {grp.items.map((it) => {
                const s = SEV[sevOf(it)] || SEV.medium
                const isOpen = expanded === it.id
                // Confidence (confidence.js) — helps a reviewer triage. When the item carries
                // an AI proposal, the chip reflects it: a validated deterministic proposal is
                // a fast Medium confirm; a subjective (decorative / sensory) one is Low and
                // wants judgement. Otherwise it falls back to detection-method confidence.
                const conf = confidenceForFinding({ sc: scOf(it.rule_id), proposal: proposalMeta(it) })
                return (
                  <div className={`rc-item${isOpen ? ' rc-item-open' : ''}${ordered[cursor]?.id === it.id ? ' rc-item-cursor' : ''}`} key={it.id}>
                    <button className="rc-item-row" onClick={() => setExpanded(isOpen ? null : it.id)} aria-expanded={isOpen}>
                      {/* Two chips, two different things. A bare "Medium" beside a bare "High"
                          reads as a contradiction — say which is which. */}
                      <span className="rc-sevchip" style={{ background: s.bg, color: s.color }}
                            title={`WCAG severity of this criterion: ${s.label}`}>{s.label} severity</span>
                      <RiskChip item={it} compact />
                      <span className="rc-item-file">{it.file || 'document'}</span>
                      {it.finding_count > 1 && <span className="muted rc-item-count">{it.finding_count} findings</span>}
                      <span className="rc-item-reason">⚑ {it.reapprove ? HELD_LABEL : reasonOf(it)}</span>
                      <span className={confClass(conf.level)} title={`Trust signal — how this was detected (tier: ${conf.level.label})`}>{conf.basis}</span>
                      <span className="rc-item-caret" aria-hidden="true">{isOpen ? '▾' : '▸'}</span>
                    </button>
                    {isOpen && (
                      <div className="rc-item-detail">
                        {/* The reviewer reviews the REMEDIATION, not a description of it:
                            thumbnail, the AI's proposed value, the evidence behind the
                            confidence level, and the real before/after diff for this
                            criterion. EvidenceCard owns the write so review telemetry
                            (edited / review_ms / ai_value) is recorded — that is how
                            "review in seconds" gets measured rather than asserted.
                            Keyed by the row's VERSION: the card seeds its editors once, at mount,
                            and HitlBell binds the approval to the row it holds now. A poll that
                            brings a new version must remount the card, or its old text would be
                            approved under the new version's binding (viewedApprovalBinding.js). */}
                        {it.reapprove && (
                          <div className="rc-reapprove" role="group" aria-label={HELD_LABEL}>
                            <p><b>{HELD_LABEL}.</b> {heldExplanation(it)}</p>
                            {reapprovalValues(it).some(Boolean) && <p className="muted">Approves: {reapprovalValues(it).filter(Boolean).map((v) => `“${v}”`).join(' · ')}</p>}
                            <button type="button" className="primary" disabled={busy === it.id} onClick={() => reapprove(it)}>
                              {busy === it.id ? 'Recording approval…' : REAPPROVE_ACTION}</button>
                          </div>
                        )}
                        <EvidenceCard
                          key={viewedBindingKey(it)}
                          item={it}
                          onAct={onAct}
                          onResolved={() => setExpanded(null)}
                          traceUrl={it.scan_id ? openTraceUrl(it.scan_id, 'file', it.file) : null}
                        />
                      </div>
                    )}
                  </div>
                )
              })}
            </section>
          ))}
            </section>
          ))}
          {pending.length > 0 && (
            <p className="muted rc-foot">↻ Re-validated against all engines after each approved fix — only re-passing files advance to publish.</p>
          )}
        </div>
      </div>
    </div>
  )
}
