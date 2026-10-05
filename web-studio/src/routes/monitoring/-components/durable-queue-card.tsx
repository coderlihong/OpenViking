import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { cn } from '#/lib/utils'
import { QueueStatusCard } from '#/routes/monitoring/-components/queue-status-card'
import {
  fetchDurableJobs,
  summarizeDurableQueue,
} from '#/routes/tasks/-lib/durable-job'

const DURABLE_LABELS = {
  DurableQueue: { zh: '持久解析队列', en: 'Durable parse queue' },
  DurableIndex: { zh: '持久索引队列', en: 'Durable index queue' },
  DurableDraft: { zh: '私有未发布草稿', en: 'Private unpublished drafts' },
} as const

const COPY = {
  loading: ['正在读取持久任务状态…', 'Loading durable job status…'],
  unavailable: [
    '持久任务状态无法读取，当前队列状态未知。',
    'Durable job status is unavailable; queue state is unknown.',
  ],
  title: ['我的持久任务状态', 'My durable job status'],
  description: [
    '仅统计当前有权限查看的最近任务，独立于处理队列。写入后仍需发布验证；私有草稿尚未发布。',
    'Recent jobs you can currently access, separate from processing queues. Written content needs publication checks; private drafts are unpublished.',
  ],
} as const

/**
 * Queue status card backed by the durable KB job surface (F3).
 *
 * Counts are the current user's visible recent jobs, separately from Observer
 * processing queues. Transport failures cannot be displayed as an empty queue.
 */
export function DurableQueueCard({
  identityScopeKey,
}: {
  identityScopeKey: string
}) {
  const { i18n } = useTranslation('monitoringPage')
  const isZh = i18n.language.startsWith('zh')
  const copy = (key: keyof typeof COPY) => COPY[key][isZh ? 0 : 1]
  const jobsQuery = useQuery({
    queryKey: ['durable-jobs', identityScopeKey, 'monitoring'],
    queryFn: () => fetchDurableJobs(),
    refetchInterval: 5_000,
  })
  const jobs = jobsQuery.data ?? []
  const rows = summarizeDurableQueue(jobs)
  const failed = jobs.filter(
    (job) => job.phase === 'failed' || job.phase === 'write_recovery_required',
  ).length
  const labels: Record<string, string> = {}
  for (const [key, value] of Object.entries(DURABLE_LABELS)) {
    labels[key] = isZh ? value.zh : value.en
  }

  if (jobsQuery.isLoading)
    return <p className="text-xs text-muted-foreground">{copy('loading')}</p>
  if (jobsQuery.isError)
    return (
      <p role="alert" className="text-xs text-destructive">
        {copy('unavailable')}
      </p>
    )

  return (
    <div className="grid gap-2">
      <QueueStatusCard
        title={copy('title')}
        customRows={rows}
        isHealthy={failed === 0}
        labels={labels}
      />
      <p className={cn('text-[11px] text-muted-foreground')}>
        {copy('description')}
      </p>
    </div>
  )
}
