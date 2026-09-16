import { describe, expect, it } from 'vitest'
import { releaseErrorCopy } from './releaseErrorCopy.js'

const RAW_404 = "HTTPStatusError: Client error '404 Not Found' for url 'https://graph.microsoft.com/v1.0/drives/b!sensitive/items/01secret/content'"

describe('releaseErrorCopy', () => {
  it('turns a destination 404 into actionable copy without leaking provider identifiers', () => {
    const copy = releaseErrorCopy({ status: 404, message: RAW_404 }, 'SharePoint')
    expect(copy.message).toMatch(/moved or deleted/i)
    expect(copy.retryable).toBe(false)
    expect(copy.diagnosticCode).toBe('destination_not_found')
    expect(copy.message).not.toMatch(/graph\.microsoft\.com|b!sensitive|01secret|HTTPStatusError/)
  })

  it('only recommends reconnecting for an authorization failure', () => {
    expect(releaseErrorCopy({ status: 401 }, 'SharePoint').message).toMatch(/Reconnect/)
    expect(releaseErrorCopy({ status: 404 }, 'SharePoint').message).not.toMatch(/Reconnect/)
  })

  it('allows a retry for temporary transport failures and safe unknown failures', () => {
    expect(releaseErrorCopy(new Error('504 Gateway Timeout')).retryable).toBe(true)
    expect(releaseErrorCopy(new Error('unexpected provider response')).retryable).toBe(true)
  })
})
