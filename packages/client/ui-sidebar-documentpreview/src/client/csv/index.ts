/** CSV/TSV registration over ordinary authorized bytes, independent of Office tooling. */
import type { Context } from '@deepseek-ai/cordis'
import type {} from '../index.ts'
import type { Config } from '../../config.ts'
import { CsvBody } from './CsvBody.tsx'

/** Register metadata and the body; both registrations follow the plugin lifetime. */
export function apply(ctx: Context, limits: Config['csv']): void {
  const id = '@deepseek-ai/dsh-client-ui-sidebar-documentpreview/csv'
  ctx.effect(() => ctx.documentPreviews.register({
    id, extensions: ['csv', 'tsv'], priority: 'builtin', title: () => 'CSV / TSV',
    loading: 'bytes-complete', wrap: false,
  }))
  ctx.effect(() => ctx.slots.inject('sidebar.right.tab.document', () => ctx.slots.register({
    name: 'sidebar.right.tab.document', key: id, inject: () => ({ limits }),
  }, CsvBody)))
}
