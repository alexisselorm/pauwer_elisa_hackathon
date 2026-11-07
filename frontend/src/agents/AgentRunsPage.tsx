import { Link, useParams } from 'react-router'
import type { AgentRun } from '../api/client'
import { useAgentRuns } from '../api/hooks'
import { Empty, ErrorNote, Loading, Pill } from '../components/ui'
import { pretty } from '../lib/format'
import { ago, fmt } from '../lib/time'

const WRITES = new Set(['report_outcome', 'open_knowledge_gap', 'propose_assertion', 'flag_assertion'])

function outcome(r: AgentRun): { value: string; label: string } {
  if (r.writes.includes('open_knowledge_gap')) return { value: 'flag', label: 'gap opened' }
  if (r.writes.includes('report_outcome')) return { value: 'pass', label: 'outcome reported' }
  if (r.calls.some((c) => !c.ok)) return { value: 'fail', label: 'error' }
  return { value: 'info', label: 'read' }
}

const fmtArgs = (args: Record<string, unknown>) =>
  Object.entries(args)
    .filter(([, v]) => v !== null && v !== '')
    .map(([k, v]) => `${k}=${typeof v === 'string' ? JSON.stringify(v) : JSON.stringify(v)}`)
    .join(', ')

export function AgentRunsPage() {
  const { runId } = useParams()
  const q = useAgentRuns()
  const runs = q.data ?? []
  const run = runs.find((r) => r.run_id === runId) ?? runs[0]
  return (
    <div className="split">
      <div className="list">
        <div className="list-head">
          <h3>Agent runs</h3>
          <span className="subtle small">last 72 h</span>
        </div>
        {q.isPending && <Loading />}
        {q.isError && <ErrorNote error={q.error} />}
        {q.data?.length === 0 && (
          <Empty title="No agent calls yet">Ask the bot in Slack, or connect any MCP client.</Empty>
        )}
        {runs.map((r) => {
          const o = outcome(r)
          return (
            <Link key={r.run_id} to={`/agents/${r.run_id}`} className={`li ${run?.run_id === r.run_id ? 'active' : ''}`}>
              <div className="l1">
                <span>{r.title}</span>
                <Pill value={o.value} label={o.label} />
              </div>
              <div className="l2">
                {r.agent_id} · {ago(r.started_at)} · {r.calls.length} MCP call{r.calls.length > 1 ? 's' : ''}
              </div>
            </Link>
          )
        })}
      </div>
      <div className="detail">{run && <RunDetail run={run} />}</div>
    </div>
  )
}

function RunDetail({ run }: { run: AgentRun }) {
  const total = run.calls.reduce((s, c) => s + c.ms, 0)
  const cited = [...new Set(run.calls.flatMap((c) => c.cited))]
  const o = outcome(run)
  return (
    <>
      <div className="detail-head">
        <h2>{run.title}</h2>
        <Pill value={o.value} label={o.label} />
        <span className="subtle">
          {run.agent_id} · {fmt(run.started_at)} · {(total / 1000).toFixed(1)} s in tools
        </span>
      </div>
      <div className="grid2">
        <div className="card sect">
          <h3>MCP calls</h3>
          <div className="calls">
            {run.calls.map((c) => (
              <div key={c.call_id} className={`call ${WRITES.has(c.tool) ? 'write' : ''} ${c.ok ? '' : 'fail'}`}>
                <div className="fn">
                  <code>{c.tool}</code>
                  <span className="mono subtle">({fmtArgs(c.args)})</span>
                  <span className="ms">{c.ms} ms</span>
                </div>
                <div className="res" style={c.ok ? undefined : { color: 'var(--bad)' }}>{c.summary}</div>
              </div>
            ))}
          </div>
        </div>
        <div className="stack">
          <div className="card sect">
            <h3>Knowledge it read</h3>
            {cited.length ? (
              <div className="chips">
                {cited.map((id) => (
                  <Link key={id} className="chip" to={`/?select=${id}`}>{id}</Link>
                ))}
              </div>
            ) : (
              <div className="subtle small">No assertion returned</div>
            )}
          </div>
          {run.writes.length > 0 && (
            <div className="card sect">
              <h3>What it wrote</h3>
              <div className="chips">
                {run.writes.map((w, i) => (
                  <span key={i} className="chip">{pretty(w)}</span>
                ))}
              </div>
              <p className="subtle small" style={{ marginBottom: 0 }}>Governed writes: no confirm, retract or validity edits.</p>
            </div>
          )}
        </div>
      </div>
    </>
  )
}
