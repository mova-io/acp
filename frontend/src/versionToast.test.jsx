import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createRoot } from 'react-dom/client'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const getConfig = vi.fn()
vi.mock('./api', () => ({ getConfig: (...a) => getConfig(...a) }))

import VersionToast, { VersionToastBanner } from './VersionToast.jsx'

// VersionToast: polls /config every 10 min and shows a reload banner when the server
// version advances past what the page loaded with.

const POLL_MS = 10 * 60 * 1000
const here = dirname(fileURLToPath(import.meta.url))
const noop = () => {}

describe('VersionToast initial state', () => {
  it('renders nothing by default (update not yet detected)', () => {
    const html = renderToStaticMarkup(createElement(VersionToast, { currentVersion: '2026.8.25.1' }))
    expect(html).toBe('')
  })

  it('renders nothing when currentVersion is null (config not loaded yet)', () => {
    const html = renderToStaticMarkup(createElement(VersionToast, { currentVersion: null }))
    expect(html).toBe('')
  })
})

describe('VersionToastBanner UI', () => {
  const html = () => renderToStaticMarkup(createElement(VersionToastBanner, { onReload: noop, onDismiss: noop }))

  it('contains "new version of ACP is available" label', () => {
    expect(html()).toContain('new version of ACP is available')
  })

  it('has a Reload button', () => {
    expect(html()).toContain('Reload')
  })

  it('has a dismiss button with aria-label', () => {
    expect(html()).toContain('Dismiss version notification')
  })

  it('uses role="status" and aria-live="polite" (never assertive)', () => {
    expect(html()).toContain('role="status"')
    expect(html()).toContain('aria-live="polite"')
    expect(html()).not.toContain('assertive')
    expect(html()).not.toContain('role="alert"')
  })

  it('is position fixed (does not displace content)', () => {
    expect(html()).toContain('position:fixed')
  })

  it('hides the decorative ✦ from the announcement', () => {
    const doc = new DOMParser().parseFromString(html(), 'text/html')
    const status = doc.querySelector('[role="status"]')
    const glyph = [...status.querySelectorAll('[aria-hidden="true"]')].find((n) => n.textContent.includes('✦'))
    expect(glyph).toBeTruthy()
    // What remains exposed in the message span is the plain sentence.
    const msg = status.querySelector('span')
    const exposed = [...msg.childNodes].filter((n) => !(n.nodeType === 1 && n.getAttribute('aria-hidden') === 'true'))
      .map((n) => n.textContent).join('')
    expect(exposed).toBe('A new version of ACP is available.')
  })
})

// ── WCAG 1.4.3 / 1.4.11 ─────────────────────────────────────────────────────────────────────
// The September 2026 vendor self-assessment reported insufficient text contrast on this banner; it
// gave no ratio. By our own calculation from the source, the old white 14px/500 text on #16a34a is
// 3.30:1. These tests read the colours the banner actually renders (its inline styles) and the toast's own focus rule
// from disk, and compute WCAG 2.x contrast. Reverting the background to #16a34a fails them.

function parseColor(value) {
  const v = value.trim().toLowerCase()
  if (v === 'transparent') return { r: 0, g: 0, b: 0, a: 0 }
  if (v === 'white') return { r: 255, g: 255, b: 255, a: 1 }
  let m = v.match(/^#([0-9a-f]{3})$/)
  if (m) return { r: parseInt(m[1][0].repeat(2), 16), g: parseInt(m[1][1].repeat(2), 16), b: parseInt(m[1][2].repeat(2), 16), a: 1 }
  m = v.match(/^#([0-9a-f]{6})$/)
  if (m) return { r: parseInt(m[1].slice(0, 2), 16), g: parseInt(m[1].slice(2, 4), 16), b: parseInt(m[1].slice(4, 6), 16), a: 1 }
  m = v.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/)
  if (m) return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] }
  throw new Error(`unparsed colour: ${value}`)
}

// Composite a (possibly translucent) colour over an opaque one.
function over(fg, bg) {
  const a = fg.a
  return { r: fg.r * a + bg.r * (1 - a), g: fg.g * a + bg.g * (1 - a), b: fg.b * a + bg.b * (1 - a), a: 1 }
}

