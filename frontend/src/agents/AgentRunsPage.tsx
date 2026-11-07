import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import type { MouseEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { api, unwrap, type AgentRun } from '../api/client'
import { keys, useAgentRuns } from '../api/hooks'
import { Empty, ErrorNote, Loading, Pill } from '../components/ui'
import { pretty } from '../lib/format'
import { ago, fmt } from '../lib/time'

function outcome(r: AgentRun): { value: string; label: string } {
  if (r.writes.includes('open_knowledge_gap')) return { value: 'flag', label: 'gap opened' }
  if (r.writes.includes('report_outcome')) return { value: 'pass', label: 'outcome reported' }
  if (r.calls.some((c) => !c.ok)) return { value: 'fail', label: 'error' }
  return { value: 'info', label: 'read' }
}

const fmtArgs = (args: Record<string, unknown>) =>
  Object.entries(args)
    .filter(([, v]) => v !== null && v !== '')
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(', ')

export function AgentRunsPage() {
  const { runId } = useParams()
  const q = useAgentRuns()
  const runs = q.data ?? []
  const run = runs.find((r) => r.run_id === runId) ?? runs[0]
  const navigate = useNavigate()
  const qc = useQueryClient()
  const done = () => qc.invalidateQueries({ queryKey: keys.agentRuns })
  // Removes the MCP calls from the log only: gaps and outcomes the agent recorded stay.
  const removeOne = useMutation({
    mutationFn: (id: string) =>
      unwrap(api.DELETE('/api/ui/agent-runs/{run_id}', { params: { path: { run_id: id } } })),
    onSuccess: (_, id) => {
      if (id === run?.run_id) void navigate('/agents')
    },
    onSettled: done,
  })
  const removeAll = useMutation({
    mutationFn: () => unwrap(api.DELETE('/api/ui/agent-runs')),
    onSuccess: () => void navigate('/agents'),
    onSettled: done,
  })
  const trashOne = (e: MouseEvent, id: string) => {
    e.preventDefault() // the button sits inside the run's link
    e.stopPropagation()
    removeOne.mutate(id)
  }
  const trashAll = () => {
    const ok = window.confirm(
      `Delete all ${runs.length} agent runs from the log?\n\nGaps and outcomes the agents recorded stay.`,
    )
    if (ok) removeAll.mutate()
  }
  return (
    <div className="split">
      <div className="list">
        <div className="list-head">
          <h3>Agent runs</h3>
          <span className="list-head-end">
            <span className="subtle small">last 72 h</span>
            {runs.length > 0 && (
              <button
                className="icon-btn"
                onClick={trashAll}
                disabled={removeAll.isPending}
                title="Delete all agent runs"
                aria-label="Delete all agent runs"
              >
                <Trash2 size={15} />
              </button>
            )}
          </span>
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
                <span>
                  {r.agent_id} · {ago(r.started_at)} · {r.calls.length} MCP call{r.calls.length > 1 ? 's' : ''}
                </span>
                <button
                  className="li-trash"
                  onClick={(e) => trashOne(e, r.run_id)}
                  disabled={removeOne.isPending && removeOne.variables === r.run_id}
                  title="Delete this run"
                  aria-label={`Delete run: ${r.title}`}
                >
                  <Trash2 size={14} />
                </button>
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
              <div key={c.call_id} className={`call ${run.writes.includes(c.tool) ? 'write' : ''} ${c.ok ? '' : 'fail'}`}>
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
