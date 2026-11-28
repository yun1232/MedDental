/** Bootstrap the retained dsh source CLI with the same loader as `pnpm dsh`. */
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { register } from 'tsx/esm/api'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
register({ tsconfig: resolve(root, 'apps/cli/tsconfig.json') })
const { runCli } = await import('../../apps/cli/src/bin.ts')
await runCli()
