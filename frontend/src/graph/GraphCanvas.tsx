import cytoscape, { type Core, type ElementDefinition, type NodeSingular } from 'cytoscape'
import fcose from 'cytoscape-fcose'
import { useEffect, useMemo, useRef } from 'react'
import type { GraphOut } from '../api/client'
import { isVisible, ms, stateAt, type TimeState } from '../lib/time'

cytoscape.use(fcose)

export type Layers = { network: boolean; knowledge: boolean; source: boolean }

type Props = {
  data: GraphOut | undefined
  t: number
  k: number
  layers: Layers
  selected: string | null
  fresh: string[]
  dark: boolean
  drawerOpen: boolean
  onSelect: (id: string | null) => void
}

const NETWORK = new Set(['device', 'hw_model', 'generation', 'segment', 'site'])
const DRAWER_W = 432

function layerOf(kind: string): keyof Layers {
  if (NETWORK.has(kind)) return 'network'
  return kind === 'source' ? 'source' : 'knowledge'
}

function edgeLabel(rel: string, scope?: string | null) {
  if (scope) return scope.replace('_', ' ')
  return rel.toLowerCase().replace('_', ' ')
}

function toElements(data: GraphOut): ElementDefinition[] {
  const nodes: ElementDefinition[] = data.nodes.map((n) => ({
    group: 'nodes',
    data: {
      id: n.id,
      kind: n.kind,
      label: n.sub && n.kind !== 'source' ? `${n.label}\n${n.sub}` : n.label,
      layer: layerOf(n.kind),
      status: n.status ?? null,
      origin: n.origin ?? null,
      platform: n.platform ?? null,
      tstate: n.status ?? null,
    },
  }))
  const edges: ElementDefinition[] = data.edges.map((e) => ({
    group: 'edges',
    data: { id: e.id, source: e.source, target: e.target, rel: e.rel, lbl: edgeLabel(e.rel, e.scope) },
  }))
  return [...nodes, ...edges]
}

/** For each source node, the assertions it is linked to by any edge. */
function assertionsBySource(data: GraphOut): Map<string, string[]> {
  const kind = new Map(data.nodes.map((n) => [n.id, n.kind]))
  const out = new Map<string, string[]>()
  const link = (source: string, other: string) => {
    if (kind.get(source) !== 'source' || kind.get(other) !== 'assertion') return
    const list = out.get(source)
    if (list) list.push(other)
    else out.set(source, [other])
  }
  for (const e of data.edges) {
    link(e.source, e.target)
    link(e.target, e.source)
  }
  return out
}

/** Run a layout with a fixed seed, so the same graph always lands the same way. */
function seeded(run: () => void, seed = 14) {
  const rnd = Math.random
  let x = seed >>> 0
  Math.random = () => {
    x = (x + 0x6d2b79f5) >>> 0
    let r = x
    r = Math.imul(r ^ (r >>> 15), r | 1)
    r ^= r + Math.imul(r ^ (r >>> 7), r | 61)
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296
  }
  try {
    run()
  } finally {
    Math.random = rnd
  }
}

const LAYOUT = {
  name: 'fcose',
  quality: 'proof',
  animate: false,
  randomize: true,
  nodeRepulsion: 9000,
  idealEdgeLength: (e: cytoscape.EdgeSingular) => (e.data('rel') === 'DERIVED_FROM' ? 45 : 85),
  edgeElasticity: 0.35,
  gravity: 0.3,
  numIter: 3000,
  packComponents: true,
  padding: 40,
} as const

