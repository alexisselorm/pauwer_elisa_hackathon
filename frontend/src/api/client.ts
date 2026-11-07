import createClient from 'openapi-fetch'
import type { components, paths } from './schema'

// Types come from the API's OpenAPI schema: `npm run gen:api` regenerates schema.d.ts.
export type Schemas = components['schemas']
export type GraphOut = Schemas['GraphOut']
/** An assertion that SUPERSEDES another, as the graph lists it on the predecessor. */
export type Successor = { assertion_id: string; recorded_at: string; retracted_at: string | null }
// `successors` is added here by hand until schema.d.ts is regenerated from an API that serves
// it; the intersection is harmless once it is. Missing means "none known".
export type GraphNode = Schemas['GraphNode'] & { successors?: Successor[] }
export type GraphEdge = Schemas['GraphEdge']
export type AssertionDetail = Schemas['AssertionDetail']
export type AssertionView = Schemas['AssertionView']
export type EntityView = Schemas['EntityView']
export type CaptureSummary = Schemas['CaptureSummary']
export type CaptureDetail = Schemas['CaptureDetail']
export type ItemView = Schemas['ItemView']
export type Gate = Schemas['Gate']
export type AgentRun = Schemas['AgentRun']
export type ToolCallView = Schemas['ToolCallView']
export type EvalReportView = Schemas['EvalReportView']
export type Meta = Schemas['Meta']

/** A `change` event on /api/ui/stream (backend: kaiku.ui.models.Change). SSE payloads are
 *  not in the OpenAPI schema, so this one is written by hand. */
export type Change = {
  assertions: boolean
  captures: boolean
  agents: boolean
  new_assertions: string[]
  new_captures: string[]
}

export const api = createClient<paths>({ baseUrl: '' })

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
  }
}

/** Unwrap an openapi-fetch result: data, or an ApiError with the problem+json detail. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call
  if (error !== undefined || data === undefined) {
    const detail =
      typeof error === 'object' && error && 'detail' in error ? String(error.detail) : response.statusText
    throw new ApiError(response.status, detail)
  }
  return data
}
