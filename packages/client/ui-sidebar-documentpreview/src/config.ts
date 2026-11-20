/** Bounds for read-only delimited data previews. */
import z from '@deepseek-ai/schemastery'

/** Transient CSV/TSV input and display limits. */
export interface Config {
  csv: {
    maxBytes: number
    maxCells: number
    maxRows: number
  }
}

/** Parse only bounded delimited data; never load Office conversion engines. */
export const Config: z<{ csv?: Partial<Config['csv']> }, Config> = z.object({
  csv: z.object({
    maxBytes: z.natural().min(1).max(Number.MAX_SAFE_INTEGER).default(4 * 1024 * 1024),
    maxCells: z.natural().min(1).max(Number.MAX_SAFE_INTEGER).default(50_000),
    maxRows: z.natural().min(1).max(Number.MAX_SAFE_INTEGER).default(1_000),
  }),
})
