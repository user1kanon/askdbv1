import type { TableData } from './sse'

export function ResultTable({ table }: { table: TableData }) {
  if (table.row_count === 0) return <p>No rows.</p>
  return (
    <div className="table-wrap">
      {table.truncated && (
        <p role="status" className="banner">
          Showing the first {table.row_count.toLocaleString()} rows; more exist.
        </p>
      )}
      <table>
        <thead>
          <tr>
            {table.columns.map((c, i) => (
              <th key={i} scope="col">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, r) => (
            <tr key={r}>
              {row.map((cell, c) => (
                <td key={c}>{cell === null ? <span className="null">NULL</span> : String(cell)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
