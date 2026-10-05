// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react'
import { createInstance } from 'i18next'
import { I18nextProvider } from 'react-i18next'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DurableJobsPanel } from './durable-jobs-panel'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('#/lib/ov-client', () => ({ ovClient: { instance: api } }))
const clients: QueryClient[] = []

async function show(scope = 'account/user', existingClient?: QueryClient) {
  const i18n = createInstance()
  await i18n.init({ lng: 'en', resources: { en: { tasksPage: {} } } })
  const client =
    existingClient ??
    new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
  clients.push(client)
  return render(
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <DurableJobsPanel identityScopeKey={scope} />
      </QueryClientProvider>
    </I18nextProvider>,
  )
}

beforeEach(() => {
  api.get.mockReset()
  api.post.mockReset()
})
afterEach(() => {
  cleanup()
  for (const client of clients.splice(0)) client.clear()
})

function envelope(result: unknown) {
  return { data: { status: 'ok', result } }
}
const written = {
  job_id: 'job-1',
  kind: 'D1',
  namespace_id: 'manuals',
  state: 'written',
  stage: 'index',
}

describe('durable job panel', () => {
  it('shows an unavailable surface as unknown rather than empty', async () => {
    api.get.mockRejectedValue(new Error('authority unavailable'))
    await show()
    expect((await screen.findByRole('alert')).textContent).toContain(
      'does not mean the queue is empty',
    )
    expect(screen.queryByText(/No durable jobs/)).toBeNull()
  })

  it('keeps written and private drafts separate from ready', async () => {
    api.get.mockResolvedValue(
      envelope([
        written,
        { job_id: 'draft', state: 'draft_ready', stage: 'wiki_validate' },
      ]),
    )
    await show()
    await screen.findByText('Written; index pending')
    await screen.findByText('Private draft ready; unpublished')
    expect(screen.queryByText('Published and ready')).toBeNull()
    expect(screen.getAllByText('Progress unknown')).toHaveLength(2)
    expect(
      screen.queryByRole('button', { name: 'Cancel job draft' }),
    ).toBeNull()
  })

  it('sends durable cancellation and shows unresolved writes as pending confirmation', async () => {
    const recovery = {
      ...written,
      state: 'write_recovery_required',
      reason_code: 'CANCEL_REQUESTED',
      info: { code: 'CANCEL_REQUESTED' },
    }
    api.get
      .mockResolvedValueOnce(envelope([written]))
      .mockResolvedValue(envelope([recovery]))
    api.post.mockResolvedValue(envelope(recovery))
    await show()
    fireEvent.click(
      await screen.findByRole('button', { name: 'Cancel job job-1' }),
    )
    await screen.findByText(/in-flight writes still need confirmation/)
    expect(api.post).toHaveBeenCalledWith('/api/v1/kb/jobs/job-1/cancel')
    expect(screen.queryByText('Cancelled')).toBeNull()
    expect(
      screen.queryByRole('button', { name: 'Cancel job job-1' }),
    ).toBeNull()
  })

  it('separates cached jobs when the current identity changes', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    api.get
      .mockResolvedValueOnce(envelope([written]))
      .mockResolvedValue(envelope([]))
    const first = await show('account/alice', client)
    await screen.findByText('job-1')
    first.unmount()
    await show('account/bob', client)
    await screen.findByText(/No durable jobs are currently visible/)
    expect(screen.queryByText('job-1')).toBeNull()
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
  })
})
