/** Exercise patient binding, role separation and shell-free application launch. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { launchPlan } from '../../../scripts/oral-project.mjs'

test('patient uses the supported dsh entry and retains both overlays', () => {
  const plan = launchPlan(['patient', '--patient', 'synthetic', '--encounter', 'visit', '--db', 'relative.sqlite3'], {
    ORAL_MEMORY_ACTOR_ROLE: 'doctor', ORAL_DOCTOR_TOKEN: 'synthetic-secret-token',
  })
  assert.equal(plan.env.ORAL_MEMORY_ACTOR_ROLE, 'patient')
  assert.equal(plan.env.ORAL_DOCTOR_TOKEN, undefined)
  assert.equal(plan.env.ORAL_MEMORY_PATIENT_ID, 'synthetic')
  assert.equal(plan.env.ORAL_MEMORY_ENCOUNTER_ID, 'visit')
  assert.ok(plan.env.ORAL_MEMORY_DB.endsWith('/MedDental-refined/relative.sqlite3') || plan.env.ORAL_MEMORY_DB.endsWith('/relative.sqlite3'))
  assert.ok(plan.args.includes('apps/cli/src/bin.ts'))
  assert.equal(plan.args.filter(x => x === '--patch').length, 2)
  const other = launchPlan(['patient', '--patient', 'other'], {})
  assert.notEqual(plan.env.DSH_HOME, other.env.DSH_HOME)
  assert.ok(plan.env.DSH_HOME.startsWith(other.env.DSH_HOME.slice(0, -24)))
})

test('doctor shares the configured database and requires a credential', () => {
  const env = { ORAL_MEMORY_PATIENT_ID: 'synthetic', ORAL_MEMORY_DB: '/tmp/synthetic.sqlite3', ORAL_DOCTOR_TOKEN: 'synthetic-secret-token' }
  const doctor = launchPlan(['doctor', '--port', '0'], env)
  const patient = launchPlan(['patient'], env)
  assert.equal(doctor.env.ORAL_MEMORY_DB, patient.env.ORAL_MEMORY_DB)
  assert.equal(doctor.env.ORAL_MEMORY_ENCOUNTER_ID, patient.env.ORAL_MEMORY_ENCOUNTER_ID)
  assert.equal(doctor.env.ORAL_MEMORY_ACTOR_ROLE, 'doctor')
  assert.deepEqual(doctor.args.slice(-2), ['--port', '0'])
  assert.throws(() => launchPlan(['doctor'], { ...env, ORAL_DOCTOR_TOKEN: '' }), /credential/)
})

test('rejects missing patient binding, invalid port and unknown arguments', () => {
  assert.throws(() => launchPlan(['patient'], {}), /PATIENT_ID/)
  assert.throws(() => launchPlan(['doctor', '--port', '-1'], { ORAL_MEMORY_PATIENT_ID: 'synthetic' }), /port/)
  assert.throws(() => launchPlan(['patient', '--shell', 'anything'], {}))
  assert.throws(() => launchPlan(['patient', 'extra'], {}), /Usage/)
})
