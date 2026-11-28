/** Cross-platform one-command batch launcher; credentials stay in the child environment. */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const args = process.argv.slice(2)
const at = args.indexOf('--env-file')
const inline = args.find(a => a.startsWith('--env-file='))
const filename = inline?.slice(11) ?? (at >= 0 ? args[at + 1] : '.env.oral')
if (!filename || filename.startsWith('--')) throw new Error('--env-file needs a path')
if (inline) args.splice(args.indexOf(inline), 1)
else if (at >= 0) args.splice(at, 2)
const file = resolve(root, filename)
if (existsSync(file)) process.loadEnvFile(file)
else if (filename !== '.env.oral') throw new Error('The selected environment file does not exist')
const python = process.env.ORAL_MEMORY_PYTHON?.trim() || (process.platform === 'win32' ? 'python' : 'python3')
const child = spawn(python, ['integrations/oral-memory/batch.py', ...args], { cwd: root, env: process.env, stdio: 'inherit', shell: false })
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal))
child.once('error', error => { console.error(error.message); process.exitCode = 1 })
child.once('exit', (code, signal) => { process.exitCode = code ?? (signal === 'SIGINT' ? 130 : 143) })
