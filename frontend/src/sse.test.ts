import { afterEach, describe, expect, it, vi } from 'vitest'
import { SSEParser, streamAsk } from './sse'
import type { AskEvent } from './sse'

const block = (name: string, data: unknown) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`

function streamResponse(chunks: string[], init: ResponseInit = { status: 200 }) {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((c) => controller.enqueue(encoder.encode(c)))
      controller.close()
    },
  })
  return new Response(body, init)
}

async function collect(question = 'q'): Promise<AskEvent[]> {
  const events: AskEvent[] = []
  for await (const e of streamAsk(question)) events.push(e)
  return events
}

afterEach(() => vi.unstubAllGlobals())

describe('SSEParser', () => {
  it('parses several events from one chunk', () => {
    const events = new SSEParser().push(block('stage', { stage: 'understanding' }) + block('done', {}))
    expect(events.map((e) => e.name)).toEqual(['stage', 'done'])
  })

  it('waits for the rest of an event split across chunks', () => {
    const parser = new SSEParser()
    const text = block('table', { columns: ['a'], rows: [[1]], row_count: 1, truncated: false })
    expect(parser.push(text.slice(0, 20))).toEqual([])
    expect(parser.push(text.slice(20))).toHaveLength(1)
  })

  it('handles CRLF line endings and ignores comment-only blocks', () => {
    const events = new SSEParser().push(': keep-alive\n\nevent: done\r\ndata: {}\r\n\r\n')
    expect(events).toEqual([{ name: 'done', data: {} }])
  })
})

describe('streamAsk', () => {
  it('posts the question and yields streamed events', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        streamResponse([block('stage', { stage: 'running' }), block('done', {})]),
      )
    vi.stubGlobal('fetch', fetchMock)
    const events = await collect('how many?')
    expect(events.map((e) => e.name)).toEqual(['stage', 'done'])
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/ask')
    expect(JSON.parse(init.body)).toEqual({ question: 'how many?' })
  })

  it('turns a 503 with issues into an error event', async () => {
    const body = JSON.stringify({ detail: { issues: ['ANTHROPIC_API_KEY is not set.'] } })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 503 })))
    expect(await collect()).toEqual([
      { name: 'error', data: { kind: 'request', message: 'ANTHROPIC_API_KEY is not set.' } },
    ])
  })

  it('reports an unreachable server', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const [event] = await collect()
    expect(event.name).toBe('error')
    expect((event.data as { message: string }).message).toMatch(/Could not reach/)
  })
})
