// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, waitFor } from '@testing-library/react'
import type { PropsWithChildren } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { VikingFsEntry } from '../-types/viking-fm'
import { FilePreview } from './file-preview'
import { getContentDownload } from '#/lib/ov-client'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

vi.mock('#/gen/ov-client/client.gen', () => ({
  client: {
    buildUrl: ({ query }: { query: { uri: string } }) =>
      `/api/v1/content/download?uri=${encodeURIComponent(query.uri)}`,
  },
}))

vi.mock('#/lib/ov-client', () => ({
  getContentDownload: vi.fn(),
  ovClient: { getOptions: () => ({ baseUrl: '' }) },
}))

const markdown = [
  '# Heading',
  '',
  '<div>rendered</div>',
  '',
  '<table><tbody><tr><th colspan="2">head</th></tr><tr><td rowspan="2">cell</td><td>a</td></tr><tr><td>b</td></tr></tbody></table>',
  '',
  '<script>window.__pwned = true</script>',
].join('\n')

const figureMarkdown = [
  '<figure>',
  '    <img src="kb://images/844935734363811" vikingsrc="viking://resources/doc/images/844935734363811.jpg"/>',
  '    <figcaption>',
  '    caption',
  '    </figcaption>',
  '</figure>',
].join('\n')

const previewContent = { markdown }

vi.mock('../-hooks/viking-fm', () => ({
  useInvalidateVikingFs: () => ({
    invalidateList: vi.fn(),
    invalidatePreview: vi.fn(),
    invalidateTree: vi.fn(),
  }),
  useVikingFilePreview: () => ({
    canLoadContent: false,
    isContentLoaded: true,
    isFetching: false,
    isLoading: false,
    preview: {
      content: previewContent.markdown,
      fileType: 'markdown',
      shouldAutoRead: true,
    },
    refetch: vi.fn(),
  }),
  useVikingFsStat: () => ({
    data: undefined,
    isLoading: false,
  }),
}))

const file: VikingFsEntry = {
  abstract: '',
  isDir: false,
  modTime: '2026-08-04 12:00',
  modTimestamp: null,
  name: 'index.md',
  overview: '',
  size: '24 B',
  sizeBytes: 24,
  uri: 'viking://resources/wiki/index.md',
}

function renderPreview() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return render(
    <FilePreview
      file={file}
      onClose={vi.fn()}
      onNavigate={vi.fn()}
      showCloseButton={false}
    />,
    { wrapper },
  )
}

describe('FilePreview Markdown raw HTML', () => {
  beforeEach(() => {
    previewContent.markdown = markdown
    vi.mocked(getContentDownload).mockReset()
  })

  it('renders raw HTML elements instead of escaping them', () => {
    const { container } = renderPreview()

    const rendered = Array.from(container.querySelectorAll('div')).find(
      (el) => el.textContent === 'rendered',
    )
    expect(rendered).toBeTruthy()
    expect(container.textContent).not.toContain('<div>')
  })

  it('renders raw HTML tables through the shared table components', () => {
    const { container } = renderPreview()

    const head = container.querySelector('table th')
    const spanned = container.querySelector('table td')
    expect(head?.textContent).toBe('head')
    expect(head?.getAttribute('colspan')).toBe('2')
    expect(spanned?.textContent).toBe('cell')
    expect(spanned?.getAttribute('rowspan')).toBe('2')
  })

  it('strips script tags', () => {
    const { container } = renderPreview()

    expect(container.querySelector('script')).toBeNull()
  })

  it('downloads a figure image through its vikingsrc pointer', async () => {
    previewContent.markdown = figureMarkdown
    vi.mocked(getContentDownload).mockResolvedValue({
      data: new Blob([new Uint8Array([1, 2, 3])], { type: 'image/jpeg' }),
    } as never)
    // jsdom does not implement object URLs; the preview turns the blob into one.
    const objectUrls = URL as typeof URL & {
      createObjectURL?: (blob: Blob) => string
      revokeObjectURL?: (url: string) => void
    }
    const createObjectURL = vi.fn(() => 'blob:preview')
    objectUrls.createObjectURL = createObjectURL
    objectUrls.revokeObjectURL = vi.fn()

    const { container } = renderPreview()

    await waitFor(() => {
      const image = container.querySelector('img')
      expect(image?.getAttribute('src')).toBe('blob:preview')
    })
    expect(getContentDownload).toHaveBeenCalledWith(
      expect.objectContaining({
        query: {
          uri: 'viking://resources/doc/images/844935734363811.jpg',
        },
      }),
    )
  })
})
