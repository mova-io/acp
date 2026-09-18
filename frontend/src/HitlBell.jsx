import { useState, useEffect, useCallback, useRef } from 'react'
import { listAllHitl, updateHitlItem } from './api.js'
import { SIM } from './sim.js'
import { requestReviewQueueRefresh, viewedDecisionOptions, viewedVersionConflict, viewedVersionMissingError } from './viewedApprovalBinding.js'
import { metaFor, SEV, sevOf, reasonOf, priorityScore, bellSeverity } from './hitlMeta.js'
import ReviewCenter from './ReviewCenter.jsx'

// Global "Review queue" entry point — a notification bell in the top nav. The queue is the
// findings awaiting a human decision, not just a counter: colour signals the most-urgent
// pending item, the dropdown previews the top few, and "View all" opens the full-screen
// Review Center.
//
// Named for the WORK, not for how the work was produced (redesign spec R4 §3). "AI Work
// Inbox" described the generator; whether a finding carries an AI draft is an attribute of
// that finding — surfaced on the card — not the identity of the queue it sits in.
export default function HitlBell() {
  const [items, setItems] = useState([])      // ALL items (metrics need resolved ones too)
  const [open, setOpen] = useState(false)      // dropdown
  const [center, setCenter] = useState(false)  // full-screen review center
  const [err, setErr] = useState(false)
  const [viewedNotice, setViewedNotice] = useState(null)   // a refused approval of a no-longer-current version
  const wrap = useRef(null)
  const mounted = useRef(true)
  useEffect(() => () => { mounted.current = false }, [])

  const load = useCallback(() => {
    listAllHitl()
      .then((rows) => { if (mounted.current) { setItems(Array.isArray(rows) ? rows : []); setErr(false) } })
      .catch(() => { if (mounted.current) setErr(true) })
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)   // gentle poll; the inbox is not latency-critical
    return () => clearInterval(t)
  }, [load])

  // Close the dropdown on outside-click / Escape.
  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (wrap.current && !wrap.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open])

  // Unify: any part of the app can open the inbox by dispatching 'acp:open-inbox'.
  useEffect(() => {
    const open = () => { setCenter(true); load() }
    window.addEventListener('acp:open-inbox', open)
    return () => window.removeEventListener('acp:open-inbox', open)
  }, [load])

  const act = useCallback((itemId, status, note = null, approvedValue = null, telemetry = {}) => {
    // Optimistic: mark resolved locally so it leaves `pending` (and the metrics update)
    // immediately — the inbox feels instant. load() reconciles with server truth; a
    // failure reverts to the snapshot so nothing is silently lost.
    //
    // Bound to the VIEWED version — the row the Review Center is rendering, never a re-read — so a
    // re-assessment since it was loaded makes the server refuse (409) instead of recording an approval
    // of a version nobody saw. No binding: nothing is sent, the card says the item must be refreshed,
    // and the queue is re-read. A refusal re-reads the row too, and is never retried automatically.
    let prev
    const viewed = items.find((item) => item.id === itemId)
    const bound = viewedDecisionOptions(viewed, status)
    if (!SIM && status === 'approved' && !bound) {
      load(); requestReviewQueueRefresh()
      return Promise.reject(viewedVersionMissingError())
    }
    const nowIso = new Date().toISOString()
    setViewedNotice(null)
    setItems((cur) => { prev = cur; return cur.map((i) => (i.id === itemId ? { ...i, status, reviewed_at: nowIso } : i)) })
    return updateHitlItem(itemId, status, note, approvedValue, { ...telemetry, ...bound })
      .then(() => { load(); window.dispatchEvent(new Event('acp:hitl-changed')) })
      .catch((e) => {
        if (prev) setItems(prev)
        const conflict = viewedVersionConflict(e)
        if (!conflict) throw e
        // The optimistic update took the item out of `pending`, so the card that was clicked has
        // unmounted and will come back fresh on the current version — it cannot carry this message.
        // The bell states it instead, above the Review Center.
        const file = viewed?.file ? ` for “${viewed.file}”` : ''
        setViewedNotice(`Approval${file} not saved: ${conflict.message}`)
        load(); requestReviewQueueRefresh()
        throw conflict
      })
  }, [load, items])

  const pending = items.filter((i) => i.status === 'pending')
  const sev = bellSeverity(pending)
  const tally = { high: 0, medium: 0, low: 0 }
  pending.forEach((i) => { tally[sevOf(i)] = (tally[sevOf(i)] || 0) + 1 })
  const top = [...pending].sort((a, b) => priorityScore(b) - priorityScore(a)).slice(0, 4)

  return (
    <div className="hitlbell" ref={wrap}>
      <button
        className={`hitlbell-btn hitlbell-${sev}`}
        aria-label={`Review queue — ${pending.length} pending`}
        aria-expanded={open}
        title={err ? 'Review queue (unavailable)' : `Review queue — ${pending.length} pending`}
        onClick={() => setOpen((v) => !v)}>
        <span aria-hidden="true">🔔</span>
        {pending.length > 0 && <span className="hitlbell-badge">{pending.length > 99 ? '99+' : pending.length}</span>}
      </button>

      {open && (
        <div className="hitlbell-pop" role="menu">
          <div className="hitlbell-pophead">
            <b>Review queue</b>
            <span className="muted">{pending.length} awaiting approval</span>
          </div>
          <div className="hitlbell-tally">
            <span className="hsev hsev-high">● {tally.high} High</span>
            <span className="hsev hsev-medium">● {tally.medium} Medium</span>
            <span className="hsev hsev-low">● {tally.low} Low</span>
          </div>
          <div className="hitlbell-list">
            {top.length === 0 && (
              <div className="hitlbell-empty">{err ? 'Queue unavailable right now.' : 'All caught up — nothing awaiting review. ✓'}</div>
            )}
            {top.map((it) => {
              const s = SEV[sevOf(it)] || SEV.medium
              return (
                <button key={it.id} className="hitlbell-item" role="menuitem"
                        onClick={() => { setOpen(false); setCenter(true) }}>
                  <span className="hitlbell-dot" style={{ background: s.dot }} aria-hidden="true" />
                  <span className="hitlbell-item-main">
                    <span className="hitlbell-item-file">{it.file || 'document'}</span>
                    <span className="hitlbell-item-rule">{it.rule_name || it.rule_id}</span>
                    <span className="hitlbell-item-why">{reasonOf(it)}</span>
                  </span>
                </button>
              )
            })}
          </div>
          <button className="hitlbell-viewall" onClick={() => { setOpen(false); setCenter(true) }}>
            View all →
          </button>
        </div>
      )}

      {center && (
        <ReviewCenter items={items} onAct={act} onClose={() => { setCenter(false); setViewedNotice(null) }} onRefresh={load} error={err} />
      )}
      {center && viewedNotice && (
        <div role="alert" className="hitlbell-viewed-version"
             style={{ position: 'fixed', top: 12, left: '50%', transform: 'translateX(-50%)', zIndex: 81,
                      maxWidth: 'min(720px, 94vw)', padding: '10px 14px', borderRadius: 8, fontSize: 13,
                      border: '1px solid #C0392B', background: '#FDEDEC', color: '#7B241C',
                      boxShadow: '0 8px 24px rgba(28,22,32,0.22)', display: 'flex', gap: 10, alignItems: 'flex-start' }}>
          <span>{viewedNotice} The queue is reloading the current version.</span>
          <button type="button" className="ghost small" aria-label="Dismiss" onClick={() => setViewedNotice(null)}>✕</button>
        </div>
      )}
    </div>
  )
}
