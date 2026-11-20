/** Host configuration supplies bounded CSV preview settings to browser pages. */
import { Context } from '@deepseek-ai/cordis'
import type { IndexInjection, WebServer } from '@deepseek-ai/dsh-host-webserver'
import { expect, it, onTestFinished } from 'vitest'
import { Config } from '../src/config.ts'
import * as host from '../src/index.ts'

/** The Peer an index render speaks for; this listener reads no field of it. */
const PAGE_PEER = { principal: { kind: 'owner' } } as Parameters<WebServer['renderIndex']>[1]

it('exposes bounded CSV/TSV limits on the Host entry', () => {
  expect(host.Config).toBe(Config)
  expect(Config({})).toEqual({ csv: { maxBytes: 4 * 1024 * 1024, maxCells: 50_000, maxRows: 1_000 } })
  for (const key of ['maxBytes', 'maxCells', 'maxRows']) expect(() => Config({ csv: { [key]: 0 } })).toThrow()
})

it('embeds YAML cache settings in browser pages and withdraws them on disposal', async () => {
  const ctx = new Context()
  onTestFinished(async () => { await ctx.fiber.dispose() })
  const config = Config({ csv: { maxRows: 2 } })
  const fiber = ctx.plugin(host, config)
  await fiber.await()
  const rows: IndexInjection[] = []
  ctx.emit('webserver/index-inject', rows, PAGE_PEER)
  expect(rows).toEqual([{ kind: 'global', name: '__DSH_DOCUMENT_PREVIEW_CONFIG__', value: config }])
  await fiber.dispose()
  const after: IndexInjection[] = []
  ctx.emit('webserver/index-inject', after, PAGE_PEER)
  expect(after).toEqual([])
})
