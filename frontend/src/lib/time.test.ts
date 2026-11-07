import { describe, expect, it } from 'vitest'
import { ago, fmt, fmtClock, fmtDay, isVisible, ms } from './time'

const H = 3600_000
const D = 24 * H

describe('fmt', () => {
  it('shows UTC instants in Helsinki time', () => {
    expect(fmt('2026-08-02T21:00:00Z')).toBe('3 Aug 2026, 00:00') // summer: UTC+3
    expect(fmt('2026-01-15T10:05:00Z')).toBe('15 Jan 2026, 12:05') // winter: UTC+2
    expect(fmtDay('2026-08-02T21:00:00Z')).toBe('3 Aug 2026')
    expect(fmtClock('2026-08-02T21:00:00Z')).toBe('00:00')
  })

  it('accepts numbers and Dates', () => {
    const at = Date.UTC(2026, 0, 15, 10, 5)
    expect(fmt(at)).toBe('15 Jan 2026, 12:05')
    expect(fmt(new Date(at))).toBe('15 Jan 2026, 12:05')
  })

  it('shows a dash for nothing', () => {
    expect(fmt(null)).toBe('—')
    expect(fmt(undefined)).toBe('—')
    expect(fmtDay(null)).toBe('—')
  })
})

describe('ago', () => {
  const now = Date.UTC(2026, 10, 7, 12, 0)
  const at = (delta: number) => now - delta

  it('rounds to the nearest sensible unit', () => {
    expect(ago(at(10_000), now)).toBe('just now')
    expect(ago(at(44_000), now)).toBe('just now')
    expect(ago(at(45_000), now)).toBe('1 min ago')
    expect(ago(at(30 * 60_000), now)).toBe('30 min ago')
    expect(ago(at(H), now)).toBe('1 h ago')
    expect(ago(at(5 * H), now)).toBe('5 h ago')
    expect(ago(at(D), now)).toBe('1 days ago')
    expect(ago(at(59 * D), now)).toBe('59 days ago')
    expect(ago(at(60 * D), now)).toBe('0.2 years ago')
    expect(ago(at(365 * D), now)).toBe('1.0 years ago')
  })

  it('takes an ISO string too', () => {
    expect(ago(new Date(at(2 * H)).toISOString(), now)).toBe('2 h ago')
  })
})

describe('ms', () => {
  it('parses an ISO instant and leaves nothing as null', () => {
    expect(ms('2026-08-02T21:00:00Z')).toBe(Date.UTC(2026, 7, 2, 21))
    expect(ms(null)).toBeNull()
    expect(ms(undefined)).toBeNull()
    expect(ms('')).toBeNull()
  })
})

describe('isVisible', () => {
  it('hides what is not on record or not yet valid', () => {
    expect(isVisible('unknown')).toBe(false)
    expect(isVisible('future')).toBe(false)
    expect(isVisible('ended')).toBe(true)
    expect(isVisible('believed_true')).toBe(true)
    expect(isVisible('unverified')).toBe(true)
    expect(isVisible('disputed')).toBe(true)
  })
})
