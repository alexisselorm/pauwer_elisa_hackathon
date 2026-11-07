import { ChevronLeft, ChevronRight, Pause, Play } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { GraphNode } from '../api/client'
import { statusColor } from '../lib/format'
import { ago, fmt, fmtDay, ms } from '../lib/time'

type Props = {
  assertions: GraphNode[]
  start: number
  now: number
  t: number
  live: boolean
  knownThen: boolean
  onTime: (t: number | null) => void // null = follow now
  onKnownThen: (v: boolean) => void
}

const STEPS = 1000

/** The as-of slider: drag to any instant; ticks mark when knowledge started or ended. */
export function Timeline({ assertions, start, now, t, live, knownThen, onTime, onKnownThen }: Props) {
  const span = Math.max(now - start, 1)
  const toV = (x: number) => Math.round((STEPS * (x - start)) / span)
  const toT = (v: number) => start + (span * v) / STEPS
  const pct = (x: number) => `${(100 * (x - start)) / span}%`

  // Every moment the graph changes: a start or an end of validity.
  const moments = useMemo(() => {
    const set = new Set<number>()
    for (const a of assertions) {
      const vf = ms(a.valid_from)
      const vt = ms(a.valid_to)
      if (vf !== null) set.add(vf)
      if (vt !== null && vt <= now) set.add(vt)
    }
    return [...set].sort((x, y) => x - y)
  }, [assertions, now])

  const years = useMemo(() => {
    const out: number[] = []
    for (let y = new Date(start).getUTCFullYear() + 1; y <= new Date(now).getUTCFullYear(); y++) {
      out.push(Date.UTC(y, 0, 1))
    }
    return out
  }, [start, now])

  const [playing, setPlaying] = useState(false)
  const tRef = useRef(t)
  useEffect(() => {
    tRef.current = t
  }, [t])
  useEffect(() => {
    if (!playing) return
    const id = window.setInterval(() => {
      const next = tRef.current + span / 220
      if (next >= now) {
        onTime(null)
        setPlaying(false)
      } else onTime(next)
    }, 40)
    return () => window.clearInterval(id)
  }, [playing, span, now, onTime])

  const step = (dir: -1 | 1) => {
    const target =
      dir < 0 ? [...moments].reverse().find((m) => m < t - 1000) : moments.find((m) => m > t + 1000)
    if (target === undefined) onTime(dir < 0 ? start : null)
    else onTime(target + (dir > 0 ? 1000 : 0))
  }

  return (
    <div className="card float timeline">
      <div className="tl-head">
        <h3>As of</h3>
        <span className="when">{fmt(t)} Helsinki</span>
        <span className="pill acc">{live ? 'now · live' : ago(t, now)}</span>
        <div className="tl-marks">
          <label className="toggle" title="Hide knowledge that was not on record yet at that time">
            <input type="checkbox" checked={knownThen} onChange={(e) => onKnownThen(e.target.checked)} />
            Only what was known then
          </label>
          <button onClick={() => step(-1)} aria-label="Previous change">
            <ChevronLeft size={14} />
          </button>
          <button onClick={() => step(1)} aria-label="Next change">
            <ChevronRight size={14} />
          </button>
          <button
            onClick={() => {
              if (!playing && live) onTime(start)
              setPlaying((p) => !p)
            }}
            className={playing ? 'on' : ''}
          >
            {playing ? <Pause size={12} /> : <Play size={12} />} {playing ? 'Pause' : 'Play'}
          </button>
          <button onClick={() => onTime(null)} className={live ? 'on' : ''}>
            Now
          </button>
        </div>
      </div>
      <div className="tl-track">
        <div className="tl-ticks" aria-hidden>
          {assertions.map((a) => {
            const vf = ms(a.valid_from)!
            return (
              <i
                key={a.id}
                style={{ left: pct(vf), background: statusColor(a.status) }}
                title={`${a.id} · ${fmtDay(vf)}`}
              />
            )
          })}
        </div>
        <input
          type="range"
          min={0}
          max={STEPS}
          value={live ? STEPS : toV(t)}
          aria-label="As of time"
          onChange={(e) => {
            const v = Number(e.target.value)
            onTime(v >= STEPS ? null : toT(v))
          }}
        />
      </div>
      <div className="tl-axis" aria-hidden>
        {years.map((y) => (
          <span key={y} style={{ left: pct(y) }}>
            {new Date(y).getUTCFullYear()}
          </span>
        ))}
      </div>
    </div>
  )
}
