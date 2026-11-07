import { ExternalLink, X } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router'
import type { AssertionView, GraphNode, GraphOut } from '../api/client'
import { useAssertion, useEntity } from '../api/hooks'
import { ErrorNote, Loading, Pill } from '../components/ui'
import { pretty, STATUS_LABEL, statusColor } from '../lib/format'
import { fmt, isVisible, ms, stateAt } from '../lib/time'

type Props = { id: string | null; data: GraphOut | undefined; t: number; k: number; now: number; onSelect: (id: string | null) => void }

export function Drawer({ id, data, t, k, now, onSelect }: Props) {
  const node = data?.nodes.find((n) => n.id === id) ?? null
  return (
    <aside className="card float drawer" aria-label="Details" aria-hidden={!id}>
      {node?.kind === 'assertion' && <AssertionPanel node={node} t={t} k={k} now={now} data={data!} onSelect={onSelect} />}
      {node && node.kind === 'source' && <SourcePanel node={node} data={data!} onSelect={onSelect} />}
      {node && node.kind !== 'assertion' && node.kind !== 'source' && (
        <EntityPanel node={node} t={t} k={k} data={data!} onSelect={onSelect} />
      )}
    </aside>
  )
}

function Head({ children, title, onClose }: { children: ReactNode; title: string; onClose: () => void }) {
  return (
    <div className="drawer-head">
      <div className="top">
        {children}
        <button className="icon-btn" style={{ marginLeft: 'auto' }} onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
      </div>
      <h2>{title}</h2>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <p className="eyebrow">{title}</p>
      {children}
    </section>
  )
}

const asList = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : v ? [String(v)] : [])

function ValidityBar({ from, to, t, start, now, color }: { from: string; to?: string | null; t: number; start: number; now: number; color: string }) {
  const span = Math.max(now - start, 1)
  const pos = (x: number) => Math.min(100, Math.max(0, (100 * (x - start)) / span))
  const l = pos(ms(from)!)
  const r = pos(ms(to) ?? now)
  return (
    <>
      <div className="vbar">
        <div className="fill" style={{ left: `${l}%`, width: `${Math.max(r - l, 0.8)}%`, background: color }} />
        <div className="now" style={{ left: `${pos(t)}%` }} title="As of" />
      </div>
      <div className="vlabels">
        <span><span className="subtle">From</span> {fmt(from)}</span>
        <span><span className="subtle">To</span> {to ? fmt(to) : 'still believed true'}</span>
      </div>
    </>
  )
}

function Ref({ a, onSelect }: { a: AssertionView; onSelect: (id: string) => void }) {
  return (
    <button className="linkish" onClick={() => onSelect(a.assertion_id)} title={a.statement}>
      {a.statement.length > 70 ? `${a.statement.slice(0, 69)}…` : a.statement}
    </button>
  )
}

