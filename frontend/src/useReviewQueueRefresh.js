import { useCallback, useEffect, useRef } from 'react'
import { listHitlQueue } from './api.js'
import { authEpoch } from './apiIdentity.js'

const POLL_MS = 5000
const TIMEOUT_MS = 20000

// Refresh recorded decisions, not optimistic counts. Consent alone cannot prove a
// proposal was admitted, applied, or verified. One reader owns every queue refresh.
export default function useReviewQueueRefresh({ scanId, batchId, approval, progressKey, active, disabled = false, onRows, onError }) {
  const epoch = authEpoch()
  const identity = `${epoch}:${scanId || ''}:${batchId || ''}`
  const current = useRef(identity)
  current.current = identity
  const callbacks = useRef({onRows,onError})
  callbacks.current = {onRows,onError}
  const request = useRef(null)
  const refresh = useCallback(() => request.current?.(), [])
  useEffect(() => {
    if (!scanId || disabled) return undefined
    let live = true, pending = false, again = false, controller, deadline, cancelPending
    const owns = () => live && current.current === identity && authEpoch() === epoch
    const load = async () => {
      if (!owns()) return
      if (pending) { again = true; return }
      pending = true
      controller = new AbortController()
      const signal = controller.signal
      let rejectDeadline
      const timeout = new Promise((resolve,reject) => { rejectDeadline=reject })
      cancelPending = () => {controller.abort();rejectDeadline(new DOMException('Review refresh cancelled','AbortError'))}
      deadline = window.setTimeout(() => {
        controller.abort()
        rejectDeadline(new Error('Review queue refresh timed out.'))
      },TIMEOUT_MS)
      try {
        // Target-replaced rows are terminal results the workspace must be able to show.
        const rows = await Promise.race([listHitlQueue(scanId,null,{signal,includeTargetReplaced:true}),timeout])
        if (owns()) callbacks.current.onRows?.(rows)
      } catch (error) {
        if (owns()) {
          if ([401,403,404].includes(error?.status)) { live=false; again=false }
          callbacks.current.onError?.(error)
        }
      } finally {
        clearTimeout(deadline)
        cancelPending=null
        pending=false
        if (owns() && again) { again=false; load() }
      }
    }
    request.current=load
    const changed = event => {
      if (event.detail?.scanId && event.detail.scanId !== scanId) return
      if (event.detail?.runId && event.detail.runId !== batchId) return
      load()
    }
    window.addEventListener('acp:hitl-changed',changed)
    const expired = () => {live=false;again=false;cancelPending?.();clearTimeout(deadline)}
    window.addEventListener('acp:session-expired',expired)
    load()
    return () => {
      live=false;again=false;cancelPending?.();clearTimeout(deadline)
      if (request.current===load) request.current=null
      window.removeEventListener('acp:hitl-changed',changed)
      window.removeEventListener('acp:session-expired',expired)
    }
  },[identity,disabled])
  useEffect(() => { refresh() },[identity,approval?.enabled,approval?.revision,refresh])
  useEffect(() => {
    const timer=window.setTimeout(refresh,800)
    return () => clearTimeout(timer)
  },[identity,progressKey,refresh])
  useEffect(() => {
    if (!active || disabled) return undefined
    const timer=window.setInterval(refresh,POLL_MS)
    return () => clearInterval(timer)
  },[identity,active,disabled,refresh])
  return refresh
}
