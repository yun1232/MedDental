/** Real SDK -> source CLI -> OpenAI-compatible SSE -> team bridge, with synthetic data only. */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { spawn, execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../../', import.meta.url))
const directory = await mkdtemp(resolve(tmpdir(), 'oral-batch-sdk-'))
const requests = [], processes = []
let mode = 'doctor', serial = 0
const server = createServer(async (request, response) => {
  try {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    requests.push({ mode, body })
    const text = body.messages.flatMap(m => typeof m.content === 'string' ? [m.content]
      : (m.content ?? []).filter(b => b.type === 'text').map(b => b.text)).join('\n')
    const tools = body.tools ?? []
    let name, value
    if (tools.some(t => t.function.name === 'batch_run')) {
      if (!body.messages.some(m => m.role === 'tool')) { name = 'batch_run'; value = {} }
    } else {
      name = 'structured_output'
      const properties = tools.find(t => t.function.name === name)?.function.parameters.properties ?? {}
      if ('opening' in properties) value = { background: '未记录', appearance: '牙齿白色区域', history: '结束正畸',
        known_answers: [{ topic: '正畸', answer: '刚结束正畸' }], unknown_fields: ['年龄'], opening: '牙上有白斑，想咨询一下' }
      else if ('answer' in properties) value = { answer: '刚结束正畸', unknown: false }
      else if ('treatment_plan' in properties) value = { treatment_plan: '合成候选方案', abstain: false, reason: '离线合成测试' }
      else {
        if (mode === 'patient') assert.ok(!text.includes('DOCTOR_ONLY_ICDAS'), 'Full doctor facts leaked into consultation')
        const role = text.match(/角色=(patient|retrieval|visual|reasoning|review)/)?.[1]
        const image = text.match(/CURRENT ORIGINAL image_id=([^;]+)/)?.[1]
        assert.ok(role && image, 'Medical role lost its original image; tools=' + tools.map(t => t.function.name).join(',') + '; prompt=' + text.slice(-700))
        if (role === 'patient' && text.includes('checkpoint=')) {
          const checkpoint = text.match(/checkpoint=(after_key1|after_key2|after_review)/)?.[1]
          const ask = mode === 'patient' && checkpoint === 'after_key1' && !text.includes('batch-user-1')
          value = { action: ask ? 'ask' : ({ after_key1: 'visual', after_key2: 'reasoning', after_review: 'finish' })[checkpoint],
            question: ask ? '做过正畸吗？' : '', reason: '合成流程', gaps: [] }
        } else if (role === 'patient') value = { summary: '牙齿有白色区域', phenotype: ['牙齿白斑'], history: ['结束正畸'],
          patient_experiences: ['结束正畸'], missing: [], next_question: '', red_flags: [] }
        if (role === 'retrieval') value = { ranking: [], transferable_lessons: [], limitations: [] }
        if (role === 'visual') value = { findings: [{ id: 'f1', image_id: image, description: '白色区域',
          supporting_views: [image], certainty: 'clear', tags: ['白色'] }], regions: [], quality_issues: [], mask_sensitivity: [] }
        if (role === 'reasoning') value = { summary: '合成评估', hypotheses: [{ name: '牙釉质白斑', support_ids: ['f1'],
          against_ids: [], missing: [] }], case_refs: [], next_steps: ['候选方案需核查'], uncertainties: [], red_flags: [] }
        if (role === 'review') value = { route: 'approve', issues: [], required_changes: '' }
      }
      assert.ok(value, 'Unhandled structured role')
    }
    response.writeHead(200, { 'content-type': 'text/event-stream' })
    const frame = data => response.write('data: ' + JSON.stringify(data) + '\n\n')
    frame({ choices: [{ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null }] })
    if (name) frame({ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call-' + serial++,
      type: 'function', function: { name, arguments: JSON.stringify(value) } }] }, finish_reason: null }] })
    else frame({ choices: [{ index: 0, delta: { content: '合成批处理完成' }, finish_reason: null }] })
    frame({ choices: [{ index: 0, delta: {}, finish_reason: name ? 'tool_calls' : 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 2 } })
    response.end('data: [DONE]\n\n')
  } catch (error) {
    response.writeHead(400, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ error: String(error) }))
  }
})
try {
  await new Promise(accept => server.listen(0, '127.0.0.1', accept))
  const baseURL = `http://127.0.0.1:${server.address().port}/v1`
  const image = resolve(directory, '1.png')
  execFileSync(process.env.ORAL_MEMORY_PYTHON || 'python3', ['-c',
    "from PIL import Image; import sys; Image.new('RGB',(60,40),(180,160,140)).save(sys.argv[1])", image])
  for (mode of ['doctor', 'patient']) {
    const input = resolve(directory, mode + '-input.json'), output = resolve(directory, mode + '-episode.json')
    await writeFile(input, JSON.stringify({ id: '1', mode, max_turns: 3,
      facts: { '既往史': '正畸结束后', '病因': 'DOCTOR_ONLY_ICDAS' }, metadata: {}, images: [image] }))
    let log = ''
    const child = spawn(process.execPath, ['--import', 'tsx/esm', 'integrations/oral-memory/batch-worker.mjs'], {
      cwd: root, env: { ...process.env, ORAL_MEMORY_ROOT: root, ORAL_BATCH_CASE_FILE: input,
        ORAL_BATCH_RESULT_FILE: output, ORAL_MEMORY_PATIENT_ID: 'synthetic-batch-' + mode,
        ORAL_MEMORY_ENCOUNTER_ID: 'synthetic-visit', ORAL_MEMORY_DB: resolve(directory, mode + '-private.sqlite3'),
        MEDICAL_SHARED_DB: resolve(directory, mode + '-shared.sqlite3'), MEDICAL_ASSET_ROOT: resolve(directory, mode + '-assets'),
        MEDICAL_DOMAIN: 'enamel-white-spot', ORAL_MEMORY_CASE_DRIVEN: '1',
        ORAL_BATCH_HARNESS_HOME: resolve(directory, mode + '-home'), ORAL_BATCH_PROVIDER: 'oral-vlm',
        ORAL_BATCH_MODEL: 'synthetic-vision', ORAL_BATCH_API_KEY: 'synthetic-local-key', ORAL_BATCH_BASE_URL: baseURL },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    processes.push(child)
    child.stdout.on('data', c => { log += c })
    child.stderr.on('data', c => { log += c })
    const timer = setTimeout(() => child.kill('SIGTERM'), 120000)
    const code = await new Promise((accept, reject) => { child.once('error', reject); child.once('exit', accept) })
    clearTimeout(timer)
    assert.equal(code, 0, log + '\nrequests=' + JSON.stringify(requests.map(r => ({ tools: r.body.tools?.map(t => t.function.name),
      role: JSON.stringify(r.body.messages).match(/角色=(patient|retrieval|visual|reasoning|review)/)?.[1] }))))
    const episode = JSON.parse(await readFile(output, 'utf8'))
    assert.equal(episode.status, 'awaiting_doctor')
    assert.equal(episode.turns, mode === 'patient' ? 2 : 1)
    assert.equal(episode.proposal.treatment_plan, '合成候选方案')
    console.log(`${mode}: SDK, provider, original image, conversation and treatment output passed`)
  }
  assert.ok(requests.length > 15)
} finally {
  for (const child of processes) if (child.exitCode === null) child.kill('SIGKILL')
  await new Promise(accept => { server.close(accept); server.closeAllConnections() })
  await rm(directory, { recursive: true, force: true })
}
