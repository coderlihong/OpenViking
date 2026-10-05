import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { LoaderCircleIcon, RotateCwIcon, TriangleAlertIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { Button } from '#/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '#/components/ui/card'
import { Input } from '#/components/ui/input'
import { Label } from '#/components/ui/label'
import { Switch } from '#/components/ui/switch'
import { Textarea } from '#/components/ui/textarea'
import { useAppConnection } from '#/hooks/use-app-connection'
import { applyRuntimeModelConfig, fetchRuntimeModelConfig } from '#/lib/admin'
import type { ApplyModelConfigResult, RuntimeModelConfig } from '#/lib/admin'
import { resolveStudioManagementCapabilities } from '#/lib/studio-permissions'

import { getErrorMessage } from './-lib/error'

export const Route = createFileRoute('/model-config')({
  component: ModelConfigRoute,
})

type SectionName = keyof RuntimeModelConfig

type FormState = {
  vlm: { apiBase: string; apiKey: string; model: string; provider: string }
  rerank: {
    apiBase: string
    apiKey: string
    model: string
    provider: string
    threshold: string
    enabled: boolean
  }
  embedding: {
    apiBase: string
    apiKey: string
    model: string
    provider: string
    dimension: string
  }
  queryPlannerEnabled: boolean
  queryPlanner: {
    apiBase: string
    apiKey: string
    model: string
    provider: string
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function toFormState(config: RuntimeModelConfig): FormState {
  const vlm = asRecord(config.vlm)
  const rerank = asRecord(config.rerank)
  const embeddingDense = asRecord(asRecord(config.embedding).dense)
  const queryPlanner = config.query_planner
    ? asRecord(config.query_planner)
    : null

  return {
    vlm: {
      apiBase: asText(vlm.api_base),
      apiKey: asText(vlm.api_key),
      model: asText(vlm.model),
      provider: asText(vlm.provider),
    },
    rerank: {
      apiBase: asText(rerank.api_base),
      apiKey: asText(rerank.api_key),
      model: asText(rerank.model),
      provider: asText(rerank.provider),
      threshold:
        rerank.threshold === undefined || rerank.threshold === null
          ? ''
          : String(rerank.threshold),
      enabled: rerank.enabled !== false,
    },
    embedding: {
      apiBase: asText(embeddingDense.api_base),
      apiKey: asText(embeddingDense.api_key),
      model: asText(embeddingDense.model),
      provider: asText(embeddingDense.provider),
      dimension:
        embeddingDense.dimension === undefined ||
        embeddingDense.dimension === null
          ? ''
          : String(embeddingDense.dimension),
    },
    queryPlannerEnabled: Boolean(queryPlanner),
    queryPlanner: {
      apiBase: asText(queryPlanner?.api_base),
      apiKey: asText(queryPlanner?.api_key),
      model: asText(queryPlanner?.model),
      provider: asText(queryPlanner?.provider),
    },
  }
}

function sectionChanged(
  next: Record<string, unknown> | null,
  previous: Record<string, unknown> | null,
): boolean {
  const a = next ?? {}
  const b = previous ?? {}
  const keys = new Set([...Object.keys(a), ...Object.keys(b)])
  for (const key of keys) {
    if (JSON.stringify(a[key]) !== JSON.stringify(b[key])) {
      return true
    }
  }
  return false
}

function buildPatchFromForm(form: FormState, previous: RuntimeModelConfig) {
  const patch: Record<string, unknown> = {}

  const nextVlm: Record<string, unknown> = {
    ...asRecord(previous.vlm),
    api_base: form.vlm.apiBase,
    api_key: form.vlm.apiKey,
    model: form.vlm.model,
    provider: form.vlm.provider,
  }
  if (sectionChanged(nextVlm, previous.vlm)) {
    patch.vlm = nextVlm
  }

  const threshold = form.rerank.threshold.trim()
  const nextRerank: Record<string, unknown> = {
    ...asRecord(previous.rerank),
    api_base: form.rerank.apiBase,
    api_key: form.rerank.apiKey,
    model: form.rerank.model,
    provider: form.rerank.provider,
    enabled: form.rerank.enabled,
    ...(threshold ? { threshold: Number(threshold) } : {}),
  }
  if (sectionChanged(nextRerank, previous.rerank)) {
    patch.rerank = nextRerank
  }

  const dimension = form.embedding.dimension.trim()
  const nextEmbedding: Record<string, unknown> = {
    ...asRecord(previous.embedding),
    dense: {
      ...asRecord(asRecord(previous.embedding).dense),
      api_base: form.embedding.apiBase,
      api_key: form.embedding.apiKey,
      model: form.embedding.model,
      provider: form.embedding.provider,
      ...(dimension ? { dimension: Number(dimension) } : {}),
    },
  }
  if (sectionChanged(nextEmbedding, previous.embedding)) {
    patch.embedding = nextEmbedding
  }

  if (form.queryPlannerEnabled) {
    const nextQueryPlanner: Record<string, unknown> = {
      ...asRecord(previous.query_planner),
      api_base: form.queryPlanner.apiBase,
      api_key: form.queryPlanner.apiKey,
      model: form.queryPlanner.model,
      provider: form.queryPlanner.provider,
    }
    if (sectionChanged(nextQueryPlanner, previous.query_planner)) {
      patch.query_planner = nextQueryPlanner
    }
  } else if (previous.query_planner) {
    patch.query_planner = null
  }

  return patch
}

function parseJsonSection(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    throw new Error(message || 'invalid JSON')
  }
}

function ModelConfigRoute() {
  const { t } = useTranslation('settings')
  const queryClient = useQueryClient()
  const {
    connection,
    connectionRole,
    isConnectionRoleLoading,
    openConnectionSettings,
    serverMode,
  } = useAppConnection()

  const { canManageAccounts } = resolveStudioManagementCapabilities({
    hasControlCredential: Boolean(connection.adminApiKey.trim()),
    isRoleLoading: isConnectionRoleLoading,
    role: connectionRole,
    serverMode,
  })

  const adminConnection = React.useMemo(
    () => ({
      accountId: connection.accountId,
      apiKey: connection.adminApiKey,
      baseUrl: connection.baseUrl,
      userId: connection.userId,
    }),
    [
      connection.accountId,
      connection.adminApiKey,
      connection.baseUrl,
      connection.userId,
    ],
  )

  const [mode, setMode] = React.useState<'form' | 'json'>('form')
  const [jsonDraft, setJsonDraft] = React.useState('')
  const [form, setForm] = React.useState<FormState | null>(null)

  const configQuery = useQuery({
    enabled: canManageAccounts,
    queryFn: () => fetchRuntimeModelConfig(adminConnection),
    queryKey: ['admin', 'model-config', adminConnection],
  })

  const config = configQuery.data

  React.useEffect(() => {
    if (config && !form) {
      setForm(toFormState(config))
    }
  }, [config, form])

  React.useEffect(() => {
    if (config) {
      setJsonDraft(JSON.stringify(config, null, 2))
    }
  }, [config])

  const saveMutation = useMutation({
    mutationFn: async (): Promise<ApplyModelConfigResult> => {
      let patch: Partial<Record<SectionName, unknown>>
      if (mode === 'json') {
        const parsed = parseJsonSection(jsonDraft)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error(t('modelConfig.errors.jsonInvalid', { message: '' }))
        }
        patch = parsed as Partial<Record<SectionName, unknown>>
      } else {
        if (!config) {
          throw new Error('not loaded')
        }
        patch = buildPatchFromForm(form ?? toFormState(config), config)
      }
      return applyRuntimeModelConfig(adminConnection, patch)
    },
    onError: (error) => {
      toast.error(
        t('modelConfig.errors.saveFailed', {
          message: getErrorMessage(error),
        }),
      )
    },
    onSuccess: (result) => {
      queryClient.setQueryData(
        ['admin', 'model-config', adminConnection],
        result.config,
      )
      setForm(toFormState(result.config))
      setJsonDraft(JSON.stringify(result.config, null, 2))
      const restart = result.restart_required
      if (restart.length > 0) {
        toast.warning(
          t('modelConfig.restartBanner', { sections: restart.join(', ') }),
        )
      } else {
        toast.success(t('modelConfig.successTitle'), {
          description: t('modelConfig.savedDetail', {
            updated: result.updated.join(', '),
          }),
        })
      }
    },
  })

  function updateForm<TField extends keyof FormState>(
    key: TField,
    value: FormState[TField],
  ) {
    setForm((current) =>
      current
        ? {
            ...current,
            [key]: value,
          }
        : current,
    )
  }

  if (!canManageAccounts) {
    return (
      <div className="mx-auto w-full max-w-3xl px-4 py-10">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <TriangleAlertIcon className="size-5 text-amber-500" />
              {t('modelConfig.accessDeniedTitle')}
            </CardTitle>
            <CardDescription>
              {t('modelConfig.accessDeniedDescription')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={openConnectionSettings} variant="outline">
              {t('modelConfig.accessDeniedOpenConnection')}
            </Button>
          </CardContent>
        </Card>
      </div>
    )
  }

  if (configQuery.isPending) {
    return (
      <div className="flex items-center gap-2 px-4 py-10 text-muted-foreground">
        <LoaderCircleIcon className="size-4 animate-spin" />
        {t('modelConfig.loading')}
      </div>
    )
  }

  if (configQuery.isError || !config || !form) {
    return (
      <div className="px-4 py-10">
        <Card>
          <CardHeader>
            <CardTitle>{t('modelConfig.title')}</CardTitle>
            <CardDescription>
              {t('modelConfig.errors.loadFailed', {
                message: getErrorMessage(configQuery.error),
              })}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              onClick={() => void configQuery.refetch()}
              variant="outline"
            >
              <RotateCwIcon className="size-4" />
              {t('actions.refresh')}
            </Button>
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 px-4 py-6">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold">{t('modelConfig.title')}</h1>
          <p className="text-sm text-muted-foreground">
            {t('modelConfig.description')}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={() => setMode(mode === 'form' ? 'json' : 'form')}
            size="sm"
            variant="ghost"
          >
            {mode === 'form'
              ? t('modelConfig.jsonMode')
              : t('modelConfig.formMode')}
          </Button>
          <Button
            disabled={saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
            size="sm"
          >
            {saveMutation.isPending ? (
              <LoaderCircleIcon className="size-4 animate-spin" />
            ) : null}
            {saveMutation.isPending
              ? t('modelConfig.saving')
              : t('modelConfig.save')}
          </Button>
        </div>
      </div>

      {mode === 'form' ? (
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                {t('modelConfig.vlm.title')}
              </CardTitle>
              <CardDescription>
                {t('modelConfig.vlm.description')}
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 sm:grid-cols-2">
              <FieldGroup
                label={t('modelConfig.vlm.provider')}
                onChange={(value) =>
                  updateForm('vlm', { ...form.vlm, provider: value })
                }
                value={form.vlm.provider}
              />
              <FieldGroup
                label={t('modelConfig.vlm.model')}
                onChange={(value) =>
                  updateForm('vlm', { ...form.vlm, model: value })
                }
                value={form.vlm.model}
              />
              <FieldGroup
                hint={t('modelConfig.apiBaseHint')}
                label={t('modelConfig.vlm.apiBase')}
                onChange={(value) =>
                  updateForm('vlm', { ...form.vlm, apiBase: value })
                }
                value={form.vlm.apiBase}
              />
              <FieldGroup
                label={t('modelConfig.vlm.apiKey')}
                onChange={(value) =>
                  updateForm('vlm', { ...form.vlm, apiKey: value })
                }
                value={form.vlm.apiKey}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle className="text-base">
                    {t('modelConfig.rerank.title')}
                  </CardTitle>
                  <CardDescription>
                    {t('modelConfig.rerank.description')}
                  </CardDescription>
                </div>
                <div className="flex items-center gap-2">
                  <Label className="text-xs text-muted-foreground">
                    {t('modelConfig.rerank.enableToggle')}
                  </Label>
                  <Switch
                    checked={form.rerank.enabled}
                    onCheckedChange={(checked) =>
                      updateForm('rerank', { ...form.rerank, enabled: checked })
                    }
                  />
                </div>
              </div>
            </CardHeader>
            {form.rerank.enabled ? (
              <CardContent className="grid gap-3 sm:grid-cols-2">
                <FieldGroup
                  label={t('modelConfig.rerank.provider')}
                  onChange={(value) =>
                    updateForm('rerank', { ...form.rerank, provider: value })
                  }
                  value={form.rerank.provider}
                />
                <FieldGroup
                  label={t('modelConfig.rerank.model')}
                  onChange={(value) =>
                    updateForm('rerank', { ...form.rerank, model: value })
                  }
                  value={form.rerank.model}
                />
                <FieldGroup
                  hint={t('modelConfig.apiBaseHint')}
                  label={t('modelConfig.rerank.apiBase')}
                  onChange={(value) =>
                    updateForm('rerank', { ...form.rerank, apiBase: value })
                  }
                  value={form.rerank.apiBase}
                />
                <FieldGroup
                  label={t('modelConfig.rerank.apiKey')}
                  onChange={(value) =>
                    updateForm('rerank', { ...form.rerank, apiKey: value })
                  }
                  value={form.rerank.apiKey}
                />
                <FieldGroup
                  label={t('modelConfig.rerank.threshold')}
                  onChange={(value) =>
                    updateForm('rerank', { ...form.rerank, threshold: value })
                  }
                  value={form.rerank.threshold}
                />
              </CardContent>
            ) : null}
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                {t('modelConfig.embedding.title')}
              </CardTitle>
              <CardDescription>
                {t('modelConfig.embedding.description')}
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 sm:grid-cols-2">
              <FieldGroup
                label={t('modelConfig.embedding.provider')}
                onChange={(value) =>
                  updateForm('embedding', {
                    ...form.embedding,
                    provider: value,
                  })
                }
                value={form.embedding.provider}
              />
              <FieldGroup
                label={t('modelConfig.embedding.model')}
                onChange={(value) =>
                  updateForm('embedding', { ...form.embedding, model: value })
                }
                value={form.embedding.model}
              />
              <FieldGroup
                label={t('modelConfig.embedding.dimension')}
                onChange={(value) =>
                  updateForm('embedding', {
                    ...form.embedding,
                    dimension: value,
                  })
                }
                value={form.embedding.dimension}
              />
              <FieldGroup
                hint={t('modelConfig.apiBaseHint')}
                label={t('modelConfig.embedding.apiBase')}
                onChange={(value) =>
                  updateForm('embedding', { ...form.embedding, apiBase: value })
                }
                value={form.embedding.apiBase}
              />
              <FieldGroup
                label={t('modelConfig.embedding.apiKey')}
                onChange={(value) =>
                  updateForm('embedding', { ...form.embedding, apiKey: value })
                }
                value={form.embedding.apiKey}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle className="text-base">
                    {t('modelConfig.queryPlanner.title')}
                  </CardTitle>
                  <CardDescription>
                    {t('modelConfig.queryPlanner.description')}
                  </CardDescription>
                </div>
                <Switch
                  checked={form.queryPlannerEnabled}
                  onCheckedChange={(checked) =>
                    updateForm('queryPlannerEnabled', checked)
                  }
                />
              </div>
            </CardHeader>
            {form.queryPlannerEnabled ? (
              <CardContent className="grid gap-3 sm:grid-cols-2">
                <FieldGroup
                  label={t('modelConfig.vlm.provider')}
                  onChange={(value) =>
                    updateForm('queryPlanner', {
                      ...form.queryPlanner,
                      provider: value,
                    })
                  }
                  value={form.queryPlanner.provider}
                />
                <FieldGroup
                  label={t('modelConfig.vlm.model')}
                  onChange={(value) =>
                    updateForm('queryPlanner', {
                      ...form.queryPlanner,
                      model: value,
                    })
                  }
                  value={form.queryPlanner.model}
                />
                <FieldGroup
                  hint={t('modelConfig.apiBaseHint')}
                  label={t('modelConfig.vlm.apiBase')}
                  onChange={(value) =>
                    updateForm('queryPlanner', {
                      ...form.queryPlanner,
                      apiBase: value,
                    })
                  }
                  value={form.queryPlanner.apiBase}
                />
                <FieldGroup
                  label={t('modelConfig.vlm.apiKey')}
                  onChange={(value) =>
                    updateForm('queryPlanner', {
                      ...form.queryPlanner,
                      apiKey: value,
                    })
                  }
                  value={form.queryPlanner.apiKey}
                />
              </CardContent>
            ) : null}
          </Card>

          <p className="text-xs text-muted-foreground">
            {t('modelConfig.formModeHint')}
          </p>
        </div>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {t('modelConfig.jsonMode')}
            </CardTitle>
            <CardDescription>{t('modelConfig.formModeHint')}</CardDescription>
          </CardHeader>
          <CardContent>
            <Textarea
              className="min-h-[420px] font-mono text-xs"
              onChange={(event) => setJsonDraft(event.target.value)}
              spellCheck={false}
              value={jsonDraft}
            />
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function FieldGroup({
  hint,
  label,
  onChange,
  value,
}: {
  hint?: string
  label: string
  onChange: (value: string) => void
  value: string
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input onChange={(event) => onChange(event.target.value)} value={value} />
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}
