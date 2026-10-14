import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'

import {
  markdownSanitizeSchema,
  resolveImageReference,
} from './file-preview'

const FIGURE = `<figure>
    <img src="kb://images/844935734363811" vikingsrc="viking://resources/raw/menus/doc/images/844935734363811.jpg"/>
    <figcaption>
    caption text
    </figcaption>
</figure>`

const VIKINGSRC_ONLY = `<img vikingsrc="viking://resources/doc/images/a.jpg"/>`

const SRC_VIKING = `<img src="viking://resources/doc/images/a.jpg"/>`

interface SeenImage {
  src?: string
  vikingsrc?: string
}

/** Render through the same pipeline the preview uses, reporting img props. */
function renderImages(source: string): SeenImage[] {
  const seen: SeenImage[] = []
  renderToStaticMarkup(
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, markdownSanitizeSchema]]}
      urlTransform={(url) => url}
      components={{
        img: ({ src, node }) => {
          seen.push({
            src: src ? String(src) : undefined,
            vikingsrc:
              typeof node?.properties?.vikingsrc === 'string'
                ? node.properties.vikingsrc
                : undefined,
          })
          return <img src={src ? String(src) : undefined} />
        },
      }}
    >
      {source}
    </ReactMarkdown>,
  )
  return seen
}

describe('markdownSanitizeSchema', () => {
  it('keeps the vikingsrc pointer on a figure image', () => {
    const images = renderImages(FIGURE)
    expect(images).toHaveLength(1)
    expect(images[0].vikingsrc).toBe(
      'viking://resources/raw/menus/doc/images/844935734363811.jpg',
    )
  })

  it('keeps vikingsrc when it is the only image attribute', () => {
    const images = renderImages(VIKINGSRC_ONLY)
    expect(images[0].vikingsrc).toBe('viking://resources/doc/images/a.jpg')
  })

  it('keeps a viking:// src reference', () => {
    const images = renderImages(SRC_VIKING)
    expect(images[0].src).toBe('viking://resources/doc/images/a.jpg')
  })
})

describe('resolveImageReference', () => {
  it('prefers the vikingsrc pointer over the authoring src', () => {
    expect(
      resolveImageReference(
        'viking://resources/doc/images/a.jpg',
        'kb://images/123',
      ),
    ).toBe('viking://resources/doc/images/a.jpg')
  })

  it('falls back to src when no pointer is present', () => {
    expect(resolveImageReference(undefined, 'images/a.jpg')).toBe('images/a.jpg')
  })

  it('ignores a non-viking pointer and keeps src', () => {
    expect(resolveImageReference('kb://images/123', 'images/a.jpg')).toBe(
      'images/a.jpg',
    )
  })
})
