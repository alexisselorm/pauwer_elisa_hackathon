import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Change } from './api/client'
import { keys } from './api/hooks'
import { LiveContext, type Fresh } from './live-context'

/** Server-sent events from /api/ui/stream: refetch what changed, remember what is new. */

const FRESH_MS = 12_000

export function LiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const [connected, setConnected] = useState(false)
  const [fresh, setFresh] = useState<Fresh | null>(null)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    const es = new EventSource('/api/ui/stream')
    es.addEventListener('ready', () => setConnected(true))
    es.onerror = () => setConnected(false) // EventSource reconnects by itself
    es.addEventListener('change', (ev) => {
      setConnected(true)
      const c = JSON.parse((ev as MessageEvent<string>).data) as Change
      if (c.assertions) {
        void qc.invalidateQueries({ queryKey: keys.graph })
        void qc.invalidateQueries({ queryKey: ['assertion'] })
        void qc.invalidateQueries({ queryKey: ['entity'] })
      }
      if (c.captures) {
        void qc.invalidateQueries({ queryKey: keys.captures })
        void qc.invalidateQueries({ queryKey: ['capture'] })
      }
      if (c.agents) void qc.invalidateQueries({ queryKey: keys.agentRuns })
      const assertions = c.new_assertions ?? []
      const captures = c.new_captures ?? []
      if (assertions.length || captures.length) {
        setFresh({ assertions, captures, at: Date.now() })
        window.clearTimeout(timer.current)
        timer.current = window.setTimeout(() => setFresh(null), FRESH_MS)
      }
    })
    return () => {
      es.close()
      window.clearTimeout(timer.current)
    }
  }, [qc])

  return (
    <LiveContext.Provider value={{ connected, fresh, dismiss: () => setFresh(null) }}>
      {children}
    </LiveContext.Provider>
  )
}
