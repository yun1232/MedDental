/** Actual doctor page -> token-bound HTTP -> review/publication -> another patient's retrieval. */
import assert from 'node:assert/strict'
import { spawn, execFileSync } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { JSDOM, VirtualConsole } from 'jsdom'

const root = fileURLToPath(new URL('../../../', import.meta.url))
const directory = await mkdtemp(resolve(tmpdir(), 'medical-doctor-dom-'))
const python = process.env.ORAL_MEMORY_PYTHON || 'python3'
const setup = "import sys; from pathlib import Path; sys.path.insert(0,'integrations/oral-memory/tests'); from test_team import TeamTests; t=TeamTests(); t.path=Path(sys.argv[1]); t.resources=[]; t.p=t.project('synthetic-team-dom'); t.complete(); [(p.close(),s.close()) for p,s in reversed(t.resources)]"
execFileSync(python, ['-c', setup, directory], { cwd: root })
const child = spawn(python, [resolve(root, 'integrations/oral-memory/doctor_portal.py'), '--port', '0'], {
  env: { PATH: process.env.PATH, ORAL_MEMORY_DB: resolve(directory, 'private.sqlite3'), ORAL_MEMORY_PATIENT_ID: 'synthetic-team-dom',
    ORAL_DOCTOR_TOKEN: 'synthetic-team-dom-token', MEDICAL_SHARED_DB: resolve(directory, 'shared.sqlite3'), MEDICAL_ASSET_ROOT: resolve(directory, 'images') },
  stdio: ['ignore', 'pipe', 'pipe'],
})
let dom
try {
  const first = await Promise.race([once(child.stdout, 'data'), new Promise((_, reject) => setTimeout(() => reject(new Error('doctor startup timed out')), 5000).unref())])
  const url = first[0].toString().match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]
  assert.ok(url)
  const unauthorized = await fetch(url + '/api/medical?encounter_id=visit')
  assert.equal(unauthorized.status, 401)
  const errors = [], virtualConsole = new VirtualConsole()
  virtualConsole.on('jsdomError', error => errors.push(error.message))
  dom = await JSDOM.fromURL(url, { runScripts: 'dangerously', resources: 'usable', virtualConsole,
    beforeParse(window) {
      window.fetch = (path, options) => fetch(new URL(path, url), options)
      window.crypto.randomUUID = randomUUID
      window.URL.createObjectURL = () => 'blob:synthetic-preview'
      window.URL.revokeObjectURL = () => {}
    },
  })
  if (dom.window.document.readyState !== 'complete') await once(dom.window, 'load')
  const doc = dom.window.document, el = id => doc.getElementById(id)
  const waitFor = async test => {
    const deadline = Date.now() + 5000
    while (!test()) { if (Date.now() > deadline) throw new Error('Doctor team UI timed out: ' + el('medical-status').textContent); await new Promise(r => setTimeout(r, 10)) }
  }
  el('token').value = 'synthetic-team-dom-token'; el('encounter').value = 'visit'
  el('medical-load').click()
  await waitFor(() => el('medical-status').textContent.includes('当前病例 v1'))
  assert.equal(doc.querySelectorAll('#medical-images img').length, 3)
  assert.ok(el('medical-readable').textContent.includes('视觉观察'))
  el('medical-note').value = '已核对原图及观察依据'; el('medical-outcome').value = '已确认的随访结果'; el('medical-confirmed').checked = true
  el('medical-review-form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
  await waitFor(() => el('medical-status').textContent.includes('医生复核已保存'))
  el('medical-case-id').value = 'case-dom'; el('publish-phenotype').value = '白色斑块'; el('publish-context').value = '持续三个月'
  el('publish-evidence').value = '医生确认的可见外观'; el('publish-experience').value = '相似外观仍需核对病史与检查'
  el('publish-pitfalls').value = '不能迁移病理结论'; el('publish-outcome').value = '已随访'; el('publish-source').value = 'authorized-synthetic-case'
  doc.querySelector('#medical-images input[type=checkbox]').checked = true
  doc.querySelector('#medical-images input[aria-label]').value = '0,0,100,100'
  el('medical-deidentified').checked = true; el('medical-publish').click()
  await waitFor(() => el('medical-status').textContent.includes('已发布 case-dom'))
  assert.equal(el('medical-case-version').value, '1')
  assert.ok(!el('medical-case').textContent.includes('原始患者问题'))
  const transfer = "import sys,json; from pathlib import Path; sys.path.insert(0,'integrations/oral-memory/tests'); from test_team import TeamTests,patient; t=TeamTests(); t.path=Path(sys.argv[1]); t.resources=[]; t.p=t.project('another-patient'); r=t.start(); t.p.save_output(r,'patient',patient()); print(json.dumps(t.p.retrieve(r,'key1'))); [(p.close(),s.close()) for p,s in reversed(t.resources)]"
  const recalled = JSON.parse(execFileSync(python, ['-c', transfer, directory], { cwd: root, encoding: 'utf8' }))
  assert.equal(recalled.cases[0].case_id, 'case-dom')
  assert.equal(recalled.cases[0].images.length, 1)
  assert.deepEqual(errors, [])
  console.log('Doctor team DOM, authenticated images, confirmed publication and cross-patient retrieval: passed')
} finally {
  dom?.window.close(); child.kill('SIGTERM'); if (child.exitCode === null) await once(child, 'exit')
  await rm(directory, { recursive: true, force: true })
}
