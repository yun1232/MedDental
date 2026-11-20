/** Bounded CSV/TSV parsing that preserves literal values, including formula-looking strings. */
import Papa from 'papaparse'
import type { Config } from '../../config.ts'

/** Parsed rows and whether the preview reached a display limit. */
export interface CsvPreview { readonly rows: readonly (readonly string[])[]; readonly truncated: boolean }

/** Decode UTF-8 or BOM-marked UTF-16 and stop before the configured allocation bounds. */
export function parseCsv(bytes: Uint8Array, path: string, limits: Config['csv']): CsvPreview {
  if (bytes.byteLength > limits.maxBytes) throw new Error('CSV file exceeds the preview byte limit.')
  const encoding = bytes[0] === 0xff && bytes[1] === 0xfe ? 'utf-16le'
    : bytes[0] === 0xfe && bytes[1] === 0xff ? 'utf-16be' : 'utf-8'
  const text = new TextDecoder(encoding, { fatal: true }).decode(bytes)
  const rows: string[][] = []
  let cells = 0
  let truncated = false
  let invalid = false
  Papa.parse<string[]>(text, {
    delimiter: /\.tsv$/i.test(path) ? '\t' : ',',
    dynamicTyping: false, skipEmptyLines: 'greedy',
    step(result, parser) {
      if (result.errors.length) { invalid = true; parser.abort(); return }
      if (rows.length >= limits.maxRows || cells + result.data.length > limits.maxCells) {
        truncated = true; parser.abort(); return
      }
      rows.push(result.data)
      cells += result.data.length
    },
  })
  if (invalid) throw new Error('Invalid CSV/TSV data.')
  return { rows, truncated }
}
