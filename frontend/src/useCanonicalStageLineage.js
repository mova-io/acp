import { useCallback, useEffect, useRef, useState } from 'react'

const POLL_MS = 15_000
// Ask for an immediate re-read of the stage lineage, e.g. after an action that changed the
// server's finding ledger (Remediate's "re-check remaining items"). detail: { scanId } — a request
// for another scan is ignored; one without a scanId refreshes whatever is shown.
export const STAGE_LINEAGE_REFRESH_EVENT = 'acp:stage-lineage-refresh'

function lineageVersion(value) {
  const lineage = value?.lineage || value
  const stageRevision = Math.max(0, ...(lineage?.stages || []).map((stage) => Number(stage.revision || 0)))
  return [Number(lineage?.workflow_revision || 0), stageRevision]
}

const STAGE_ORDER = { discover: 0, assess: 1, remediate: 2, release: 3 }

function furthestStage(value) {
  const lineage = value?.lineage || value
  return Math.max(-1, ...(lineage?.stages || []).map((stage) => STAGE_ORDER[stage.stage] ?? -1))
}

export function isNewerLineage(previous, next) {
  if (!next) return false
  if (!previous) return true
  const [previousWorkflow, previousStage] = lineageVersion(previous)
  const [nextWorkflow, nextStage] = lineageVersion(next)
  if (nextWorkflow !== previousWorkflow) return nextWorkflow > previousWorkflow
  // A full lineage response may update counters without incrementing its revision, so equality is
  // valid. It may not forget a downstream stage or lower the greatest durable stage revision.
  return furthestStage(next) >= furthestStage(previous) && nextStage >= previousStage
}

export function useCanonicalStageLineage(scanId, loadLineage) {
  const [result, setResult] = useState(null)
  const [receivedAt, setReceivedAt] = useState(null)
  const latest = useRef(null)

  const accept = useCallback((next) => {
    if (!isNewerLineage(latest.current, next)) return
    latest.current = next
    setResult(next)
    setReceivedAt(Date.now())
  }, [])

  useEffect(() => {
    latest.current = null
    setResult(null)
    setReceivedAt(null)
    if (!scanId || typeof loadLineage !== 'function') return undefined

    let live = true
    const refresh = () => {
      if (!live || document.hidden) return
      loadLineage(scanId).then((next) => { if (live) accept(next) }).catch(() => {})
    }
    const stop = () => { live = false }
    refresh()
    const poll = setInterval(refresh, POLL_MS)
    const requested = (event) => { if (!event?.detail?.scanId || event.detail.scanId === scanId) refresh() }
    window.addEventListener('focus', refresh)
    window.addEventListener(STAGE_LINEAGE_REFRESH_EVENT, requested)
    window.addEventListener('acp:session-expired', stop)
    return () => {
      live = false
      clearInterval(poll)
      window.removeEventListener('focus', refresh)
      window.removeEventListener(STAGE_LINEAGE_REFRESH_EVENT, requested)
      window.removeEventListener('acp:session-expired', stop)
    }
  }, [scanId, loadLineage, accept])

  return { lineage: result?.lineage || result, contentDigest: result?.content_digest || null,
    receivedAt }
}
