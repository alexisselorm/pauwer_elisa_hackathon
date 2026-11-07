import type { ReactNode } from 'react'

/** Mark the extractor's verbatim quotes in a message (V1 grounding made visible).
 *  A quote may join fragments with "…"; each fragment is marked on its own. */

export const CATEGORIES = ['action', 'avoid', 'condition', 'observed', 'entity'] as const
export type Category = (typeof CATEGORIES)[number]

type Range = { start: number; end: number; cat: Category }

export function highlight(text: string, quotes: Partial<Record<Category, string[]>>): ReactNode[] {
  const lower = text.toLowerCase()
  const ranges: Range[] = []
  for (const cat of CATEGORIES) {
    for (const quote of quotes[cat] ?? []) {
      for (const frag of quote.split(/\s*…\s*|\s*\.\.\.\s*/)) {
        const f = frag.trim().toLowerCase()
        if (f.length < 3) continue
        let from = 0
        for (;;) {
          const i = lower.indexOf(f, from)
          if (i < 0) break
          ranges.push({ start: i, end: i + f.length, cat })
          from = i + f.length
        }
      }
    }
  }
  // Earlier categories win where marks overlap.
  const taken: Range[] = []
  for (const r of ranges) {
    if (!taken.some((x) => r.start < x.end && x.start < r.end)) taken.push(r)
  }
  taken.sort((a, b) => a.start - b.start)
  const out: ReactNode[] = []
  let at = 0
  taken.forEach((r, i) => {
    if (r.start > at) out.push(text.slice(at, r.start))
    out.push(
      <mark key={i} className={r.cat} title={r.cat}>
        {text.slice(r.start, r.end)}
      </mark>,
    )
    at = r.end
  })
  if (at < text.length) out.push(text.slice(at))
  return out
}
