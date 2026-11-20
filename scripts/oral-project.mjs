/** Launch the oral workflow through the existing dsh CLI and doctor service. */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { parseArgs } from 'node:util'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

/** Build a shell-free command; deployment identity comes from arguments or environment. */
export function launchPlan(args, environment = process.env) {
  const { values, positionals } = parseArgs({ args, allowPositionals: true, options: {
    patient: { type: 'string' }, encounter: { type: 'string' }, db: { type: 'string' },
    port: { type: 'string' }, 'env-file': { type: 'string' }, 'dry-run': { type: 'boolean' },
  } })
  const [action = 'check', ...rest] = positionals
  if (rest.length || !['patient', 'doctor', 'check', 'test'].includes(action)) {
    throw new Error('Usage: oral-project.mjs patient|doctor|check|test [--patient ID] [--encounter ID] [--db PATH] [--port PORT]')
  }
  const env = { ...environment }
  env.ORAL_MEMORY_ROOT = root
  env.ORAL_MEMORY_PATIENT_ID = (values.patient ?? env.ORAL_MEMORY_PATIENT_ID ?? '').trim()
  env.ORAL_MEMORY_ENCOUNTER_ID = (values.encounter ?? env.ORAL_MEMORY_ENCOUNTER_ID ?? 'current').trim()
  const database = (values.db ?? env.ORAL_MEMORY_DB ?? '').trim()
  env.ORAL_MEMORY_DB = resolve(root, database || resolve(homedir(), '.dsh/oral-memory/memory.sqlite3'))
  const python = env.ORAL_MEMORY_PYTHON?.trim() || 'python3'
  if (action === 'patient' || action === 'doctor') {
    if (!env.ORAL_MEMORY_PATIENT_ID || !env.ORAL_MEMORY_ENCOUNTER_ID) {
      throw new Error('Set ORAL_MEMORY_PATIENT_ID and a nonempty encounter ID, or pass --patient and --encounter.')
    }
  }
  if (action === 'patient') {
    env.ORAL_MEMORY_ACTOR_ROLE = 'patient'
    const patientDirectory = createHash('sha256').update(env.ORAL_MEMORY_PATIENT_ID).digest('hex').slice(0, 24)
    env.DSH_HOME = resolve(root, env.ORAL_HARNESS_HOME?.trim() || resolve(homedir(), '.dsh-oral'), patientDirectory)
    delete env.ORAL_DOCTOR_TOKEN
    return { action, command: process.execPath, args: ['--import', 'tsx/esm', 'apps/cli/src/bin.ts',
      'web', '--patch', 'apps/cli/config/examples/mcp-memory/oral-leukoplakia.cordis.yml',
      '--patch', 'apps/cli/config/examples/mcp-memory/oral-patient.cordis.yml'], env, dryRun: !!values['dry-run'] }
  }
  if (action === 'doctor') {
    const port = values.port ?? env.ORAL_DOCTOR_PORT ?? '8765'
    if (!/^\d+$/.test(port) || Number(port) > 65535) throw new Error('Doctor port must be an integer from 0 to 65535.')
    if (!env.ORAL_DOCTOR_TOKEN || env.ORAL_DOCTOR_TOKEN.length < 16) {
      throw new Error('Set ORAL_DOCTOR_TOKEN to a local doctor credential with at least 16 characters.')
    }
    env.ORAL_MEMORY_ACTOR_ROLE = 'doctor'
    return { action, command: python, args: ['integrations/oral-memory/doctor_portal.py', '--port', port], env, dryRun: !!values['dry-run'] }
  }
  return { action, command: process.execPath, args: [], env, python, dryRun: !!values['dry-run'] }
}

/** Wait for completion and forward termination to the child owned by this invocation. */
async function run(command, args, env) {
  const child = spawn(command, args, { cwd: root, env, stdio: 'inherit', shell: false })
  const onInterrupt = () => child.kill('SIGINT')
  const onTerminate = () => child.kill('SIGTERM')
  process.on('SIGINT', onInterrupt)
  process.on('SIGTERM', onTerminate)
  try {
    return await new Promise((accept, reject) => {
      child.once('error', reject)
      child.once('exit', (code, signal) => accept(code ?? (signal === 'SIGINT' ? 130 : 143)))
    })
  } finally {
    process.off('SIGINT', onInterrupt)
    process.off('SIGTERM', onTerminate)
  }
}

async function main() {
  const args = process.argv.slice(2)
  const envFileArg = args.findIndex(value => value === '--env-file')
  const envFile = args.find(value => value.startsWith('--env-file='))?.slice(11)
    ?? (envFileArg >= 0 ? args[envFileArg + 1] : '.env.oral')
  if (!envFile) throw new Error('--env-file requires a file path.')
  const configPath = resolve(root, envFile)
  if (existsSync(configPath)) process.loadEnvFile(configPath)
  else if (envFile !== '.env.oral') throw new Error('The selected environment file does not exist.')
  const plan = launchPlan(args)
  if (plan.dryRun) {
    // Credentials never enter the command preview.
    console.log(JSON.stringify({ action: plan.action, command: plan.command, args: plan.args,
      patientRole: plan.env.ORAL_MEMORY_ACTOR_ROLE, database: plan.env.ORAL_MEMORY_DB }, null, 2))
    return 0
  }
  if (plan.action === 'patient' || plan.action === 'doctor') return run(plan.command, plan.args, plan.env)
  const checks = [
    [process.execPath, ['--test', 'integrations/oral-memory/tests/launch.test.mjs']],
    [plan.python, ['integrations/oral-memory/check_core.py']],
  ]
  if (plan.action === 'check') {
    checks.push([plan.python, ['--version']])
  } else {
    checks.push(
      [plan.python, ['-m', 'unittest', 'discover', '-s', 'integrations/oral-memory/tests', '-v']],
      [process.execPath, ['--import', 'tsx/esm', 'integrations/oral-memory/tests/dsh-smoke.mjs']],
      [process.execPath, ['integrations/oral-memory/tests/doctor-dom-smoke.mjs']],
      [process.execPath, ['--import', 'tsx/esm', 'integrations/oral-memory/tests/patient-preset-smoke.mjs']],
    )
  }
  for (const [command, commandArgs] of checks) {
    const code = await run(command, commandArgs, plan.env)
    if (code !== 0) return code
  }
  return 0
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try { process.exitCode = await main() }
  catch (error) {
    console.error(error instanceof Error ? error.message : 'Oral workflow failed to start.')
    process.exitCode = 1
  }
}
