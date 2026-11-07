import { Moon, Sun } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes } from 'react-router'
import { AgentRunsPage } from './agents/AgentRunsPage'
import { CapturesPage } from './captures/CapturesPage'
import { GraphPage } from './graph/GraphPage'
import { useLive } from './live-context'
import { ScorecardPage } from './score/ScorecardPage'

type Theme = 'light' | 'dark'

function readTheme(): Theme | null {
  try {
    const t = localStorage.getItem('theme')
    return t === 'light' || t === 'dark' ? t : null
  } catch {
    return null
  }
}

function useTheme() {
  const [theme, setTheme] = useState<Theme | null>(readTheme)
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme
    try {
      if (theme) localStorage.setItem('theme', theme)
    } catch {
      /* private mode: the choice lasts for this page only */
    }
  }, [theme])
  const dark = theme ? theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches
  return { dark, toggle: () => setTheme(dark ? 'light' : 'dark') }
}

export function App() {
  const { connected } = useLive()
  const { dark, toggle } = useTheme()
  return (
    <div className="app">
      <header className="top">
        <Link to="/" className="brand" aria-label="silentctx home">
          <img src="/favicon.svg" alt="" />
          <b>silentctx</b>
          <span>Context layer</span>
        </Link>
        <nav className="tabs" aria-label="Views">
          <NavLink to="/" end>Graph</NavLink>
          <NavLink to="/captures">Captures</NavLink>
          <NavLink to="/agents">Agent runs</NavLink>
          <NavLink to="/scorecard">Scorecard</NavLink>
        </nav>
        <div className="hdr-right">
          <span className="subtle small">Slack · Email · MCP</span>
          <span className={`live ${connected ? 'on' : ''}`} title={connected ? 'Live updates on' : 'Reconnecting'}>
            <i />
            {connected ? 'Live' : 'Offline'}
          </span>
          <button className="icon-btn" onClick={toggle} aria-label="Toggle theme" data-dark={dark}>
            {dark ? <Sun size={16} /> : <Moon size={16} />}
          </button>
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<GraphPage dark={dark} />} />
          <Route path="/captures/:threadId?" element={<CapturesPage />} />
          <Route path="/agents/:runId?" element={<AgentRunsPage />} />
          <Route path="/scorecard" element={<ScorecardPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
