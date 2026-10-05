import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ListTodoIcon, LoaderCircleIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { Badge } from '#/components/ui/badge'
import { Button } from '#/components/ui/button'
import { Card, CardTitle } from '#/components/ui/card'
import { cn } from '#/lib/utils'
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
} from '#/routes/tasks/-lib/durable-job'
import type { DurableJob } from '#/routes/tasks/-lib/durable-job'

const PHASE_LABEL: Record<string, [string, string]> = {
  queued: ['排队中', 'Queued'],
  parsing: ['解析写入中', 'Parsing and writing'],
  written: ['内容已写入，索引未就绪', 'Written; index pending'],
  indexing: ['索引中', 'Indexing'],
  ready: ['已发布就绪', 'Published and ready'],
  draft_ready: ['私有草稿就绪，尚未发布', 'Private draft ready; unpublished'],
  retry: ['等待恢复或重试', 'Waiting to resume'],
  failed: ['失败', 'Failed'],
  cancelled: ['已取消', 'Cancelled'],
  write_recovery_required: ['写入结果待确认', 'Write recovery required'],
  unknown: ['状态未知', 'Unknown'],
}
const STAGE_LABEL: Record<string, [string, string]> = {
  validate: ['检查输入与权限', 'Input and permission checks'],
  parse_write: ['解析与正文写入', 'Parsing and content writes'],
  index: ['生成索引', 'Building indexes'],
  wiki_extract: ['抽取知识候选', 'Extracting knowledge candidates'],
  wiki_validate: ['验证私有候选', 'Validating private candidates'],
  publish_gate: ['验证共同发布条件', 'Checking publication requirements'],
}

const COPY = {
  title: ['我的持久任务', 'My durable jobs'],
  refresh: ['刷新任务', 'Refresh jobs'],
  description: [
    '这里显示当前用户仍有权限查看的最近任务。正文写入或处理队列完成后，仍需通过发布检查才能就绪。',
    'Recent jobs you can currently access. Content writes and processing queue completion still require publication checks before ready.',
  ],
  cancellationFailed: ['取消请求失败：', 'Cancellation failed: '],
  loading: ['加载持久任务…', 'Loading durable jobs…'],
  unavailable: [
    '持久任务状态暂时无法读取，请刷新重试；这不表示任务队列为空。',
    'Durable job status is unavailable. Refresh to retry; this does not mean the queue is empty.',
  ],
  empty: [
    '当前没有可见的持久任务；这不代表内容已发布就绪。',
    'No durable jobs are currently visible; this does not imply content is ready.',
  ],
  cancelling: ['正在请求取消…', 'Requesting cancellation…'],
  cancel: ['取消任务', 'Cancel job'],
} as const

function phaseTone(job: DurableJob): string {
  if (isReadyPhase(job.phase)) return 'text-emerald-600 dark:text-emerald-400'
  if (job.phase === 'failed') return 'text-destructive'
  if (
    ['written', 'draft_ready', 'write_recovery_required'].includes(job.phase)
  ) {
    return 'text-amber-600 dark:text-amber-400'
  }
  return 'text-muted-foreground'
}

