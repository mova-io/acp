import { act, createElement } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import ReleaseRecoveryActions from './ReleaseRecoveryActions.jsx'
afterEach(unmountAll)
async function mount(props = {}) {
 const { root, container } = createTestRoot()
 await act(async () => root.render(createElement(ReleaseRecoveryActions, {
  error: { diagnosticCode: 'destination_not_found' },
  destinationPicker: createElement('button', {}, 'Choose destination'), ...props,
 })))
 return container
}
const click = element => act(async () => element.click())
it('offers existing picker and authoritative status read without publishing', async () => {
 const check = vi.fn().mockResolvedValue(true)
 const c = await mount({ onCheckStatus: check })
 expect(c.textContent).toContain('Choose destination')
 await click([...c.querySelectorAll('button')].find(b => b.textContent === 'Check delivery status'))
 expect(check).toHaveBeenCalledOnce()
 expect(c.textContent).toContain('Status refreshed')
})
it('preserves approved destination', async () => {
 const c = await mount({ destinationLocked: true })
 expect(c.textContent).not.toContain('Choose destination')
 expect(c.textContent).toContain('requires a new release plan')
})
it('routes authorization recovery to existing source screen', async () => {
 const reconnect = vi.fn()
 const c = await mount({ error: { diagnosticCode: 'destination_authorization_required' }, onReconnect: reconnect })
 await click(c.querySelector('button'))
 expect(reconnect).toHaveBeenCalledOnce()
})
it('blocks recovery in read-only/busy views', async () => {
 const check = vi.fn()
 const c = await mount({ disabled: true, onCheckStatus: check })
 expect(c.textContent).not.toContain('Choose destination')
 await click(c.querySelector('button'))
 expect(check).not.toHaveBeenCalled()
})
it('does not claim recovery on failed status reads', async () => {
 const c = await mount({ onCheckStatus: vi.fn().mockResolvedValue(false) })
 await click([...c.querySelectorAll('button')].find(b => b.textContent === 'Check delivery status'))
 expect(c.textContent).toContain('Status is still unavailable')
})
it('does not invent recovery for unrelated errors', async () => {
 const c = await mount({ error: { diagnosticCode: 'unknown' } })
 expect(c.textContent).toBe('')
})
