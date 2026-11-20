/** Read-only data table. React renders all fields as text without evaluating formulas or HTML. */
import { useMemo, type ReactNode } from 'react'
import type { DocumentPreviewProps } from '../document/contract.ts'
import type { Config } from '../../config.ts'
import { hostFileOf } from '../rpc.ts'
import { parseCsv } from './parse.ts'
import css from './CsvBody.module.css'

/** Show bounded rows from the authorized complete file bytes. */
export function CsvBody(props: DocumentPreviewProps & { readonly limits: Config['csv'] }): ReactNode {
  const data = props.content.kind === 'bytes' ? props.content.data : undefined
  const path = hostFileOf(props.resourceAddress).path
  const preview = useMemo(() => {
    try { return data === undefined ? undefined : parseCsv(data, path, props.limits) }
    catch (error) { return error instanceof Error ? error.message : 'Unable to read CSV/TSV.' }
  }, [data, path, props.limits])
  if (preview === undefined) return null
  if (typeof preview === 'string') return <p role="alert">{preview}</p>
  return <section className={css.body} data-csv-preview aria-label="CSV / TSV">
    {preview.truncated && <p role="note">仅预览部分数据 / Preview limit reached.</p>}
    <table><tbody>{preview.rows.map((row, i) => <tr key={i}>
      {row.map((value, j) => <td key={j}>{value}</td>)}
    </tr>)}</tbody></table>
  </section>
}
