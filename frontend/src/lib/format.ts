import type { TimeState } from './time'

export const STATUS_LABEL: Record<string, string> = {
  believed_true: 'Believed true',
  unverified: 'Unverified',
  disputed: 'Disputed',
  superseded: 'Superseded',
  expired: 'Expired',
  ended: 'Ended',
  captured: 'captured',
  ignored: 'ignored',
  extraction_failed: 'failed',
  pending: 'pending',
}

export const TONE: Record<string, string> = {
  believed_true: 'ok',
  unverified: 'mute',
  disputed: 'bad',
  superseded: 'mute',
  expired: 'mute',
  ended: 'mute',
  captured: 'ok',
  ignored: 'mute',
  extraction_failed: 'bad',
  pending: 'warn',
  pass: 'ok',
  flag: 'warn',
  fail: 'bad',
  skip: 'mute',
  info: 'acc',
}

export const statusColor = (s: TimeState | string | null | undefined): string =>
  s === 'believed_true'
    ? 'var(--g-believed)'
    : s === 'disputed'
      ? 'var(--g-disputed)'
      : s === 'unverified'
        ? 'var(--g-unverified)'
        : 'var(--g-ended)'

/** A Phoenix trace link, when tracing is on. */
export function traceUrl(phoenix: string | null | undefined, traceId: string | null | undefined) {
  if (!phoenix || !traceId) return null
  return `${phoenix.replace(/\/$/, '')}/redirects/traces/${traceId}`
}

export const pretty = (token: string) => token.replace(/_/g, ' ')
