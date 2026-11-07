import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api, unwrap } from './client'

export const keys = {
  meta: ['meta'] as const,
  graph: ['graph'] as const,
  assertion: (id: string) => ['assertion', id] as const,
  entity: (id: string, asOf: string) => ['entity', id, asOf] as const,
  captures: ['captures'] as const,
  capture: (id: string) => ['capture', id] as const,
  agentRuns: ['agent-runs'] as const,
  eval: ['eval'] as const,
}

export const useMeta = () =>
  useQuery({ queryKey: keys.meta, queryFn: () => unwrap(api.GET('/api/ui/meta')), staleTime: Infinity })

export const useGraph = () =>
  useQuery({ queryKey: keys.graph, queryFn: () => unwrap(api.GET('/api/ui/graph')) })

export const useAssertion = (id: string | null) =>
  useQuery({
    queryKey: keys.assertion(id ?? ''),
    queryFn: () =>
      unwrap(api.GET('/api/ui/assertions/{assertion_id}', { params: { path: { assertion_id: id! } } })),
    enabled: !!id,
    placeholderData: keepPreviousData,
  })

export const useEntity = (id: string | null, asOf: string) =>
  useQuery({
    queryKey: keys.entity(id ?? '', asOf),
    queryFn: () =>
      unwrap(
        api.GET('/api/ui/entities/{entity_id}', {
          params: { path: { entity_id: id! }, query: { as_of: asOf } },
        }),
      ),
    enabled: !!id,
    placeholderData: keepPreviousData,
  })

export const useCaptures = () =>
  useQuery({ queryKey: keys.captures, queryFn: () => unwrap(api.GET('/api/ui/captures')) })

export const useCapture = (id: string | undefined) =>
  useQuery({
    queryKey: keys.capture(id ?? ''),
    queryFn: () =>
      unwrap(api.GET('/api/ui/captures/{thread_id}', { params: { path: { thread_id: id! } } })),
    enabled: !!id,
  })

export const useAgentRuns = () =>
  useQuery({ queryKey: keys.agentRuns, queryFn: () => unwrap(api.GET('/api/ui/agent-runs')) })

export const useEval = () =>
  useQuery({ queryKey: keys.eval, queryFn: () => unwrap(api.GET('/api/ui/eval')) })
