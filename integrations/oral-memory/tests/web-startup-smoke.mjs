/** Start the retained patient Web application and serve its real frontend without a model request. */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../../', import.meta.url))
const directory = await mkdtemp(resolve(tmpdir(), 'meddental-web-'))
const child = spawn(process.execPath, ['--import', 'tsx/esm', 'apps/cli/src/bin.ts', 'web',
  '--patch', 'apps/cli/config/examples/mcp-memory/oral-leukoplakia.cordis.yml',
  '--patch', 'apps/cli/config/examples/mcp-memory/oral-patient.cordis.yml',
  '--host', '127.0.0.1', '--port', '0', '--no-open'], {
  cwd: root, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'],
  env: { ...process.env, DSH_HOME: resolve(directory, 'home'), ORAL_MEMORY_ROOT: root,
    ORAL_MEMORY_DB: resolve(directory, 'memory.sqlite3'), ORAL_MEMORY_PATIENT_ID: 'synthetic-web-smoke',
    ORAL_MEMORY_ENCOUNTER_ID: 'synthetic-visit', ORAL_MEMORY_ACTOR_ROLE: 'patient', DSH_TELEMETRY_DISABLED: '1' },
})
const closed = once(child, 'close')
let output = ''
let timeout
try {
  const url = await new Promise((accept, reject) => {
    timeout = setTimeout(() => reject(new Error(`Patient Web startup timed out:\n${output}`)), 30000)
    const read = chunk => {
      output = (output + chunk.toString()).slice(-65536)
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+(?:\/\S*)?/)
      if (match && match[0].includes('?token=')) accept(match[0])
    }
    child.stdout.on('data', read)
    child.stderr.on('data', read)
    child.once('error', reject)
    child.once('exit', code => reject(new Error(`Patient Web exited with ${code}:\n${output}`)))
  })
  clearTimeout(timeout)
  const login = await fetch(url, { redirect: 'manual', signal: AbortSignal.timeout(10000) })
  const cookie = login.headers.getSetCookie().map(value => value.split(';')[0]).join('; ')
  assert.ok(cookie, 'Local entry token must produce a browser session cookie')
  const response = await fetch(new URL('/', url), { headers: { cookie }, signal: AbortSignal.timeout(10000) })
  assert.equal(response.status, 200)
  const html = await response.text()
  assert.ok(html.includes('__DSH_BOOT__'), 'Served page must carry the real Harness boot manifest')
  assert.ok(html.includes('dsh-client-ui-conversation'), 'Patient conversation UI must be available')
  for (const removed of ['dsh-client-product-analytics', 'dsh-client-ui-plugin-market', 'dsh-client-ui-open-in-app', 'dsh-client-ui-entry-customization']) {
    assert.ok(!html.includes(removed), `Removed UI must not be served: ${removed}`)
  }
  console.log('Patient Web startup and real frontend response: passed')
} finally {
  clearTimeout(timeout)
  const stop = signal => {
    if (child.exitCode !== null || child.signalCode !== null) return
    try { if (process.platform === 'win32') child.kill(signal); else process.kill(-child.pid, signal) } catch (error) { if (error.code !== 'ESRCH') throw error }
  }
  stop('SIGTERM')
  const force = setTimeout(() => stop('SIGKILL'), 5000)
  await closed
  clearTimeout(force)
  await rm(directory, { recursive: true, force: true })
}
