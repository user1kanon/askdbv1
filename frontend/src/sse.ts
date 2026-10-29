export type Stage = 'understanding' | 'running' | 'summarizing'

export interface Interpretation {
  sql: string
  assumptions: string[]
  filters: string[]
  method_steps: string[]
  confidence: 'high' | 'medium' | 'low'
}

export interface TableData {
  columns: string[]
  rows: (string | number | null)[][]
  row_count: number
  truncated: boolean
}

export interface SummaryData {
  text: string
  note: string | null
  verified: boolean
}

export type AskEvent =
  | { name: 'stage'; data: { stage: Stage } }
  | { name: 'interpretation'; data: Interpretation }
  | { name: 'table'; data: TableData }
  | { name: 'summary'; data: SummaryData }
  | { name: 'error'; data: { kind: string; message: string } }
  | { name: 'done'; data: Record<string, never> }

/** Incremental Server-Sent Events parser: feed it text chunks, get back complete events. */
export class SSEParser {
  private buffer = ''

  push(chunk: string): AskEvent[] {
    this.buffer += chunk.replace(/\r\n/g, '\n')
    const events: AskEvent[] = []
    let end = this.buffer.indexOf('\n\n')
    while (end !== -1) {
      const event = parseBlock(this.buffer.slice(0, end))
      if (event) events.push(event)
      this.buffer = this.buffer.slice(end + 2)
      end = this.buffer.indexOf('\n\n')
    }
    return events
  }
}

function parseBlock(block: string): AskEvent | null {
  let name = ''
  const data: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) name = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  }
  if (!name || data.length === 0) return null
  return { name, data: JSON.parse(data.join('\n')) } as AskEvent
}

function failure(message: string): AskEvent {
  return { name: 'error', data: { kind: 'request', message } }
}

/** POST a question and yield the events of the SSE response as they arrive. */
export async function* streamAsk(question: string, signal?: AbortSignal): AsyncGenerator<AskEvent> {
  let response: Response
  try {
    response = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
      signal,
    })
  } catch {
    yield failure('Could not reach the AskDB server. Is it running?')
    return
  }

  if (!response.ok || !response.body) {
    yield failure(await describeHttpError(response))
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SSEParser()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    yield* parser.push(decoder.decode(value, { stream: true }))
  }
}

async function describeHttpError(response: Response): Promise<string> {
  try {
    const body = await response.json()
    const issues = body?.detail?.issues
    if (Array.isArray(issues) && issues.length > 0) return issues.join(' ')
    if (response.status === 422) return 'Please type a question (up to 2,000 characters).'
  } catch {
    // fall through to the generic message
  }
  return `The server returned an error (${response.status}).`
}
