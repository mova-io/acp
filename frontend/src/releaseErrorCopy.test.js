import { describe, expect, it } from 'vitest'
import { releaseErrorCopy } from './releaseErrorCopy.js'

const RAW_404 = "HTTPStatusError: Client error '404 Not Found' for url 'https://graph.microsoft.com/v1.0/drives/b!sensitive/items/01secret/content'"

describe('releaseErrorCopy', () => {
  it('turns a destination 404 into actionable copy without leaking provider identifiers', () => {
    const copy = releaseErrorCopy({ status: 404, message: RAW_404 }, 'SharePoint')
    expect(copy.message).toMatch(/moved or been deleted/i)
    expect(copy.retryable).toBe(false)
    expect(copy.diagnosticCode).toBe('destination_not_found')
    expect(copy.message).not.toMatch(/graph\.microsoft\.com|b!sensitive|01secret|HTTPStatusError/)
  })

  it('only recommends reconnecting for a 401 authorization failure', () => {
    expect(releaseErrorCopy({ status: 401 }, 'SharePoint').message).toMatch(/Reconnect/)
    const denied = releaseErrorCopy({ status: 403 }, 'SharePoint')
    expect(denied.message).toMatch(/permission|write access/i)
    expect(denied.message).not.toMatch(/Reconnect/)
    expect(denied.retryable).toBe(false)
    expect(releaseErrorCopy({ status: 404 }, 'SharePoint').message).not.toMatch(/Reconnect/)
  })

  it('does not retry a structured destination permission block', () => {
    const copy = releaseErrorCopy({ status: 409, detail: { code: 'release_destination_not_ready', preflight: { message: 'Folder permission denied' } } }, 'SharePoint')
    expect(copy.diagnosticCode).toBe('destination_write_access_required')
    expect(copy.retryable).toBe(false)
    expect(copy.message).not.toMatch(/Folder permission denied/)
  })

  it('does not invent a missing-folder diagnosis for a generic 404', () => {
    const copy = releaseErrorCopy({ status: 404, message: 'API route not found' }, 'SharePoint')
    expect(copy.diagnosticCode).toBe('release_resource_unavailable')
    expect(copy.message).not.toMatch(/moved|deleted/i)
    expect(copy.retryable).toBe(false)
  })

  it('allows a retry for temporary transport failures and safe unknown failures', () => {
    expect(releaseErrorCopy(new Error('504 Gateway Timeout')).retryable).toBe(true)
    expect(releaseErrorCopy(new Error('unexpected provider response')).retryable).toBe(true)
  })
})
