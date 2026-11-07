import type { Schemas } from '../api/client'
import { useEval } from '../api/hooks'
import { Empty, ErrorNote, Loading, Pill } from '../components/ui'
import { pretty } from '../lib/format'
import { fmt } from '../lib/time'

type EvalSet = Schemas['EvalSet']

const scored = (s: EvalSet) => s.items.filter((i) => !i.skipped)
const passed = (s: EvalSet) => scored(s).filter((i) => i.passed).length

function ratio(v: string): [number, number] {
  const [p, n] = v.split('/').map(Number)
  return [p || 0, n || 0]
}

export function ScorecardPage() {
  const q = useEval()
  if (q.isPending) return <Loading rows={6} />
  if (q.isError) return <ErrorNote error={q.error} />
  const r = q.data
  if (!r)
    return (
      <div className="page">
        <Empty title="No eval run yet">
          <span className="mono">uv run kaiku eval</span> writes <span className="mono">eval/results/*.json</span>, shown here.
        </Empty>
      </div>
    )
  return (
    <div className="page">
      <div className="detail-head" style={{ marginBottom: 0 }}>
        <h2>Scorecard</h2>
        <span className="subtle">{fmt(r.started_at)} · {r.graph}</span>
        <div className="right">
          <span className="mono subtle">{r.file}</span>
        </div>
      </div>
      <div className="tiles">
        {r.sets.map((s) => {
          const n = scored(s).length
          const p = passed(s)
          return (
            <div key={s.name} className={`card tile ${n && p === n ? 'good' : ''}`}>
              <div className="v">
                {n ? p : '—'}
                <small> / {n}</small>
              </div>
              <div className="k">{s.name}</div>
              <div className="n">{s.items.length - n ? `${s.items.length - n} skipped` : 'items passed'}</div>
            </div>
          )
        })}
      </div>
      <div className="grid2">
        {r.sets.map((s) => (
          <div key={s.name} className="card sect">
            <h3>{s.name}</h3>
            {Object.keys(s.field_accuracy).length > 0 && (
              <table style={{ marginBottom: 16 }}>
                <thead>
                  <tr><th>Check</th><th>Passed</th><th /></tr>
                </thead>
                <tbody>
                  {Object.entries(s.field_accuracy).map(([k, v]) => {
                    const [p, n] = ratio(v)
                    return (
                      <tr key={k}>
                        <td>{pretty(k)}</td>
                        <td className="mono">{v}</td>
                        <td style={{ width: '40%' }}><div className="bar"><i style={{ width: `${n ? (100 * p) / n : 0}%` }} /></div></td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
            <table>
              <tbody>
                {s.items.map((i) => (
                  <tr key={i.id}>
                    <td className="mono" style={{ width: 90 }}>{i.id}</td>
                    <td className="small">
                      {i.note || i.checks.filter((c) => !c.passed).map((c) => `${pretty(c.name)}: ${c.detail}`).join('; ') || <span className="subtle">all checks passed</span>}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <Pill value={i.skipped ? 'skip' : i.passed ? 'pass' : 'fail'} label={i.skipped ? 'skipped' : i.passed ? 'pass' : 'fail'} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
      {r.caveat && <p className="subtle small" style={{ marginTop: 20 }}>{r.caveat}</p>}
      {Object.keys(r.models).length > 0 && (
        <p className="subtle small mono">{Object.entries(r.models).map(([k, v]) => `${k}=${v ?? '—'}`).join(' · ')}</p>
      )}
    </div>
  )
}