export function DurableJobsPanel({
  identityScopeKey,
  namespaceId,
}: {
  identityScopeKey: string
  namespaceId?: string
}) {
  const { i18n } = useTranslation('tasksPage')
  const isZh = i18n.language.startsWith('zh')
  const copy = (key: keyof typeof COPY) => COPY[key][isZh ? 0 : 1]
  const queryClient = useQueryClient()
  const jobsQuery = useQuery({
    queryKey: ['durable-jobs', identityScopeKey, namespaceId ?? 'all'],
    queryFn: () => fetchDurableJobs(namespaceId),
    refetchInterval: 5_000,
  })
  const cancellation = useMutation({
    mutationFn: ({ jobId }: { jobId: string; scope: string }) =>
      cancelDurableJob(jobId),
    onSuccess: async (_job, variables) => {
      await queryClient.invalidateQueries({
        queryKey: ['durable-jobs', variables.scope],
      })
    },
  })
  const jobs = jobsQuery.data ?? []
  const cancellationError =
    cancellation.isError && cancellation.variables.scope === identityScopeKey

  return (
    <Card className="gap-3 p-3 shadow-none">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ListTodoIcon className="size-4 text-muted-foreground" />
          <CardTitle className="text-sm font-semibold">
            {copy('title')}
          </CardTitle>
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={jobsQuery.isFetching}
          onClick={() => void jobsQuery.refetch()}
        >
          {copy('refresh')}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">{copy('description')}</p>
      {cancellationError ? (
        <p role="alert" className="text-xs text-destructive">
          {copy('cancellationFailed')}
          {String(
            cancellation.error instanceof Error
              ? cancellation.error.message
              : cancellation.error,
          )}
        </p>
      ) : null}
      {jobsQuery.isLoading ? (
        <div className="flex items-center gap-2 py-3 text-xs text-muted-foreground">
          <LoaderCircleIcon className="size-3.5 animate-spin" />
          {copy('loading')}
        </div>
      ) : jobsQuery.isError ? (
        <p role="alert" className="text-xs text-destructive">
          {copy('unavailable')}
        </p>
      ) : jobs.length === 0 ? (
        <p className="rounded-lg border bg-muted/20 p-2.5 text-center text-xs text-muted-foreground">
          {copy('empty')}
        </p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {jobs.map((job) => {
            const position = formatQueuePosition(job, i18n.language)
            const fallback = describeFallback(job, i18n.language)
            const reason = describeReason(job, i18n.language)
            const pendingCancel =
              cancellation.isPending &&
              cancellation.variables.jobId === job.job_id &&
              cancellation.variables.scope === identityScopeKey
            return (
              <div
                key={job.job_id}
                className="grid gap-1 rounded-lg border bg-muted/15 px-2.5 py-2 text-xs"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <code className="min-w-0 truncate font-mono text-[11px]">
                    {job.document_id || job.job_id}
                  </code>
                  <Badge
                    variant={
                      isTerminalPhase(job.phase) ? 'secondary' : 'outline'
                    }
                    className={cn('shrink-0 font-normal', phaseTone(job))}
                  >
                    {
                      (PHASE_LABEL[job.phase] ?? PHASE_LABEL.unknown)[
                        isZh ? 0 : 1
                      ]
                    }
                  </Badge>
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                  <span>
                    {job.kind} · {job.namespace_id}
                  </span>
                  {position ? <span>{position}</span> : null}
                  <span>{formatProgress(job, i18n.language)}</span>
                  {job.stage ? (
                    <span>
                      {
                        (STAGE_LABEL[job.stage] ?? [job.stage, job.stage])[
                          isZh ? 0 : 1
                        ]
                      }
                    </span>
                  ) : null}
                  {job.attempt ? (
                    <span>
                      {isZh
                        ? `第 ${job.attempt} 次尝试`
                        : `Attempt ${job.attempt}`}
                    </span>
                  ) : null}
                </div>
                {reason ? (
                  <p className="text-[11px] text-amber-600 dark:text-amber-400">
                    {reason}
                  </p>
                ) : null}
                {fallback ? (
                  <p className="text-[11px] text-muted-foreground">
                    {fallback}
                  </p>
                ) : null}
                {canCancelJob(job) ? (
                  <div>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={pendingCancel}
                      aria-label={
                        isZh
                          ? `取消任务 ${job.job_id}`
                          : `Cancel job ${job.job_id}`
                      }
                      onClick={() =>
                        cancellation.mutate({
                          jobId: job.job_id,
                          scope: identityScopeKey,
                        })
                      }
                    >
                      {pendingCancel ? copy('cancelling') : copy('cancel')}
                    </Button>
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
