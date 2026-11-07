import type { Schemas } from '../api/client'
import { useEval } from '../api/hooks'
import { Empty, ErrorNote, Loading, Pill } from '../components/ui'
import { pretty } from '../lib/format'
import { fmt } from '../lib/time'

type EvalSet = Schemas['EvalSet']
type EvalItem = Schemas['EvalItem']

// What each T0 test checks (plan 003 §1), so the scorecard reads without the plan open.
const T0_LABEL: Record<string, string> = {
  'ST-01': "Elisa's 12 chat messages → exactly one rule",
  'ST-02': 'Her email corroborates it, no new rule',
  'ST-10': 'Linked to TRN-042, TNC-1X / Gen1, SFP swap, bounce, avoid list',
  'ST-20': 'recorded_at = valid_from = message 8, to the second',
  'ST-21': 'Backfilled chat times strictly increasing; message 8 exact',
  'ST-30': 'Agent recalls it for TRN-017 with the cited rule',
}

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
                  <ItemRow key={i.id} item={i} />
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

function ItemRow({ item: i }: { item: EvalItem }) {
  const failed = i.checks.filter((c) => !c.passed)
  const label = T0_LABEL[i.id]
  const summary = failed.length
    ? failed.map((c) => `${pretty(c.name)}: ${c.detail}`).join('; ')
    : i.note || label
  return (
    <tr>
      <td className="mono" style={{ width: 70, verticalAlign: 'top' }}>{i.id}</td>
      <td className="small">
        {i.checks.length > 0 ? (
          <details className="eval-item">
            <summary>
              {label && failed.length > 0 && <b>{label}. </b>}
              {summary || <span className="subtle">all checks passed</span>}
            </summary>
            {i.note && i.note !== summary && <p className="subtle">{i.note}</p>}
            <ul>
              {i.checks.map((c, k) => (
                <li key={k} className={c.passed ? 'ok' : 'bad'}>
                  <span aria-hidden>{c.passed ? '✓' : '✗'}</span> {pretty(c.name)}
                  {c.detail && <span className="mono subtle"> {c.detail}</span>}
                </li>
              ))}
            </ul>
          </details>
        ) : (
          summary || <span className="subtle">all checks passed</span>
        )}
      </td>
      <td style={{ textAlign: 'right', verticalAlign: 'top' }}>
        <Pill value={i.skipped ? 'skip' : i.passed ? 'pass' : 'fail'} label={i.skipped ? 'skipped' : i.passed ? 'pass' : 'fail'} />
      </td>
    </tr>
  )
}
