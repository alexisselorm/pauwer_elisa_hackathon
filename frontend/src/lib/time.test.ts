import { describe, expect, it } from 'vitest'
import type { GraphNode } from '../api/client'
import { ago, fmt, fmtClock, fmtDay, isVisible, ms, stateAt, validToAt } from './time'

const H = 3600_000
const D = 24 * H

const assertion = (over: Partial<GraphNode>): GraphNode => ({
  id: 'A:1',
  kind: 'assertion',
  label: 'statement',
  status: 'believed_true',
  support_n: 0,
  refute_n: 0,
  review_due: false,
  alarms: 0,
  successors: [],
  ...over,
})

describe('stateAt', () => {
  // The Kemi padlock: stated in June, its lock replaced by a code lock recorded on 18 Aug
  // that applies from 2 Aug 21:00 UTC.
  const padlock = assertion({
    id: 'A:padlock',
    status: 'superseded',
    valid_from: '2026-06-01T09:00:00Z',
    valid_to: '2026-08-02T21:00:00Z',
    recorded_at: '2026-06-01T09:00:00Z',
    successors: [{ assertion_id: 'A:codelock', recorded_at: '2026-08-18T10:00:00Z', retracted_at: null }],
  })
  const codelock = assertion({
    id: 'A:codelock',
    status: 'believed_true',
    valid_from: '2026-08-02T21:00:00Z',
    recorded_at: '2026-08-18T10:00:00Z',
  })
  const aug10 = Date.UTC(2026, 7, 10)
  const aug20 = Date.UTC(2026, 7, 20)

  it('ignores a supersession that was not yet on record at k', () => {
    expect(stateAt(padlock, aug10, aug10)).toBe('believed_true')
    expect(stateAt(codelock, aug10, aug10)).toBe('unknown')
  })

  it('applies the supersession once its successor is live at k', () => {
    expect(stateAt(padlock, aug20, aug20)).toBe('ended')
    expect(stateAt(codelock, aug20, aug20)).toBe('believed_true')
  })

  it('shows the past as known today: the padlock had ended by 10 Aug', () => {
    expect(stateAt(padlock, aug10, aug20)).toBe('ended')
    expect(stateAt(codelock, aug10, aug20)).toBe('believed_true')
    expect(stateAt(codelock, Date.UTC(2026, 7, 1), aug20)).toBe('future')
  })

  it('applies a stated expiry with no successor at all', () => {
    const seasonal = assertion({
      status: 'expired',
      valid_from: '2026-01-01T00:00:00Z',
      valid_to: '2026-03-01T00:00:00Z',
      recorded_at: '2026-01-01T00:00:00Z',
    })
    const feb = Date.UTC(2026, 1, 1)
    const apr = Date.UTC(2026, 3, 1)
    expect(stateAt(seasonal, feb, feb)).toBe('believed_true')
    expect(stateAt(seasonal, apr, feb)).toBe('ended')
    expect(stateAt(seasonal, apr, apr)).toBe('ended')
    expect(stateAt({ ...seasonal, successors: undefined }, apr, apr)).toBe('ended')
  })

  it('reopens the predecessor when its only successor is retracted', () => {
    const retracted = assertion({
      ...padlock,
      successors: [{ assertion_id: 'A:codelock', recorded_at: '2026-08-18T10:00:00Z', retracted_at: '2026-08-25T10:00:00Z' }],
    })
    const sep = Date.UTC(2026, 8, 1)
    expect(stateAt(retracted, aug20, aug20)).toBe('ended')
    expect(stateAt(retracted, sep, sep)).toBe('believed_true')
    expect(validToAt(retracted, sep)).toBeNull()
  })

  it('keeps the supersession while any one successor is live', () => {
    const twice = assertion({
      ...padlock,
      successors: [
        { assertion_id: 'A:first', recorded_at: '2026-08-05T10:00:00Z', retracted_at: '2026-08-06T10:00:00Z' },
        { assertion_id: 'A:codelock', recorded_at: '2026-08-18T10:00:00Z', retracted_at: null },
      ],
    })
    expect(stateAt(twice, aug10, aug10)).toBe('believed_true')
    expect(stateAt(twice, aug20, aug20)).toBe('ended')
  })

  it('is future before valid_from, and unknown before it was recorded or once retracted', () => {
    const a = assertion({
      status: 'unverified',
      valid_from: '2026-08-02T21:00:00Z',
      recorded_at: '2026-08-01T00:00:00Z',
      retracted_at: '2026-09-01T00:00:00Z',
    })
    expect(stateAt(a, Date.UTC(2026, 6, 31), Date.UTC(2026, 6, 31))).toBe('unknown')
    expect(stateAt(a, Date.UTC(2026, 7, 2), Date.UTC(2026, 7, 2))).toBe('future')
    expect(stateAt(a, aug10, aug10)).toBe('unverified')
    expect(stateAt(a, aug10, Date.UTC(2026, 8, 1))).toBe('unknown')
  })

  it('falls back to valid_from when recorded_at is missing', () => {
    const a = assertion({ status: 'disputed', valid_from: '2026-08-02T21:00:00Z', recorded_at: null })
    expect(stateAt(a, Date.UTC(2026, 7, 2), Date.UTC(2026, 7, 2))).toBe('unknown')
    expect(stateAt(a, aug10, aug10)).toBe('disputed')
  })
})

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
