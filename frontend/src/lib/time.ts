import type { GraphNode, Successor } from '../api/client'

// Elisa's time conventions: stored in UTC, shown in Europe/Helsinki.
const TZ = 'Europe/Helsinki'
const full = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ, day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
})
const day = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, day: 'numeric', month: 'short', year: 'numeric' })
const clock = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit' })

export const fmt = (t: string | number | Date | null | undefined) => (t ? full.format(new Date(t)) : '—')
export const fmtDay = (t: string | number | Date | null | undefined) => (t ? day.format(new Date(t)) : '—')
export const fmtClock = (t: string | number | Date) => clock.format(new Date(t))

export function ago(t: string | number | Date, now = Date.now()): string {
  const s = Math.round((now - new Date(t).getTime()) / 1000)
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  const d = Math.round(s / 86400)
  if (d < 60) return `${d} days ago`
  return `${(d / 365).toFixed(1)} years ago`
}

export const ms = (t: string | null | undefined) => (t ? new Date(t).getTime() : null)

/** What an assertion was at time t, as known at k (Elisa's valid_from / valid_to model).
 *  - unknown: not on record yet at k (or retracted by then)
 *  - future: on record, but its validity starts after t
 *  - ended: its validity ended at or before t (superseded or expired), as known at k
 *  - otherwise its status; superseded/expired knowledge was current before it ended. */
export type TimeState = 'unknown' | 'future' | 'ended' | 'believed_true' | 'unverified' | 'disputed'

/** Whether a successor was on record at k: recorded by then and not retracted by then. */
const liveAt = (s: Successor, k: number) => ms(s.recorded_at)! <= k && (ms(s.retracted_at) ?? Infinity) > k

/** The valid_to in force as known at k, mirroring the backend. A stored valid_to applies when
 *  nothing supersedes the assertion (a stated expiry) or while a successor is live at k. With
 *  successors but none live at k, the assertion is open-ended again, however its record reads. */
export function validToAt(a: GraphNode, k: number): number | null {
  const successors = a.successors ?? []
  if (successors.length > 0 && !successors.some((s) => liveAt(s, k))) return null
  return ms(a.valid_to)
}

export function stateAt(a: GraphNode, t: number, k: number): TimeState {
  const recorded = ms(a.recorded_at) ?? ms(a.valid_from)!
  const retracted = ms(a.retracted_at)
  if (recorded > k || (retracted !== null && retracted <= k)) return 'unknown'
  if (ms(a.valid_from)! > t) return 'future'
  const vt = validToAt(a, k)
  if (vt !== null && vt <= t) return 'ended'
  const s = a.status ?? 'unverified'
  // Before its successor was recorded (or after that was retracted), it was simply believed.
  if (s === 'superseded' || s === 'expired') return 'believed_true'
  return s as TimeState
}

export const isVisible = (s: TimeState) => s !== 'unknown' && s !== 'future'
