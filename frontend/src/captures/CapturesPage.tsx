import { ExternalLink } from 'lucide-react'
import { useMemo } from 'react'
import { Link, useParams } from 'react-router'
import type { CaptureDetail, CaptureSummary, ItemView } from '../api/client'
import { useCapture, useCaptures, useMeta } from '../api/hooks'
import { Empty, ErrorNote, Loading, Pill } from '../components/ui'
import { pretty, traceUrl } from '../lib/format'
import { useLive } from '../live-context'
import { ago, fmt, fmtClock } from '../lib/time'
import { CATEGORIES, type Category, highlight } from './highlight'

const NODES = ['triage', 'extract', 'link', 'validate', 'reconcile', 'persist'] as const
const AVATAR = ['#101064', '#4060ff', '#f27513', '#008253', '#ae520d', '#212185', '#60686e']

function initials(name: string) {
  const parts = name.replace(/^@/, '').split(/\s+/).filter(Boolean)
  return ((parts[0]?.[0] ?? '?') + (parts[1]?.[0] ?? '')).toUpperCase()
}
function colorFor(name: string) {
  let h = 0
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return AVATAR[h % AVATAR.length]
}

export function CapturesPage() {
  const { threadId } = useParams()
  const list = useCaptures()
  const { fresh } = useLive()
  const selected = threadId ?? list.data?.[0]?.thread_id
  return (
    <div className="split">
      <div className="list">
        <div className="list-head">
          <h3>Threads</h3>
          <span className="subtle small">{list.data?.length ?? ''}</span>
        </div>
        {list.isPending && <Loading />}
        {list.isError && <ErrorNote error={list.error} />}
        {list.data?.length === 0 && <Empty title="No threads yet">Talk in a captured Slack channel.</Empty>}
        {list.data?.map((c) => (
          <ThreadRow key={c.thread_id} c={c} active={c.thread_id === selected} fresh={fresh?.captures.includes(c.thread_id) ?? false} />
        ))}
      </div>
      <div className="detail">{selected ? <Detail id={selected} /> : !list.isPending && <Empty title="Pick a thread" />}</div>
    </div>
  )
}

function ThreadRow({ c, active, fresh }: { c: CaptureSummary; active: boolean; fresh: boolean }) {
  const n = c.assertion_ids?.length ?? 0
  return (
    <Link to={`/captures/${encodeURIComponent(c.thread_id)}`} className={`li ${active ? 'active' : ''} ${fresh ? 'fresh' : ''}`}>
      <div className="l1">
        <span>{c.title}</span>
        <Pill value={c.status ?? 'pending'} />
      </div>
      <div className="l2">
        {c.channel ?? c.platform} · {c.last_message_ts ? ago(c.last_message_ts) : ''} · {n ? `${n} assertion${n > 1 ? 's' : ''}` : (c.triage_reason ?? '')}
      </div>
    </Link>
  )
}

function Detail({ id }: { id: string }) {
  const q = useCapture(id)
  const meta = useMeta()
  if (q.isPending) return <Loading rows={8} />
  if (q.isError) return <ErrorNote error={q.error} />
  const d = q.data
  const url = traceUrl(meta.data?.phoenix_url, d.trace_id)
  return (
    <>
      <div className="detail-head">
        <h2>{d.summary.title}</h2>
        <Pill value={d.summary.status ?? 'pending'} />
        <span className="subtle">
          {d.summary.channel} · {fmt(d.summary.started_at)}
        </span>
        <div className="right">
          {url && (
            <a className="btn" href={url} target="_blank" rel="noreferrer">
              Trace <ExternalLink size={13} />
            </a>
          )}
        </div>
      </div>
      <Pipeline d={d} />
      <div className="grid2">
        <Transcript d={d} />
        <div className="stack">
          <div className="card sect">
            <h3>Decision</h3>
            <p style={{ margin: 0 }}>
              <Pill value="info" label={pretty(d.summary.triage_kind ?? 'none')} /> {d.summary.triage_reason}
            </p>
            {!d.summary.has_trace && <p className="subtle small" style={{ marginBottom: 0 }}>Captured before step tracing was on: no gate details.</p>}
          </div>
          {d.items.map((item, i) => (
            <Rule key={i} item={item} />
          ))}
        </div>
      </div>
    </>
  )
}

