import { Zap } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { useGraph } from '../api/hooks'
import { ErrorNote, Loading } from '../components/ui'
import { useLive } from '../live-context'
import { ms, stateAt, type TimeState } from '../lib/time'
import { Drawer } from './Drawer'
import { GraphCanvas, type Layers } from './GraphCanvas'
import { Timeline } from './Timeline'

const DAY = 86_400_000

function useNow(everyMs = 30_000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs)
    return () => window.clearInterval(id)
  }, [everyMs])
  return now
}

/** Apply the latest of a burst of values once per animation frame: the slider and Play
 *  report a time far more often than the canvas can restyle. */
function useFrameCoalesced<T>(apply: (v: T) => void) {
  const latest = useRef<{ v: T } | null>(null)
  const frame = useRef(0)
  useEffect(() => () => cancelAnimationFrame(frame.current), [])
  return useCallback(
    (v: T) => {
      latest.current = { v }
      if (frame.current) return
      frame.current = requestAnimationFrame(() => {
        frame.current = 0
        const next = latest.current
        latest.current = null
        if (next) apply(next.v)
      })
    },
    [apply],
  )
}

export function GraphPage({ dark }: { dark: boolean }) {
  const graph = useGraph()
  const { fresh, dismiss } = useLive()
  const now = useNow()
  const [asOf, setAsOf] = useState<number | null>(null) // null = now, following live
  const [knownThen, setKnownThen] = useState(false)
  const [layers, setLayers] = useState<Layers>({ network: true, knowledge: true, source: true })
  const [params] = useSearchParams()
  const [selected, setSelected] = useState<string | null>(params.get('select'))

  const t = asOf ?? now
  const k = knownThen ? t : now
  const data = graph.data
  const assertions = useMemo(() => (data?.nodes ?? []).filter((n) => n.kind === 'assertion'), [data])
  const start = useMemo(() => {
    const first = Math.min(...assertions.map((a) => ms(a.valid_from)!), now)
    return first - 60 * DAY
  }, [assertions, now])

  const counts = useMemo(() => {
    const c: Record<TimeState, number> = { believed_true: 0, unverified: 0, disputed: 0, ended: 0, future: 0, unknown: 0 }
    for (const a of assertions) c[stateAt(a, t, k)]++
    return c
  }, [assertions, t, k])
  const inForce = counts.believed_true + counts.unverified + counts.disputed
  const captured = assertions.filter((a) => a.origin !== 'gold').length
  const sources = (data?.nodes ?? []).filter((n) => n.kind === 'source').length
  const today = assertions.filter((a) => a.origin !== 'gold' && now - (ms(a.recorded_at) ?? 0) < DAY).length
  const layerCount = (l: keyof Layers) =>
    (data?.nodes ?? []).filter((n) =>
      l === 'network' ? !['assertion', 'source', 'topic'].includes(n.kind) : l === 'source' ? n.kind === 'source' : n.kind === 'assertion' || n.kind === 'topic',
    ).length

  // A live capture: jump to now so it is visible, and offer to open it.
  const freshIds = useMemo(() => fresh?.assertions ?? [], [fresh])
  const [seenFresh, setSeenFresh] = useState(fresh)
  if (fresh !== seenFresh) {
    setSeenFresh(fresh)
    if (fresh?.assertions.length) setAsOf(null)
  }
  const freshNode = assertions.find((a) => freshIds.includes(a.id))

  const onTime = useFrameCoalesced<number | null>(setAsOf)
  const drawerOpen = selected !== null

  if (graph.isPending) return <Loading rows={6} />
  if (graph.isError) return <ErrorNote error={graph.error} />

  return (
    <div className={`graph-view ${drawerOpen ? 'drawer-open' : ''}`}>
      <GraphCanvas
        data={data}
        t={t}
        k={k}
        layers={layers}
        selected={selected}
        fresh={freshIds}
        dark={dark}
        drawerOpen={drawerOpen}
        onSelect={setSelected}
      />

      <div className="card float panel-left">
        <p className="eyebrow">Layers</p>
        {(['network', 'knowledge', 'source'] as const).map((l) => (
          <div className="row" key={l}>
            <label>
              <input type="checkbox" checked={layers[l]} onChange={(e) => setLayers({ ...layers, [l]: e.target.checked })} />
              {l === 'network' ? 'Network' : l === 'knowledge' ? 'Knowledge' : 'Sources'}
            </label>
            <span className="count">{layerCount(l)}</span>
          </div>
        ))}
        <div className="sep" />
        <p className="eyebrow">Status at this time</p>
        <Legend color="var(--g-believed)" label="Believed true" n={counts.believed_true} />
        <Legend color="var(--g-unverified)" label="Unverified" n={counts.unverified} />
        <Legend color="var(--g-disputed)" label="Disputed" n={counts.disputed} />
        <Legend ring label="Ended" n={counts.ended} />
        <div className="row">
          <span className="lbl"><span className="dot" style={{ background: 'var(--surface)', border: '3px solid var(--accent)' }} /> Captured live</span>
          <span className="count">{captured}</span>
        </div>
        <div className="sep" />
        <p className="eyebrow">Links</p>
        <Legend sq color="var(--g-supersedes)" label="Supersedes" />
        <Legend sq color="var(--g-believed)" label="Corroborates" />
        <Legend sq color="var(--g-disputed)" label="Contradicts" />
      </div>

      <div className="card float kpis">
        <div className="kpi"><b>{inForce}</b><span>in force</span></div>
        <div className="kpi"><b>{captured}</b><span>captured</span></div>
        <div className="kpi"><b>{today}</b><span>last 24 h</span></div>
        <div className="kpi"><b>{sources}</b><span>sources</span></div>
      </div>

      <Timeline
        assertions={assertions}
        start={start}
        now={now}
        t={t}
        live={asOf === null}
        knownThen={knownThen}
        onTime={onTime}
        onKnownThen={setKnownThen}
      />

      <Drawer
        id={selected}
        data={data}
        t={t}
        k={k}
        now={now}
        onSelect={setSelected}
      />

      {freshNode && (
        <div className="card float toast" role="status">
          <Zap size={20} color="var(--accent)" />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="t1">New knowledge captured</div>
            <div className="t2">{freshNode.label}</div>
          </div>
          <button
            className="btn"
            onClick={() => {
              setSelected(freshNode.id)
              dismiss()
            }}
          >
            Open
          </button>
        </div>
      )}
    </div>
  )
}

function Legend({ color, label, n, ring, sq }: { color?: string; label: string; n?: number; ring?: boolean; sq?: boolean }) {
  const swatch = ring ? (
    <span className="dot" style={{ background: 'var(--surface)', border: '2px dashed var(--n-09)' }} />
  ) : (
    <span className={sq ? 'sq' : 'dot'} style={{ background: color }} />
  )
  return (
    <div className="row">
      <span className="lbl">{swatch} {label}</span>
      {n !== undefined && <span className="count">{n}</span>}
    </div>
  )
}
