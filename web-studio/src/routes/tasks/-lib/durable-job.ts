import { ovClient } from '#/lib/ov-client'

/**
 * Durable KB job status as returned by GET /api/v1/kb/jobs.
 *
 * These are authority-owned job records. Observer queues remain separate;
 * visible waiting order is not a global scheduler position.
 */
export type DurableJobPhase =
  | 'queued'
  | 'parsing'
  | 'written'
  | 'indexing'
  | 'ready'
  | 'draft_ready'
  | 'retry'
  | 'failed'
  | 'cancelled'
  | 'write_recovery_required'
  | 'unknown'

export type DurableJob = {
  job_id: string
  task_id?: string
  kind?: string
  namespace_id?: string
  document_id?: string | null
  state: string
  stage: string
  phase: DurableJobPhase
  attempt?: number
  queue_position?: number
  queued_total?: number
  queue_position_scope?: 'visible_window'
  reason_code?: string | null
  progress_known?: boolean
  processed_units?: number | null
  total_units?: number | null
  retryable?: boolean
  resume_granularity?: string | null
  alignment_status?: string | null
  structure_source?: string | null
  fallback_strategy?: string | null
  artifact_uri?: string | null
  trace_id?: string | null
  info?: Record<string, unknown>
  error?: string | null
  warnings?: string[]
  heartbeat?: number | null
  created?: number
  updated?: number
}

/** Only an explicit authority-verified state counts as ready. */
export function isReadyPhase(phase: DurableJobPhase): boolean {
  return phase === 'ready'
}

/** Terminal states never masquerade as success; `written` is not `ready`. */
export function isTerminalPhase(phase: DurableJobPhase): boolean {
  return (
    phase === 'ready' ||
    phase === 'draft_ready' ||
    phase === 'failed' ||
    phase === 'cancelled'
  )
}

export function normalizeDurableJob(value: unknown): DurableJob | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return undefined
  }
  const record = value as Record<string, unknown>
  const jobId = record.job_id
  if (typeof jobId !== 'string' || !jobId) {
    return undefined
  }
  const state = typeof record.state === 'string' ? record.state : 'unknown'
  const stage = typeof record.stage === 'string' ? record.stage : ''
  const direct: Record<string, DurableJobPhase> = {
    queued: 'queued',
    backoff: 'retry',
    written: 'written',
    ready: 'ready',
    draft_ready: 'draft_ready',
    failed: 'failed',
    cancelled: 'cancelled',
    write_recovery_required: 'write_recovery_required',
  }
  const phase: DurableJobPhase =
    state === 'running'
      ? ['index', 'wiki_extract', 'wiki_validate', 'publish_gate'].includes(
          stage,
        )
        ? 'indexing'
        : 'parsing'
      : (direct[state] ?? 'unknown')
  return {
    ...(record as DurableJob),
    job_id: jobId,
    state: typeof record.state === 'string' ? record.state : 'unknown',
    stage: typeof record.stage === 'string' ? record.stage : '',
    phase,
  }
}

export function normalizeDurableJobs(value: unknown): DurableJob[] {
  if (!Array.isArray(value)) return []
  return value
    .map(normalizeDurableJob)
    .filter((job): job is DurableJob => Boolean(job))
}

function unwrapEnvelope(value: unknown): unknown {
  if (value && typeof value === 'object' && 'result' in value) {
    return (value as { result: unknown }).result
  }
  return value
}

/** Transport and malformed-response failures remain observable in the UI. */
export async function fetchDurableJobs(
  namespaceId?: string,
): Promise<DurableJob[]> {
  const response = await ovClient.instance.get('/api/v1/kb/jobs', {
    params: namespaceId ? { namespace_id: namespaceId } : undefined,
  })
  const value = unwrapEnvelope(response.data)
  if (!Array.isArray(value))
    throw new Error('Invalid durable job list response')
  const jobs = normalizeDurableJobs(value)
  if (jobs.length !== value.length)
    throw new Error('Invalid durable job record')
  return jobs
}

export async function cancelDurableJob(jobId: string): Promise<DurableJob> {
  const response = await ovClient.instance.post(
    `/api/v1/kb/jobs/${encodeURIComponent(jobId)}/cancel`,
  )
  const job = normalizeDurableJob(unwrapEnvelope(response.data))
  if (!job) throw new Error('Invalid cancellation response')
  return job
}

export function canCancelJob(job: DurableJob): boolean {
  return (
    !isTerminalPhase(job.phase) &&
    job.phase !== 'unknown' &&
    job.info?.code !== 'CANCEL_REQUESTED'
  )
}

