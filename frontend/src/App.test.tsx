import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { App } from './App'

const block = (name: string, data: unknown) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`

function sse(...blocks: string[]) {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      blocks.forEach((b) => controller.enqueue(encoder.encode(b)))
      controller.close()
    },
  })
  return new Response(body, { status: 200 })
}

afterEach(() => vi.unstubAllGlobals())

async function ask(response: Response, question = 'plans by price') {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
  const user = userEvent.setup()
  render(<App />)
  await user.type(screen.getByLabelText(/ask a question/i), question)
  await user.click(screen.getByRole('button', { name: 'Ask' }))
}

it('shows the result table with NULLs made visible', async () => {
  await ask(
    sse(
      block('stage', { stage: 'understanding' }),
      block('stage', { stage: 'running' }),
      block('table', {
        columns: ['plan', 'end_date'],
        rows: [
          ['Starter', null],
          ['Business', '2025-01-01'],
        ],
        row_count: 2,
        truncated: false,
      }),
      block('done', {}),
    ),
  )
  expect(await screen.findByRole('columnheader', { name: 'plan' })).toBeInTheDocument()
  expect(screen.getByRole('cell', { name: 'Starter' })).toBeInTheDocument()
  expect(screen.getByText('NULL')).toBeInTheDocument()
  expect(screen.getByText('plans by price')).toBeInTheDocument()
  await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
})

it('shows the stage while the stream is open, then clears it', async () => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => (release = resolve))
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encoder.encode(block('stage', { stage: 'running' })))
      await gate
      controller.enqueue(encoder.encode(block('done', {})))
      controller.close()
    },
  })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 200 })))
  const user = userEvent.setup()
  render(<App />)
  await user.type(screen.getByLabelText(/ask a question/i), 'q')
  await user.click(screen.getByRole('button', { name: 'Ask' }))
  expect(await screen.findByText('Running the query…')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
  release()
  await waitFor(() => expect(screen.queryByText('Running the query…')).not.toBeInTheDocument())
})

it('shows server errors in an alert and no table', async () => {
  await ask(
    sse(
      block('stage', { stage: 'understanding' }),
      block('error', { kind: 'guard', message: 'Only SELECT queries are allowed.' }),
    ),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent('Only SELECT queries are allowed.')
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
})

it('shows a banner when the result was truncated', async () => {
  await ask(
    sse(
      block('table', { columns: ['n'], rows: [[1]], row_count: 1, truncated: true }),
      block('done', {}),
    ),
  )
  expect(await screen.findByText(/more exist/)).toBeInTheDocument()
})

it('does not submit an empty question', async () => {
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
  expect(fetchMock).not.toHaveBeenCalled()
})

it('shows the summary above the table', async () => {
  await ask(
    sse(
      block('table', { columns: ['n'], rows: [[4]], row_count: 1, truncated: false }),
      block('stage', { stage: 'summarizing' }),
      block('summary', { text: 'There are 4 plans.', note: null, verified: true }),
      block('done', {}),
    ),
  )
  const summary = await screen.findByRole('region', { name: 'Summary' })
  expect(summary).toHaveTextContent('There are 4 plans.')
  expect(screen.queryByRole('note')).not.toBeInTheDocument()
  const table = screen.getByRole('table')
  expect(summary.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
})

it('explains a fallback summary with its note', async () => {
  await ask(
    sse(
      block('table', { columns: ['n'], rows: [[4]], row_count: 1, truncated: false }),
      block('summary', {
        text: 'The result is 4 (n).',
        note: 'The generated summary quoted figures that could not be checked against the data.',
        verified: false,
      }),
      block('done', {}),
    ),
  )
  expect(await screen.findByRole('note')).toHaveTextContent(/could not be checked/)
  expect(screen.getByText('The result is 4 (n).')).toBeInTheDocument()
})

it('shows the summarizing stage', async () => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => (release = resolve))
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encoder.encode(block('stage', { stage: 'summarizing' })))
      await gate
      controller.close()
    },
  })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 200 })))
  const user = userEvent.setup()
  render(<App />)
  await user.type(screen.getByLabelText(/ask a question/i), 'q')
  await user.click(screen.getByRole('button', { name: 'Ask' }))
  expect(await screen.findByText('Writing a summary…')).toBeInTheDocument()
  release()
})
