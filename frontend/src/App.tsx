import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ResultTable } from './ResultTable'
import { streamAsk } from './sse'
import type { Stage, TableData } from './sse'

const STAGE_LABEL: Record<Stage, string> = {
  understanding: 'Understanding your question…',
  running: 'Running the query…',
}

export function App() {
  const [question, setQuestion] = useState('')
  const [asked, setAsked] = useState('')
  const [stage, setStage] = useState<Stage | null>(null)
  const [table, setTable] = useState<TableData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const busy = useRef(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const q = question.trim()
    if (!q || busy.current) return
    busy.current = true
    setAsked(q)
    setTable(null)
    setError(null)
    setStage('understanding')
    try {
      for await (const event of streamAsk(q)) {
        if (event.name === 'stage') setStage(event.data.stage)
        else if (event.name === 'table') setTable(event.data)
        else if (event.name === 'error') setError(event.data.message)
      }
    } finally {
      setStage(null)
      busy.current = false
    }
  }

  return (
    <main>
      <h1>AskDB</h1>
      <form onSubmit={submit}>
        <label htmlFor="question">Ask a question about your data</label>
        <input
          id="question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          maxLength={2000}
          autoComplete="off"
        />
        <button type="submit" disabled={stage !== null || question.trim() === ''}>
          Ask
        </button>
      </form>
      {asked && <p className="asked">{asked}</p>}
      {stage && (
        <p role="status" className="stage">
          {STAGE_LABEL[stage]}
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {table && <ResultTable table={table} />}
    </main>
  )
}