function AssertionPanel({ node, t, k, now, data, onSelect }: { node: GraphNode; t: number; k: number; now: number; data: GraphOut; onSelect: (id: string | null) => void }) {
  const q = useAssertion(node.id)
  const st = stateAt(node, t, k)
  const shown = st === 'ended' ? (node.status ?? 'superseded') : st
  const assertionTimes = data.nodes.filter((n) => n.kind === 'assertion').map((n) => ms(n.valid_from)!)
  const start = Math.min(...assertionTimes) - 60 * 86_400_000
  const d = q.data
  const a = d?.assertion
  const qual = (a?.qualifiers ?? {}) as Record<string, unknown>
  const symptoms = asList(qual.symptom)
  const avoid = asList(qual.avoid)
  const action = qual.action ? String(qual.action) : null
  const telemetry = qual.telemetry ? String(qual.telemetry) : null
  const platform = d?.sources[0]?.platform
  const origin =
    node.origin === 'gold' ? 'Seeded history' : node.origin === 'agent_proposal' ? 'Proposed by an agent' : `Captured from ${platform === 'email' ? 'email' : 'Slack'}`
  const support = a?.support_n ?? 0
  const refute = a?.refute_n ?? 0
  const conf = (1 + support) / (2 + support + refute)
  const scopeEntity = a?.bindings.find((b) => b.scope !== 'activity')?.entity_id

  return (
    <>
      <Head title={pretty(node.topic ?? node.id)} onClose={() => onSelect(null)}>
        <Pill value={shown} label={STATUS_LABEL[shown]} />
        {st === 'ended' && <Pill value="ended" label="Ended at this time" />}
        {!isVisible(st) && <Pill value="ended" label="Not yet at this time" />}
        <span className="mono subtle">{node.id}</span>
      </Head>
      {q.isPending && <Loading />}
      {q.isError && <ErrorNote error={q.error} />}
      {a && d && (
        <div className="drawer-body">
          <section>
            <p style={{ margin: '0 0 10px' }}>{a.statement}</p>
            <span className="pill acc">{origin}</span>
          </section>

          <Section title="Bound to">
            <div className="chips">
              {a.bindings.map((b) => (
                <button key={`${b.scope}${b.entity_id}`} className={`chip ${b.candidate ? 'avoid' : ''}`} onClick={() => onSelect(b.entity_id)} title={b.candidate ? 'Unconfirmed: asked on the Slack card (V3)' : undefined}>
                  <small>{pretty(b.scope)}</small>
                  {b.entity_id.replace('TOPIC:', '').replace(/_/g, ' ')}
                  {b.candidate && ' ?'}
                </button>
              ))}
            </div>
          </Section>

          <Section title="Validity">
            <ValidityBar from={a.valid_from} to={a.valid_to} t={t} start={start} now={now} color={statusColor(st === 'ended' ? 'ended' : shown)} />
            <div className="subtle small" style={{ marginTop: 6 }}>On record since {fmt(a.recorded_at)}</div>
          </Section>

          {(symptoms.length > 0 || action || avoid.length > 0) && (
            <Section title="When · then">
              {symptoms.map((s) => (
                <div className="cond" key={s}>
                  <span style={{ color: telemetry === 'agrees' ? 'var(--ok)' : 'var(--text-subtle)' }}>{telemetry === 'agrees' ? '✓' : '·'}</span>
                  <span>{pretty(s)}</span>
                  <span />
                </div>
              ))}
              {telemetry && telemetry !== 'unknown' && (
                <div className="small" style={{ margin: '6px 0 10px' }}>
                  <Pill value={telemetry === 'agrees' ? 'pass' : 'flag'} label={`V4 · telemetry ${telemetry}`} />{' '}
                  <span className="subtle">{String(qual.telemetry_detail ?? '')}</span>
                </div>
              )}
              <div className="steps" style={{ marginTop: 8 }}>
                {action && <div className="step do"><b>Do</b><span>{pretty(action)}</span></div>}
                {avoid.map((x) => (
                  <div className="step avoid" key={x}><b>Avoid</b><span>{pretty(x)}</span></div>
                ))}
              </div>
            </Section>
          )}

          <Section title="Evidence">
            {d.sources.map((s) => (
              <div key={s.event_id} className={`quote ${s.platform === 'email' ? 'email' : ''}`}>
                <q>{s.quote ?? s.text}</q>
                <div className="src">
                  {s.author && <b>{s.author}</b>}
                  {s.channel && <span>{s.channel}</span>}
                  <span>{fmt(s.event_time)}</span>
                  {s.permalink && (
                    <a href={s.permalink} target="_blank" rel="noreferrer">
                      Open <ExternalLink size={11} />
                    </a>
                  )}
                </div>
              </div>
            ))}
            {d.sources.length === 0 && (
              <div className="quote record">
                <q>{d.source_detail ?? 'Seeded record'}</q>
                <div className="src">{d.asserted_by && <b>{d.asserted_by}</b>} {a.citations[0]?.source_type && pretty(a.citations[0].source_type)}</div>
              </div>
            )}
          </Section>

          <Section title="Confidence">
            <div className="conf">
              <div className="meter"><i style={{ width: `${conf * 100}%` }} /></div>
              <b>{conf.toFixed(2)}</b>
            </div>
            <div className="subtle small" style={{ marginTop: 4 }}>
              {support} network-confirmed · {refute} refuted{a.review_due ? ' · review due' : ''}
            </div>
            {d.evidence.map((e) => (
              <div className="rel" key={e.event_id}>
                <Pill value={e.relation === 'SUPPORTS' ? 'pass' : 'fail'} label={e.relation.toLowerCase()} />
                <span>{e.result ?? ''} {e.entity_id ?? ''}</span>
                <span className="subtle small" style={{ marginLeft: 'auto' }}>{fmt(e.event_time)}</span>
              </div>
            ))}
          </Section>

          <Section title="History">
            {d.versions.length > 1 &&
              d.versions.map((v) => (
                <div className="rel" key={v.assertion_id}>
                  <span className="kind">{v.assertion_id === a.assertion_id ? 'This version' : (ms(v.valid_from)! < ms(a.valid_from)! ? 'Earlier' : 'Later')}</span>
                  <span className="dot" style={{ background: statusColor(v.status) }} />
                  {v.assertion_id === a.assertion_id ? <span>{fmt(v.valid_from)}</span> : <Ref a={v} onSelect={onSelect} />}
                </div>
              ))}
            {d.corroborates.map((v) => (
              <div className="rel" key={`c${v.assertion_id}`}>
                <span className="kind">Corroborates</span>
                <span className="dot" style={{ background: 'var(--g-believed)' }} />
                <Ref a={v} onSelect={onSelect} />
              </div>
            ))}
            {d.contradicts.map((v) => (
              <div className="rel" key={`x${v.assertion_id}`}>
                <span className="kind">Contradicts</span>
                <span className="dot" style={{ background: 'var(--g-disputed)' }} />
                <Ref a={v} onSelect={onSelect} />
              </div>
            ))}
            {d.versions.length <= 1 && d.corroborates.length === 0 && d.contradicts.length === 0 && <div className="subtle small">No related knowledge</div>}
          </Section>

          {scopeEntity && (
            <Section title="What an agent calls (MCP)">
              <div className="codebox mono">get_context("{scopeEntity}", as_of="{new Date(t).toISOString().slice(0, 19)}Z")</div>
            </Section>
          )}

          {d.thread_id && (
            <div className="foot">
              <Link className="btn" to={`/captures/${encodeURIComponent(d.thread_id)}`}>How it was captured</Link>
            </div>
          )}
        </div>
      )}
    </>
  )
}

