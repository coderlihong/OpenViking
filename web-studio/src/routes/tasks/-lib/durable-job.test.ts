import { describe, expect, it, vi } from 'vitest'

import {
  canCancelJob,
  cancelDurableJob,
  describeFallback,
  describeReason,
  fetchDurableJobs,
  formatProgress,
  formatQueuePosition,
  isReadyPhase,
  isTerminalPhase,
  normalizeDurableJobs,
  summarizeDurableQueue,
} from './durable-job'
import type { DurableJob } from './durable-job'

const clientMocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

vi.mock('#/lib/ov-client', () => ({
  ovClient: { instance: { get: clientMocks.get, post: clientMocks.post } },
}))

function job(overrides: Partial<DurableJob> = {}): DurableJob {
  return {
    job_id: 'job-1',
    state: 'queued',
    stage: 'validate',
    phase: 'queued',
    ...overrides,
  }
}

describe('durable job state projection', () => {
  it('never treats written or an empty queue as ready', () => {
    expect(isReadyPhase('written')).toBe(false)
    expect(isReadyPhase('indexing')).toBe(false)
    expect(isReadyPhase('ready')).toBe(true)
    expect(isReadyPhase('draft_ready')).toBe(false)
    expect(isTerminalPhase('written')).toBe(false)
    expect(isTerminalPhase('cancelled')).toBe(true)
  })

  it('reports an unknown phase rather than guessing', () => {
    expect(
      normalizeDurableJobs([{ job_id: 'a', state: 'x', phase: 'bogus' }]),
    ).toEqual([{ job_id: 'a', state: 'x', stage: '', phase: 'unknown' }])
    expect(normalizeDurableJobs([null, 3, { state: 'queued' }])).toEqual([])
  })

  it('shows the Nth-in-line position and totals', () => {
    const queued = job({
      queue_position: 2,
      queued_total: 5,
      queue_position_scope: 'visible_window',
    })
    expect(formatQueuePosition(queued, 'en')).toBe(
      'Visible waiting order: 2 / 5',
    )
    expect(formatQueuePosition(queued, 'zh-CN')).toBe('当前可见等待顺序：2 / 5')
    expect(formatQueuePosition(job({ queue_position: 0 }), 'en')).toBeNull()
  })

  it('reports unknown progress instead of a fabricated percentage', () => {
    expect(formatProgress(job({ progress_known: false }), 'en')).toBe(
      'Progress unknown',
    )
    expect(formatProgress(job({ progress_known: false }), 'zh-CN')).toBe(
      '进度未知',
    )
    expect(
      formatProgress(
        job({ progress_known: true, processed_units: 4, total_units: 10 }),
        'en',
      ),
    ).toBe('4 / 10')
    // A "known" flag without real units is still unknown.
    expect(formatProgress(job({ progress_known: true }), 'en')).toBe(
      'Progress unknown',
    )
  })

  it('describes a successful auto-fallback as persistent and clear', () => {
    expect(
      describeFallback(
        job({
          phase: 'ready',
          alignment_status: 'failed',
          fallback_strategy: 'rule_blocks',
        }),
        'en',
      ),
    ).toContain('Bookmark alignment did not succeed')
    expect(
      describeFallback(
        job({
          phase: 'ready',
          alignment_status: 'failed',
          structure_source: 'markdown_headings',
        }),
        'zh-CN',
      ),
    ).toContain('书签对齐未成功')
    expect(describeFallback(job({ alignment_status: 'ok' }), 'en')).toBeNull()
    expect(describeFallback(job(), 'en')).toBeNull()
  })

  it('summarizes durable phases into queue rows', () => {
    const rows = summarizeDurableQueue([
      job({ job_id: 'a', phase: 'queued' }),
      job({ job_id: 'b', phase: 'written' }),
      job({ job_id: 'c', phase: 'indexing' }),
      job({ job_id: 'd', phase: 'ready' }),
      job({ job_id: 'e', phase: 'failed' }),
    ])
    const [queue, index] = rows
    // Parse/write queue: one waiting, one ready, one failed. Jobs already
    // written/indexing belong to the index row, not here.
    expect(queue.pending).toBe(1)
    expect(queue.completed).toBe(1)
    expect(queue.errors).toBe(1)
    expect(queue.total).toBe(3)
    expect(index.processing).toBe(1)
    expect(index.pending).toBe(1)
    expect(index.completed).toBe(1)
  })

  it('preserves transport failure instead of displaying an empty queue', async () => {
    clientMocks.get.mockRejectedValueOnce(new Error('network down'))
    await expect(fetchDurableJobs()).rejects.toThrow('network down')
  })

  it('unwraps the response envelope', async () => {
    clientMocks.get.mockResolvedValueOnce({
      data: {
        status: 'ok',
        result: [{ job_id: 'a', state: 'queued', phase: 'queued' }],
      },
    })
    await expect(fetchDurableJobs('manuals')).resolves.toEqual([
      { job_id: 'a', state: 'queued', stage: '', phase: 'queued' },
    ])
    expect(clientMocks.get).toHaveBeenCalledWith('/api/v1/kb/jobs', {
      params: { namespace_id: 'manuals' },
    })
  })
})

describe('durable lifecycle boundaries', () => {
  it('uses durable state rather than a conflicting ready projection or legacy task', () => {
    const [written] = normalizeDurableJobs([
      { job_id: 'a', state: 'written', phase: 'ready' },
    ])
    expect(written.phase).toBe('written')
    expect(
      normalizeDurableJobs([{ task_id: 'legacy', status: 'completed' }]),
    ).toEqual([])
    const [paused] = normalizeDurableJobs([
      { job_id: 'b', state: 'write_recovery_required' },
    ])
    expect(paused.phase).toBe('write_recovery_required')
    expect(isTerminalPhase(paused.phase)).toBe(false)
    expect(
      describeReason(
        { ...paused, reason_code: 'WRITE_RECOVERY_REQUIRED' },
        'en',
      ),
    ).toContain('backend confirmation')
  })

  it('keeps private drafts separate from published completion', () => {
    const rows = summarizeDurableQueue([
      job({ phase: 'draft_ready', state: 'draft_ready' }),
    ])
    expect(rows[0].completed).toBe(0)
    expect(rows[1].completed).toBe(0)
    expect(rows[2].pending).toBe(1)
  })

  it('rejects malformed lists instead of silently treating them as empty', async () => {
    clientMocks.get.mockResolvedValueOnce({
      data: { status: 'ok', result: {} },
    })
    await expect(fetchDurableJobs()).rejects.toThrow('Invalid durable job list')
  })

  it('cancels by durable id and preserves uncertain write cancellation status', async () => {
    clientMocks.post.mockResolvedValueOnce({
      data: {
        status: 'ok',
        result: {
          job_id: 'a',
          state: 'write_recovery_required',
          info: { code: 'CANCEL_REQUESTED' },
        },
      },
    })
    const result = await cancelDurableJob('a')
    expect(clientMocks.post).toHaveBeenCalledWith('/api/v1/kb/jobs/a/cancel')
    expect(result.phase).toBe('write_recovery_required')
    expect(canCancelJob(result)).toBe(false)
    expect(describeReason(result, 'en')).toContain('still need confirmation')
  })
})