function luminance({ r, g, b }) {
  const lin = (c) => { const s = c / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4 }
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

function styleOf(el) {
  const out = {}
  for (const decl of (el.getAttribute('style') || '').split(';')) {
    const i = decl.indexOf(':')
    if (i > 0) out[decl.slice(0, i).trim()] = decl.slice(i + 1).trim()
  }
  return out
}

function bannerParts() {
  const markup = renderToStaticMarkup(createElement(VersionToastBanner, { onReload: noop, onDismiss: noop }))
  const doc = new DOMParser().parseFromString(markup, 'text/html')
  const banner = doc.querySelector('[role="status"]')
  const [reload, dismiss] = banner.querySelectorAll('button')
  return { banner, reload, dismiss }
}

describe('VersionToast colour contrast', () => {
  it('message text meets 4.5:1 on the banner (the self-assessment reported insufficient contrast here)', () => {
    const { banner } = bannerParts()
    const s = styleOf(banner)
    const bg = parseColor(s.background)
    expect(bg.a).toBe(1)
    const fg = over(parseColor(s.color), bg)
    // The message span and ✦ inherit the banner colour — no inline override.
    for (const span of banner.querySelectorAll('span')) {
      expect(styleOf(span).color).toBeUndefined()
      expect(styleOf(span).opacity).toBeUndefined()
    }
    expect(styleOf(banner).opacity).toBeUndefined()
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(4.5)
  })

  it('"Reload now" label meets 4.5:1 and the button boundary meets 3:1 on the banner', () => {
    const { banner, reload } = bannerParts()
    const bannerBg = parseColor(styleOf(banner).background)
    const s = styleOf(reload)
    const btnBg = over(parseColor(s.background), bannerBg)
    const label = over(parseColor(s.color), btnBg)
    expect(contrast(label, btnBg)).toBeGreaterThanOrEqual(4.5)
    expect(contrast(btnBg, bannerBg)).toBeGreaterThanOrEqual(3)
  })

  it('dismiss ✕ (transparent button) meets 4.5:1 against the banner', () => {
    const { banner, dismiss } = bannerParts()
    const bannerBg = parseColor(styleOf(banner).background)
    const s = styleOf(dismiss)
    const btnBg = over(parseColor(s.background), bannerBg)
    const glyph = over(parseColor(s.color), btnBg)
    expect(contrast(glyph, btnBg)).toBeGreaterThanOrEqual(4.5)
  })

  it('both controls use the toast focus ring, which meets 3:1 on the banner in every theme', () => {
    const { banner, reload, dismiss } = bannerParts()
    expect(banner.classList.contains('acp-version-toast')).toBe(true)
    expect(reload.classList.contains('acp-version-toast__control')).toBe(true)
    expect(dismiss.classList.contains('acp-version-toast__control')).toBe(true)

    // The stylesheet is actually loaded by the component.
    const src = readFileSync(join(here, 'VersionToast.jsx'), 'utf8')
    expect(src).toMatch(/import\s+['"]\.\/version-toast\.css['"]/)

    const css = readFileSync(join(here, 'version-toast.css'), 'utf8')
    const rule = css.match(/\.acp-version-toast\s+\.acp-version-toast__control:focus-visible\s*\{([^}]*)\}/)
    expect(rule).toBeTruthy()
    const outline = rule[1].match(/outline\s*:\s*([\d.]+)px\s+solid\s+(#[0-9a-fA-F]{3,6})\s*!important/)
    expect(outline).toBeTruthy()
    expect(Number(outline[1])).toBeGreaterThanOrEqual(2)
    // !important + (0,3,0) specificity beats styles.css `html[data-wcag="on"] :focus-visible`
    // (0,2,1, !important), so the WCAG theme's #005fcc ring (1.19:1 here) cannot replace it.
    expect(rule[1]).toMatch(/outline-offset\s*:[^;]*!important/)
    const ring = parseColor(outline[2])
    const bannerBg = parseColor(styleOf(banner).background)
    expect(contrast(ring, bannerBg)).toBeGreaterThanOrEqual(3)
  })

  it('contrast helper confirms the old colours fail (our calculation; guards the maths, not the component)', () => {
    const white = parseColor('#fff')
    expect(contrast(white, parseColor('#16a34a'))).toBeLessThan(4.5)       // 3.30:1 by our calculation
    expect(contrast(white, parseColor('#15803d'))).toBeCloseTo(5.02, 1)
    expect(contrast(over(parseColor('rgba(255,255,255,.8)'), parseColor('#16a34a')), parseColor('#16a34a'))).toBeLessThan(3)
  })
})

// ── WCAG 1.4.10 / 1.4.4 / 2.5.8 layout guarantees ──────────────────────────────────────────
// jsdom has no layout engine, so these pin the CSS that prevents overlap rather than measuring
// it. The ✕ is absolutely positioned and invisible to flex layout. Each side must reserve at
// least the ✕'s right offset + its width + its focus ring, or the centred content runs under it
// on narrow screens. Real rendering at 320px / 200% text still needs a manual browser check.

const px = (v) => {
  const m = String(v ?? '').trim().match(/^(-?[\d.]+)px$/)
  if (!m) throw new Error(`expected a px length, got: ${v}`)
  return Number(m[1])
}

function paddingSides(s) {
  const parts = (s.padding || '0px').trim().split(/\s+/)
  const [t, r = t, b = t, l = r] = parts
  return {
    top: px(s['padding-top'] ?? t), right: px(s['padding-right'] ?? r),
    bottom: px(s['padding-bottom'] ?? b), left: px(s['padding-left'] ?? l),
  }
}

function focusRingExtent() {
  const css = readFileSync(join(here, 'version-toast.css'), 'utf8')
  const rule = css.match(/\.acp-version-toast\s+\.acp-version-toast__control:focus-visible\s*\{([^}]*)\}/)[1]
  const width = Number(rule.match(/outline\s*:\s*([\d.]+)px/)[1])
  const offset = Number(rule.match(/outline-offset\s*:\s*([\d.]+)px/)[1])
  return width + offset
}

describe('VersionToast narrow-screen layout', () => {
  it('lets the row wrap so "Reload now" can drop below the message', () => {
    const { banner } = bannerParts()
    const s = styleOf(banner)
    expect(s.display).toBe('flex')
    expect(s['flex-wrap']).toBe('wrap')
  })

  it('message can shrink and break inside the flex row', () => {
    const { banner } = bannerParts()
    const msg = banner.querySelector('span')
    expect(msg.textContent).toContain('A new version of ACP is available.')
    const s = styleOf(msg)
    expect(s['min-width']).toMatch(/^0(px)?$/)
    expect(['anywhere', 'break-word']).toContain(s['overflow-wrap'])
  })

  it('both sides reserve room for the absolutely-positioned ✕ and its focus ring', () => {
    const { banner, dismiss } = bannerParts()
    const d = styleOf(dismiss)
    expect(d.position).toBe('absolute')
    const needed = px(d.right) + px(d.width) + focusRingExtent()
    const pad = paddingSides(styleOf(banner))
    expect(pad.right).toBeGreaterThanOrEqual(needed)
    // Symmetric, so the message stays visually centred.
    expect(pad.left).toBe(pad.right)
    // The focus ring stays inside the viewport on the right edge.
    expect(px(d.right)).toBeGreaterThanOrEqual(focusRingExtent())
    // At 320 CSS px the content box is still wide enough for the longest word at 200% text
    // ("available." ≈ 10 glyphs × ~0.6em × 28px ≈ 170px) and for "Reload now" at 200%.
    expect(320 - pad.left - pad.right).toBeGreaterThanOrEqual(200)
  })

  it('dismiss ✕ and "Reload now" are at least 24×24 CSS px targets (2.5.8)', () => {
    const { reload, dismiss } = bannerParts()
    const d = styleOf(dismiss)
    expect(px(d.width)).toBeGreaterThanOrEqual(24)
    expect(px(d.height)).toBeGreaterThanOrEqual(24)
    expect(px(styleOf(reload)['min-height'])).toBeGreaterThanOrEqual(24)
  })
})

// ── Behaviour ──────────────────────────────────────────────────────────────────────────────
describe('VersionToast polling behaviour', () => {
  let host, root
  beforeEach(() => {
    vi.useFakeTimers()
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
  })
  afterEach(() => {
    act(() => root.unmount())
    host.remove()
    vi.useRealTimers()
    getConfig.mockReset()
  })

  async function tick() {
    await act(async () => {
      vi.advanceTimersByTime(POLL_MS)
      await Promise.resolve()
      await Promise.resolve()
    })
  }

  it('shows the polite banner once the server version advances, and dismiss hides it', async () => {
    getConfig.mockResolvedValue({ version: '2026.9.17.2' })
    await act(async () => { root.render(<VersionToast currentVersion="2026.9.17.1" />) })
    expect(host.querySelector('[role="status"]')).toBeNull()
    await tick()
    expect(getConfig).toHaveBeenCalledTimes(1)
    const status = host.querySelector('[role="status"]')
    expect(status).not.toBeNull()
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toContain('A new version of ACP is available.')
    await act(async () => { host.querySelector('button[aria-label="Dismiss version notification"]').click() })
    expect(host.querySelector('[role="status"]')).toBeNull()
  })

  it('stays hidden while the server reports the same version', async () => {
    getConfig.mockResolvedValue({ version: '2026.9.17.1' })
    await act(async () => { root.render(<VersionToast currentVersion="2026.9.17.1" />) })
    await tick()
    expect(getConfig).toHaveBeenCalledTimes(1)
    expect(host.querySelector('[role="status"]')).toBeNull()
  })

  it('does not poll when the loaded version is unknown', async () => {
    getConfig.mockResolvedValue({ version: '2026.9.17.2' })
    await act(async () => { root.render(<VersionToast currentVersion={null} />) })
    await tick()
    expect(getConfig).not.toHaveBeenCalled()
  })

  it('banner buttons call onReload / onDismiss', async () => {
    const onReload = vi.fn()
    const onDismiss = vi.fn()
    await act(async () => { root.render(<VersionToastBanner onReload={onReload} onDismiss={onDismiss} />) })
    const [reload, dismiss] = host.querySelectorAll('button')
    expect(reload.textContent).toContain('Reload now')
    await act(async () => { reload.click() })
    await act(async () => { dismiss.click() })
    expect(onReload).toHaveBeenCalledTimes(1)
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})
