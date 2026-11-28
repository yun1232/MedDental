/** One SDK-owned subprocess per case; no replacement model loop or Web service. */
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { DeepSeekHarness } from '../../packages/sdk/client/src/index.ts'
import yaml from 'js-yaml'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
process.env.ORAL_MEMORY_ROOT = root
// Preset declarations validate literal plugin names before mounting. Resolve the
// trusted repository expressions before handing these overlays to the SDK CLI.
const js = new yaml.Type('tag:yaml.org,2002:js', { kind: 'scalar',
  construct: value => Function('process', `return (${value})`)(process) })
const schema = yaml.DEFAULT_SCHEMA.extend([js])
function materialize(relative, name, filter = rows => rows) {
  const destination = resolve(dirname(process.env.ORAL_BATCH_CASE_FILE), name)
  const rows = filter(yaml.load(readFileSync(resolve(root, relative), 'utf8'), { schema }))
  writeFileSync(destination, JSON.stringify(rows), { mode: 0o600 })
  return destination
}
const medicalPatch = materialize('apps/cli/config/examples/mcp-memory/oral-patient.cordis.yml',
  'medical-presets.cordis.yml', rows => rows.filter(row => row.id !== 'agent-preset-registry'))
const batchPatch = materialize('integrations/oral-memory/batch.cordis.yml', 'batch-presets.cordis.yml')
// Host-plane plugin names must be literal strings for the CLI's requirement audit.
// Resolve the local plugin here rather than passing a deferred YAML name expression.
const hostPatch = resolve(dirname(process.env.ORAL_BATCH_CASE_FILE), 'batch-host.cordis.yml')
writeFileSync(hostPatch, JSON.stringify([{ insert: [{ id: 'oral-batch-driver',
  name: pathToFileURL(resolve(root, 'integrations/oral-memory/batch-driver.mjs')).href }] }]), { mode: 0o600 })
const harness = new DeepSeekHarness({
  dshBin: resolve(root, 'integrations/oral-memory/batch-runtime.mjs'),
  profile: 'sdk',
  processCwd: root, cwd: root, dshHome: process.env.ORAL_BATCH_HARNESS_HOME,
  env: { ...process.env, ORAL_MEMORY_ROOT: root },
  patches: [resolve(root, 'apps/cli/src/sdk-source.cordis.patch.yml'),
    medicalPatch, batchPatch,
    hostPatch,
    ...(process.env.ORAL_BATCH_PROVIDER_PATCH ? [resolve(root, process.env.ORAL_BATCH_PROVIDER_PATCH)] : [])],
  provider: process.env.ORAL_BATCH_PROVIDER, model: process.env.ORAL_BATCH_MODEL,
  maxTokens: 4000, initializeTimeoutMs: 60000, requestTimeoutMs: 3600000,
})
try {
  const result = await harness.run('调用 batch_run 工具完成主机已指定的一个离线病例。不要自己诊断，不要调用其他工具，完成后简短结束。')
  if (result.finishReason !== 'completed' || !existsSync(process.env.ORAL_BATCH_RESULT_FILE)) {
    console.error(JSON.stringify({ finishReason: result.finishReason, finalResponse: result.finalResponse,
      failures: result.events.filter(e => e.type === 'turn/end' || e.type === 'step/end' || e.type === 'tool/result') }))
    throw new Error('The batch tool did not finish; inspect the private worker log')
  }
  const episode = JSON.parse(readFileSync(process.env.ORAL_BATCH_RESULT_FILE, 'utf8'))
  console.log(JSON.stringify({ case_id: episode.case_id, status: episode.status }))
} catch (error) {
  console.error(error instanceof Error ? error.message : 'Batch worker failed')
  process.exitCode = 1
} finally { await harness.close() }