export function describeReason(
  job: DurableJob,
  language: string,
): string | null {
  const labels: Partial<Record<string, [string, string]>> = {
    PUBLICATION_GATE_PENDING: [
      '等待共同发布门禁，内容尚未就绪',
      'Waiting for publication checks; content is not ready',
    ],
    PARSE_SLOT_BUSY: [
      '等待共享解析写入时段',
      'Waiting for the shared parser/write slot',
    ],
    WORKER_LEASE_EXPIRED: [
      '执行者已离线，任务等待恢复',
      'Worker lease expired; waiting to resume',
    ],
    WRITE_RECOVERY_REQUIRED: [
      '写入结果未知，需确认后端状态后才能恢复',
      'Write outcome is unknown; backend confirmation is required',
    ],
    CANCEL_REQUESTED: [
      '已请求取消；在途写入仍需确认，不能视为已结束',
      'Cancellation requested; in-flight writes still need confirmation',
    ],
    PERMISSION_DENIED: [
      '当前权限不足，任务已停止',
      'Current permissions do not allow this task',
    ],
  }
  const code =
    job.reason_code ?? (typeof job.info?.code === 'string' ? job.info.code : '')
  const label = labels[code]
  return label
    ? label[language.startsWith('zh') ? 0 : 1]
    : job.error || code || null
}

/** "第 N 本 / 共 M 本" — 0 position means the job is not waiting in line. */
export function formatQueuePosition(
  job: DurableJob,
  language: string,
): string | null {
  const position = job.queue_position ?? 0
  const total = job.queued_total ?? 0
  if (position <= 0 || job.queue_position_scope !== 'visible_window')
    return null
  if (language.startsWith('zh')) {
    return `当前可见等待顺序：${position} / ${total}`
  }
  return `Visible waiting order: ${position} / ${total}`
}

/**
 * Progress text. Unknown progress is reported as unknown, never as 0% or a
 * fabricated percentage.
 */
export function formatProgress(job: DurableJob, language: string): string {
  const known =
    job.progress_known === true &&
    typeof job.processed_units === 'number' &&
    typeof job.total_units === 'number' &&
    Number.isInteger(job.processed_units) &&
    Number.isInteger(job.total_units) &&
    job.processed_units >= 0 &&
    job.processed_units <= job.total_units &&
    job.total_units > 0
  if (!known) {
    return language.startsWith('zh') ? '进度未知' : 'Progress unknown'
  }
  return `${job.processed_units} / ${job.total_units}`
}

/**
 * Human notice for a successful automatic fallback. It is persistent (read
 * from durable checkpoint/info) and explicit that the bookmark alignment did
 * not succeed, rather than silently reporting a plain success.
 */
export function describeFallback(
  job: DurableJob,
  language: string,
): string | null {
  const aligned = (job.alignment_status || '').toLowerCase()
  if (
    !aligned ||
    aligned === 'ok' ||
    aligned === 'aligned' ||
    aligned === 'success'
  ) {
    return null
  }
  const strategy = job.fallback_strategy || job.structure_source || ''
  if (language.startsWith('zh')) {
    return `书签对齐未成功，已按标题/规则切分（${strategy || '规则'}）`
  }
  return `Bookmark alignment did not succeed; split by headings/rules (${strategy || 'rules'})`
}

export function durablePhaseLabelKey(job: DurableJob): string {
  return `durable.phase.${job.phase}`
}

/** Queue summary rows for the monitoring queue-status card. */
export function summarizeDurableQueue(jobs: DurableJob[]): {
  name: string
  processing: number
  pending: number
  completed: number
  errors: number
  total: number
}[] {
  const rows = [
    {
      name: 'DurableQueue',
      processing: 0,
      pending: 0,
      completed: 0,
      errors: 0,
    },
    {
      name: 'DurableIndex',
      processing: 0,
      pending: 0,
      completed: 0,
      errors: 0,
    },
    {
      name: 'DurableDraft',
      processing: 0,
      pending: 0,
      completed: 0,
      errors: 0,
    },
  ]
  for (const job of jobs) {
    if (job.phase === 'queued' || job.phase === 'retry') rows[0].pending += 1
    else if (job.phase === 'parsing') rows[0].processing += 1
    else if (isReadyPhase(job.phase)) rows[0].completed += 1
    else if (
      job.phase === 'failed' ||
      job.phase === 'write_recovery_required'
    ) {
      rows[0].errors += 1
    } else if (job.phase === 'draft_ready') rows[2].pending += 1

    if (job.phase === 'indexing') rows[1].processing += 1
    else if (job.phase === 'written') rows[1].pending += 1
    else if (isReadyPhase(job.phase)) rows[1].completed += 1
  }
  const total = (row: (typeof rows)[number]) =>
    row.processing + row.pending + row.completed + row.errors
  return [...rows.map((row) => ({ ...row, total: total(row) }))]
}
