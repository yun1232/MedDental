/** Drive the doctor form's JavaScript against its real loopback HTTP service. */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { JSDOM, VirtualConsole } from 'jsdom'

const root = fileURLToPath(new URL('../', import.meta.url))
const directory = await mkdtemp(resolve(tmpdir(), 'oral-doctor-dom-'))
const child = spawn(process.env.ORAL_MEMORY_PYTHON || 'python3', [resolve(root, 'doctor_portal.py'), '--port', '0'], {
  env: { PATH: process.env.PATH, ORAL_MEMORY_DB: resolve(directory, 'memory.sqlite3'),
    ORAL_MEMORY_PATIENT_ID: 'synthetic-dom', ORAL_DOCTOR_TOKEN: 'synthetic-dom-token' },
  stdio: ['ignore', 'pipe', 'pipe'],
})
let dom
try {
  const first = await Promise.race([once(child.stdout, 'data'), new Promise((_, reject) => setTimeout(() => reject(new Error('doctor service startup timed out')), 5000).unref())])
  const url = first[0].toString().match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]
  assert.ok(url)
  const errors = []
  const virtualConsole = new VirtualConsole()
  virtualConsole.on('jsdomError', error => errors.push(error.message))
  dom = await JSDOM.fromURL(url, { runScripts: 'dangerously', resources: 'usable', virtualConsole,
    beforeParse(window) {
      window.fetch = (path, options) => fetch(new URL(path, url), options)
      window.crypto.randomUUID = randomUUID
    },
  })
  if (dom.window.document.readyState !== 'complete') await once(dom.window, 'load')
  const document = dom.window.document
  const waitFor = async check => {
    const end = Date.now() + 5000
    while (!check()) {
      if (Date.now() > end) throw new Error('doctor UI operation timed out: ' + document.getElementById('status').textContent)
      await new Promise(resolve => setTimeout(resolve, 10))
    }
  }
  document.getElementById('token').value = 'synthetic-dom-token'
  document.getElementById('load').click()
  await waitFor(() => document.getElementById('case-meta').textContent.includes('synthetic-dom'))
  document.getElementById('note-type').value = 'follow_up'
  document.getElementById('target').value = 'prior_treatment'
  document.getElementById('description').value = '请补充上次口腔处理的大致经过。'
  document.getElementById('source').value = 'synthetic/doctor-review'
  document.getElementById('confirmed').checked = true
  document.getElementById('submit-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
  await waitFor(() => document.getElementById('questions').textContent.includes('请补充上次'))
  assert.ok(document.getElementById('next').textContent.includes('请补充上次'))
  assert.deepEqual(errors, [])
  process.stdout.write('Doctor form DOM, real HTTP submission and refreshed question: passed\n')
} finally {
  dom?.window.close()
  child.kill('SIGTERM')
  if (child.exitCode === null) await once(child, 'exit')
  await rm(directory, { recursive: true, force: true })
}
