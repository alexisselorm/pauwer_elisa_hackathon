import { useMutation, useQueryClient } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import { api, unwrap } from '../api/client'

/** Reset the graph to the demo baseline (POST /api/ui/reset): live captures, agent calls
 *  and their effects are deleted, the network and the seeded knowledge come back. */
export function ResetButton() {
  const qc = useQueryClient()
  const reset = useMutation({
    mutationFn: () => unwrap(api.POST('/api/ui/reset')),
    onSettled: () => qc.invalidateQueries(),
  })
  const title = reset.isError
    ? `Reset failed: ${reset.error.message}`
    : reset.isSuccess
      ? `Reset: ${reset.data.deleted_nodes} nodes deleted, ${reset.data.assertions} seeded assertions restored`
      : 'Reset the graph to the demo baseline'

  const run = () => {
    const ok = window.confirm(
      'Reset the graph to the demo baseline?\n\nEvery capture, agent run and outcome since seeding is deleted. ' +
        'Slack messages stay in Slack but are not captured again.',
    )
    if (ok) reset.mutate()
  }

  return (
    <button
      className={`btn reset ${reset.isError ? 'failed' : ''}`}
      onClick={run}
      disabled={reset.isPending}
      title={title}
    >
      <RotateCcw size={14} className={reset.isPending ? 'spin' : undefined} />
      {reset.isPending ? 'Resetting…' : reset.isError ? 'Reset failed' : 'Reset'}
    </button>
  )
}
