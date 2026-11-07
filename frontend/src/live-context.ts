import { createContext, useContext } from 'react'

export type Fresh = { assertions: string[]; captures: string[]; at: number }

type Live = { connected: boolean; fresh: Fresh | null; dismiss: () => void }

export const LiveContext = createContext<Live>({ connected: false, fresh: null, dismiss: () => {} })

export const useLive = () => useContext(LiveContext)
