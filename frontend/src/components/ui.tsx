import type { ReactNode } from 'react'
import { STATUS_LABEL, TONE } from '../lib/format'

export function Pill({ value, label, tone }: { value?: string | null; label?: ReactNode; tone?: string }) {
  const v = value ?? 'unverified'
  return <span className={`pill ${tone ?? TONE[v] ?? 'mute'}`}>{label ?? STATUS_LABEL[v] ?? v}</span>
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <div>{children}</div>}
    </div>
  )
}

export function Loading({ rows = 4 }: { rows?: number }) {
  return (
    <div style={{ display: 'grid', gap: 10, padding: 16 }} aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ width: `${90 - i * 12}%` }} />
      ))}
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  return <div className="err">{error instanceof Error ? error.message : 'Something went wrong'}</div>
}