function EntityPanel({ node, t, k, data, onSelect }: { node: GraphNode; t: number; k: number; data: GraphOut; onSelect: (id: string | null) => void }) {
  const q = useEntity(node.id, new Date(t).toISOString())
  const e = q.data
  // Knowledge in scope: bound to the entity, its model or its generation (like get_context).
  const scope = new Set([node.id, e?.model, e?.generation].filter(Boolean) as string[])
  const linked = new Set(data.edges.filter((x) => x.rel === 'ABOUT' && scope.has(x.target)).map((x) => x.source))
  const assertions = data.nodes.filter((n) => linked.has(n.id))
  const active = assertions.filter((a) => isVisible(stateAt(a, t, k)) && stateAt(a, t, k) !== 'ended')
  const other = assertions.filter((a) => !active.includes(a))
  const kindLabel: Record<string, string> = { device: 'Device', hw_model: 'Hardware model', generation: 'Generation', segment: 'Segment', site: 'Site', topic: 'Activity' }
  return (
    <>
      <Head title={node.label} onClose={() => onSelect(null)}>
        <Pill value="mute" tone="mute" label={kindLabel[node.kind] ?? node.kind} />
        {e && !e.exists_at && <Pill value="fail" label="Not in service at this time" />}
      </Head>
      <div className="drawer-body">
        {q.isPending && node.kind !== 'topic' && <Loading rows={3} />}
        {e && (
          <dl className="kv">
            {e.name && e.name !== e.entity_id && (<><dt>Name</dt><dd>{e.name}</dd></>)}
            {e.model && (<><dt>Model</dt><dd><button className="chip" onClick={() => onSelect(e.model!)}>{e.model}</button>{e.generation && <button className="chip" onClick={() => onSelect(e.generation!)}>{e.generation}</button>}</dd></>)}
            {e.firmware && (<><dt>Firmware</dt><dd>{e.firmware} <span className="subtle">at this time</span></dd></>)}
            {e.location && (<><dt>Location</dt><dd>{e.location}</dd></>)}
            {e.valid_from && (<><dt>In service</dt><dd>since {fmt(e.valid_from)}</dd></>)}
            {node.alarms > 0 && (<><dt>Alarms</dt><dd>{node.alarms} on record</dd></>)}
            {e.members.length > 0 && (<><dt>Devices</dt><dd>{e.members.length} in service</dd></>)}
          </dl>
        )}
        <Section title={`Knowledge in scope at this time · ${active.length}`}>
          {active.map((a) => (
            <div className="rel" key={a.id}>
              <span className="dot" style={{ background: statusColor(stateAt(a, t, k)) }} />
              <button className="linkish" onClick={() => onSelect(a.id)}>{a.label}</button>
            </div>
          ))}
          {active.length === 0 && <div className="subtle small">Nothing documented yet</div>}
        </Section>
        {other.length > 0 && (
          <Section title={`Ended or not yet known · ${other.length}`}>
            {other.map((a) => (
              <div className="rel subtle" key={a.id}>
                <button className="linkish subtle" onClick={() => onSelect(a.id)}>{a.label}</button>
              </div>
            ))}
          </Section>
        )}
        <Section title="What an agent calls (MCP)">
          <div className="codebox mono">get_context("{node.id}", as_of="{new Date(t).toISOString().slice(0, 19)}Z")</div>
        </Section>
      </div>
    </>
  )
}

function SourcePanel({ node, data, onSelect }: { node: GraphNode; data: GraphOut; onSelect: (id: string | null) => void }) {
  const ids = new Set(data.edges.filter((e) => e.rel === 'DERIVED_FROM' && e.target === node.id).map((e) => e.source))
  const produced = data.nodes.filter((n) => ids.has(n.id))
  const isThread = /^(slack|email|teams):/.test(node.id)
  const kind = node.platform === 'email' ? 'Email' : node.platform === 'slack' ? 'Slack thread' : node.sub ?? 'Record'
  return (
    <>
      <Head title={node.label} onClose={() => onSelect(null)}>
        <Pill value="mute" tone="mute" label={kind} />
        {node.platform === 'slack' && node.sub && <span className="subtle small">{node.sub}</span>}
      </Head>
      <div className="drawer-body">
        <dl className="kv">
          <dt>Said</dt>
          <dd>{fmt(node.valid_from)}</dd>
        </dl>
        <Section title={`Knowledge from it · ${produced.length}`}>
          {produced.map((a) => (
            <div className="rel" key={a.id}>
              <span className="dot" style={{ background: statusColor(a.status) }} />
              <button className="linkish" onClick={() => onSelect(a.id)}>{a.label}</button>
            </div>
          ))}
        </Section>
        {isThread && (
          <div className="foot">
            <Link className="btn" to={`/captures/${encodeURIComponent(node.id)}`}>How it was captured</Link>
          </div>
        )}
      </div>
    </>
  )
}
