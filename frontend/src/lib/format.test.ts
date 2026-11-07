import { describe, expect, it } from 'vitest'
import { pretty, STATUS_LABEL, statusColor, TONE, traceUrl } from './format'

describe('statusColor', () => {
  it('maps each live state to its own colour', () => {
    expect(statusColor('believed_true')).toBe('var(--g-believed)')
    expect(statusColor('disputed')).toBe('var(--g-disputed)')
    expect(statusColor('unverified')).toBe('var(--g-unverified)')
  })

  it('draws everything else, including nothing, as ended', () => {
    for (const s of ['ended', 'superseded', 'expired', 'future', 'unknown', null, undefined]) {
      expect(statusColor(s)).toBe('var(--g-ended)')
    }
  })
})

describe('traceUrl', () => {
  it('needs both a Phoenix base and a trace id', () => {
    expect(traceUrl(null, 'abc')).toBeNull()
    expect(traceUrl('http://phoenix:6006', null)).toBeNull()
    expect(traceUrl(undefined, undefined)).toBeNull()
    expect(traceUrl('', 'abc')).toBeNull()
  })

  it('builds the redirect link without doubling the slash', () => {
    expect(traceUrl('http://phoenix:6006', 'abc')).toBe('http://phoenix:6006/redirects/traces/abc')
    expect(traceUrl('http://phoenix:6006/', 'abc')).toBe('http://phoenix:6006/redirects/traces/abc')
  })
})

describe('pretty', () => {
  it('turns every underscore into a space', () => {
    expect(pretty('report_outcome')).toBe('report outcome')
    expect(pretty('a_b_c')).toBe('a b c')
    expect(pretty('plain')).toBe('plain')
  })
})

describe('status tables', () => {
  it('give every labelled status a tone', () => {
    for (const s of Object.keys(STATUS_LABEL)) expect(TONE).toHaveProperty(s)
  })
})