function style(): cytoscape.StylesheetJson {
  const v = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return [
    {
      selector: 'node',
      style: {
        'font-family': 'Open Sans, sans-serif',
        'font-size': 10,
        color: v('--text'),
        'text-wrap': 'wrap',
        'text-max-width': '110px',
        'text-valign': 'bottom',
        'text-margin-y': 5,
        label: 'data(label)',
        'min-zoomed-font-size': 8,
        'transition-property': 'opacity, background-color, border-color',
        'transition-duration': 250,
      },
    },
    { selector: "node[kind='device']", style: { shape: 'round-rectangle', width: 40, height: 26, 'background-color': v('--g-device'), 'border-width': 1.5, 'border-color': v('--g-device-b'), 'font-weight': 600 } },
    { selector: "node[kind='hw_model']", style: { shape: 'hexagon', width: 36, height: 32, 'background-color': v('--g-model'), 'font-weight': 600 } },
    { selector: "node[kind='generation']", style: { shape: 'diamond', width: 22, height: 22, 'background-color': v('--g-model') } },
    { selector: "node[kind='segment'], node[kind='site']", style: { shape: 'barrel', width: 34, height: 20, 'background-color': v('--g-device'), 'border-width': 1.5, 'border-color': v('--g-device-b'), 'border-style': 'dashed' } },
    { selector: "node[kind='topic']", style: { shape: 'round-tag', width: 28, height: 28, 'background-color': v('--g-topic'), 'border-width': 1.5, 'border-color': v('--g-topic-b'), 'font-weight': 700, color: v('--accent'), 'font-size': 11 } },
    // Statements only show when zoomed in (or selected): the overview stays readable.
    { selector: "node[kind='assertion']", style: { shape: 'ellipse', width: 24, height: 24, 'background-color': v('--g-unverified'), 'text-max-width': '120px', 'min-zoomed-font-size': 11 } },
    { selector: "node[tstate='believed_true']", style: { 'background-color': v('--g-believed') } },
    { selector: "node[tstate='disputed']", style: { 'background-color': v('--g-disputed') } },
    { selector: "node[kind='assertion'][origin!='gold']", style: { 'border-width': 3, 'border-color': v('--accent') } },
    { selector: "node[tstate='ended']", style: { 'background-color': v('--surface'), 'border-width': 2, 'border-style': 'dashed', 'border-color': v('--n-09'), color: v('--text-subtle'), opacity: 0.6 } },
    { selector: "node[kind='source']", style: { shape: 'round-rectangle', width: 14, height: 14, 'font-size': 9, color: v('--text-subtle'), 'background-color': v('--g-record'), 'min-zoomed-font-size': 11 } },
    { selector: "node[platform='slack'], node[platform='chat']", style: { 'background-color': v('--g-chat') } },
    { selector: "node[platform='email']", style: { 'background-color': v('--g-email') } },
    { selector: 'edge', style: { width: 1, 'line-color': v('--g-edge'), 'curve-style': 'bezier', opacity: 0.9, 'font-size': 9, 'font-family': 'Open Sans, sans-serif', color: v('--text-subtle'), 'text-background-color': v('--bg'), 'text-background-opacity': 1, 'text-background-padding': '2px', 'text-rotation': 'autorotate' } },
    { selector: "edge[rel='DERIVED_FROM']", style: { 'line-style': 'dotted' } },
    { selector: "edge[rel='SUPERSEDES']", style: { width: 2.5, 'line-color': v('--g-supersedes'), 'target-arrow-shape': 'triangle', 'target-arrow-color': v('--g-supersedes'), 'line-style': 'dashed' } },
    { selector: "edge[rel='CORROBORATES']", style: { width: 2, 'line-color': v('--g-believed') } },
    { selector: "edge[rel='CONTRADICTS']", style: { width: 2.5, 'line-color': v('--g-disputed') } },
    { selector: '.hidden', style: { display: 'none' } },
    { selector: '.dim', style: { opacity: 0.1 } },
    { selector: 'node.hl', style: { 'font-weight': 700, 'z-index': 10, 'min-zoomed-font-size': 0 } },
    { selector: 'edge.hl', style: { opacity: 1, 'z-index': 10, label: 'data(lbl)' } },
    { selector: 'node:selected', style: { 'overlay-color': v('--accent'), 'overlay-opacity': 0.14, 'overlay-padding': 8 } },
    { selector: 'node.pulse', style: { 'overlay-color': v('--accent'), 'overlay-opacity': 0.3, 'overlay-padding': 14 } },
  ]
}