function Pipeline({ d }: { d: CaptureDetail }) {
  const byNode = new Map(d.steps.map((s) => [s.node, s]))
  const items = d.items
  const out: Record<string, string> = {
    triage: pretty(d.summary.triage_kind ?? ''),
    extract: items.length ? `${items.length} rule${items.length > 1 ? 's' : ''}` : '',
    link: `${items.filter((i) => i.gates.find((g) => g.code === 'V3')?.result === 'pass').length} linked`,
    validate: items.some((i) => i.gates.find((g) => g.code === 'V4')?.result === 'pass') ? 'telemetry agrees' : 'checked',
    reconcile: items.map((i) => i.gates.find((g) => g.code === 'V6')?.detail).filter(Boolean)[0] ?? '',
    persist: `${items.filter((i) => i.assertion_id).length} stored`,
  }
  if (!d.steps.length) return null
  return (
    <div className="pipe">
      {NODES.map((n, i) => {
        const s = byNode.get(n)
        return (
          <div key={n} className={`ps ${s ? 'done' : ''}`}>
            <div className="node">{s ? '✓' : i + 1}</div>
            <div className="nm">{n}</div>
            <div className="small">{s ? out[n] : 'skipped'}</div>
            <div className="meta">{s ? `${(s.ms / 1000).toFixed(1)} s${s.runs > 1 ? ` · ×${s.runs}` : ''}` : ''}</div>
          </div>
        )
      })}
    </div>
  )
}

function Transcript({ d }: { d: CaptureDetail }) {
  const quotes = useMemo(() => {
    const out: Partial<Record<Category, string[]>> = {}
    for (const item of d.items) {
      for (const cat of CATEGORIES) out[cat] = [...(out[cat] ?? []), ...(item.quotes[cat] ?? [])]
    }
    return out
  }, [d.items])
  const used = CATEGORIES.filter((c) => quotes[c]?.length)
  return (
    <div className="card sect">
      <h3>Source</h3>
      {used.length > 0 && (
        <div className="legend-row">
          {used.map((c) => (
            <mark key={c} className={c}>{c}</mark>
          ))}
        </div>
      )}
      {d.messages.map((m) => {
        const who = m.author ?? 'unknown'
        return (
          <div className="msg" key={m.event_id}>
            <div className="avatar" style={{ background: colorFor(who) }}>{initials(who)}</div>
            <div>
              <div className="who">
                {who}
                <span>
                  {fmtClock(m.event_time)}
                  {m.permalink && (
                    <>
                      {' · '}
                      <a href={m.permalink} target="_blank" rel="noreferrer">open</a>
                    </>
                  )}
                </span>
              </div>
              <div className="txt">{highlight(m.text, quotes)}</div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function Rule({ item }: { item: ItemView }) {
  return (
    <div className="card sect">
      <h3>
        {item.assertion_id ? (
          <Link to={`/?select=${encodeURIComponent(item.assertion_id)}`} className="mono">{item.assertion_id}</Link>
        ) : (
          <Pill value="fail" label="dropped" />
        )}
        <span className="subtle small" style={{ fontWeight: 400 }}>{pretty(item.topic)}</span>
      </h3>
      <p style={{ marginTop: 0 }}>{item.statement}</p>
      {item.dropped && <p className="small" style={{ color: 'var(--bad)' }}>{item.dropped}</p>}
      <table>
        <tbody>
          {item.gates.map((g) => (
            <tr key={g.code}>
              <td className="mono" style={{ width: 56 }}>{g.code}</td>
              <td>
                <b>{g.name}</b>
                {g.detail && <div className="subtle small">{g.detail}</div>}
              </td>
              <td style={{ textAlign: 'right' }}>
                <Pill value={g.result} label={g.result} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