export function GraphCanvas({ data, t, k, layers, selected, fresh, dark, drawerOpen, onSelect }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)
  const onSelectRef = useRef(onSelect)
  useEffect(() => {
    onSelectRef.current = onSelect
  }, [onSelect])

  // One Cytoscape instance for the life of the page.
  useEffect(() => {
    const cy = cytoscape({ container: box.current, style: style(), minZoom: 0.2, maxZoom: 2.5 })
    cy.on('tap', 'node', (e) => onSelectRef.current(e.target.id()))
    cy.on('tap', (e) => {
      if (e.target === cy) onSelectRef.current(null)
    })
    cyRef.current = cy
    return () => {
      cy.destroy()
      cyRef.current = null
    }
  }, [])

  // Theme change: re-read the tokens.
  useEffect(() => {
    cyRef.current?.style(style())
  }, [dark])

  // Data: add what is new, drop what is gone, update the rest; lay out only new nodes.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !data) return
    const defs = toElements(data)
    const want = new Set(defs.map((d) => d.data.id!))
    const first = cy.elements().empty()
    cy.batch(() => {
      cy.elements().filter((el) => !want.has(el.id())).remove()
      for (const d of defs) {
        const el = cy.getElementById(d.data.id!)
        if (el.nonempty()) el.data(d.data)
      }
    })
    const added = cy.add(defs.filter((d) => cy.getElementById(d.data.id!).empty()))
    if (first) {
      seeded(() => cy.layout(LAYOUT as unknown as cytoscape.LayoutOptions).run())
      cy.fit(undefined, 90)
      cy.panBy({ x: 110, y: -30 }) // clear of the left panel and the timeline
      return
    }
    const newNodes = added.nodes()
    if (newNodes.empty()) return
    newNodes.forEach((n: NodeSingular) => {
      const anchor = n.neighborhood().nodes().not(newNodes)[0] as NodeSingular | undefined
      const p = anchor ? anchor.position() : { x: 0, y: 0 }
      n.position({ x: p.x + 40 + Math.random() * 40, y: p.y - 40 - Math.random() * 40 })
    })
    const fixed = cy.nodes().not(newNodes)
    fixed.lock()
    cy.layout({ ...LAYOUT, randomize: false, animate: true, animationDuration: 600, numIter: 600 } as unknown as cytoscape.LayoutOptions)
      .one('layoutstop', () => fixed.unlock())
      .run()
  }, [data])

  // Time and layers: which nodes exist at (t, k), and what state each is in. The slider runs
  // this every frame, so only what changed touches Cytoscape, and the edges are revisited only
  // when some node appeared or disappeared.
  const bySource = useMemo(() => (data ? assertionsBySource(data) : new Map<string, string[]>()), [data])
  const shownFor = useRef<GraphOut | null>(null)
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !data) return
    const show = new Map<string, boolean>()
    const state = new Map<string, TimeState>()
    for (const n of data.nodes) {
      if (n.kind === 'source') continue
      let on = layers[layerOf(n.kind)]
      if (n.kind === 'assertion') {
        const st = stateAt(n, t, k)
        state.set(n.id, st)
        on &&= isVisible(st)
      } else {
        const vf = ms(n.valid_from)
        const vt = ms(n.valid_to)
        on &&= (vf === null || vf <= t) && (vt === null || vt > t)
      }
      show.set(n.id, on)
    }
    // A source shows only with something it supports.
    for (const n of data.nodes) {
      if (n.kind !== 'source') continue
      show.set(n.id, layers.source && (bySource.get(n.id) ?? []).some((id) => show.get(id) === true))
    }
    let changed = shownFor.current !== data
    shownFor.current = data
    cy.batch(() => {
      cy.nodes().forEach((el) => {
        const id = el.id()
        const on = show.get(id)
        if (on === undefined) return
        const st = state.get(id)
        if (st !== undefined && el.data('tstate') !== st) el.data('tstate', st)
        if (el.hasClass('hidden') !== !on) {
          el.toggleClass('hidden', !on)
          changed = true
        }
      })
      if (!changed) return
      cy.edges().forEach((e) => {
        e.toggleClass('hidden', e.source().hasClass('hidden') || e.target().hasClass('hidden'))
      })
    })
  }, [data, bySource, t, k, layers])

  // Selection: dim the rest, bring the neighbourhood into view beside the drawer.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.elements().removeClass('dim hl')
    cy.$(':selected').unselect()
    if (!selected) return
    const n = cy.getElementById(selected)
    if (n.empty()) return
    n.select()
    const hood = n.closedNeighborhood().not('.hidden')
    cy.elements().not(hood).addClass('dim')
    hood.addClass('hl')
    const w = cy.width() - (drawerOpen ? DRAWER_W : 0)
    const zoom = Math.min(1.5, Math.max(cy.zoom(), 0.9))
    cy.animate({
      zoom,
      pan: { x: w / 2 - n.position('x') * zoom + 120, y: (cy.height() - 120) / 2 - n.position('y') * zoom },
      duration: 400,
    })
  }, [selected, drawerOpen])

  // New knowledge: pulse it once, as soon as its node exists (the refetch may land later).
  const pulsed = useRef<string>('')
  useEffect(() => {
    const cy = cyRef.current
    const key = fresh.join(',')
    if (!cy || fresh.length === 0 || pulsed.current === key) return
    const nodes = fresh.reduce((acc, id) => acc.union(cy.getElementById(id)), cy.collection())
    if (nodes.empty()) return
    pulsed.current = key
    let i = 0
    const timer = window.setInterval(() => {
      nodes.toggleClass('pulse')
      if (++i > 9) {
        window.clearInterval(timer)
        nodes.removeClass('pulse')
      }
    }, 350)
  }, [fresh, data])

  return <div ref={box} className="cy" role="img" aria-label="Knowledge graph" />
}
