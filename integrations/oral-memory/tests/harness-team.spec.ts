/** Real Loader/preset -> parent tool -> spawn AgentLoops -> image prompts -> persistent medical state. */
import { afterEach, describe, expect, it } from 'vitest'
import { mkdtemp, rm, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { Context } from '@deepseek-ai/cordis'
import Loader from '@deepseek-ai/cordis-plugin-loader'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'
import AgentPresets from '@deepseek-ai/dsh-agent-preset-registry'
import AttachmentLocal from '@deepseek-ai/dsh-attachment-local'
import SubagentRuntime from '@deepseek-ai/dsh-subagent'
import * as Spawn from '@deepseek-ai/dsh-subagent-spawn-in-process'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { mountAgentLoopTestDependencies } from '@deepseek-ai/dsh-agent-loop-testkit'
import { scopeOf } from '@deepseek-ai/dsh-scope'
import { brandString } from '@deepseek-ai/dsh-brand'
import type { SessionId } from '@deepseek-ai/dsh-session'
import { mountLocalActivations } from '../../../packages/subagent/subagent/tests/local-activation.ts'
import { MockAdapter, textResponse, toolCallResponse } from '../../../packages/core/agent-loop/tests/mock-adapter.ts'
import yaml from 'js-yaml'
import { execFileSync } from 'node:child_process'

const root = fileURLToPath(new URL('../../../', import.meta.url))
const resources: Array<() => Promise<unknown>> = []
afterEach(async () => { for (const cleanup of resources.splice(0).reverse()) await cleanup() })

async function fixture(reviewRoutes = ['approve'], imageCapable = true, seedCase = false, seedPersonal = false, askAfterRetrieval = false, visualVerdict = '') {
  const directory = await mkdtemp(resolve(tmpdir(), 'medical-team-'))
  resources.push(() => rm(directory, { recursive: true, force: true }))
  const before = { ...process.env }
  Object.assign(process.env, { ORAL_MEMORY_ROOT: root, ORAL_MEMORY_DB: resolve(directory, 'memory.sqlite3'),
    ORAL_MEMORY_PATIENT_ID: 'synthetic-team', ORAL_MEMORY_ENCOUNTER_ID: 'visit',
    MEDICAL_SHARED_DB: resolve(directory, 'shared.sqlite3'), MEDICAL_ASSET_ROOT: resolve(directory, 'images'), MEDICAL_DOMAIN: 'oral-leukoplakia', MEDICAL_TOP_K: seedPersonal ? '1' : '5', MEDICAL_CANDIDATE_LIMIT: '12' })
  resources.push(async () => { for (const key of Object.keys(process.env)) if (!(key in before)) delete process.env[key]; Object.assign(process.env, before) })
  if (seedCase) execFileSync(process.env.ORAL_MEMORY_PYTHON || 'python3', ['-c', "import sys; from pathlib import Path; sys.path.insert(0,'integrations/oral-memory/tests'); from test_team import TeamTests; t=TeamTests(); t.path=Path(sys.argv[1]); t.resources=[]; t.p=t.project('patient-a'); t.publish_case(); [(p.close(),s.close()) for p,s in reversed(t.resources)]", directory], { cwd: root })
  if (seedPersonal) execFileSync(process.env.ORAL_MEMORY_PYTHON || 'python3', ['-c', `
import sys,base64,io
from pathlib import Path
sys.path.insert(0,'integrations/oral-memory/tests')
from test_memory import lesion,narrative,query
from store import MemoryStore
from team_memory import MedicalProject
from extensions import reference
from PIL import Image
s=MemoryStore(Path(sys.argv[1])/'memory.sqlite3','synthetic-team','doctor')
p=MedicalProject(s)
for rid,site,color in [('native-a','颊黏膜',(10,20,30)),('native-b','舌',(30,20,10))]:
 r=s.put('lesion',{'record':lesion(rid,'historical-'+rid,site=site),'expected_version':0})['record']
 s.put('experience',{'record':narrative('experience','event-'+rid,'historical-'+rid,description='持续三个月' if rid=='native-a' else '三个月',links=[rid]),'expected_version':0})
 b=io.BytesIO();Image.new('RGB',(40,40),color).save(b,'PNG')
 image=p._save_image(base64.b64encode(b.getvalue()).decode(),p._private_dir())
 s.link_visual(reference(r),image,[{'image_id':image['image_id'],'box':[100,100,600,600],'description':'历史区域','basis':['历史原图']}] if ${visualVerdict ? 'True' : 'False'} else [],[],'old-memory',rid)
for rid,count in [('native-a',6),('native-b',2)]:
 for i in range(count):
  receipt=s.retrieve(query(top_k=50))
  s.memory_feedback({'event_id':rid+str(i),'retrieval_id':receipt['retrieval_id'],'reference':{'category':'lesion','record_id':rid,'version':1},'kind':'not_helpful','reason':'此前实际无帮助'})
p.close();s.close()
`, directory], { cwd: root })
  const ctx = new Context()
  resources.push(() => ctx.fiber.dispose())
  ctx.baseUrl = pathToFileURL(resolve(root, 'apps/cli/config') + '/').href
  await ctx.plugin(Loader)
  await mountAgentLoopTestDependencies(ctx)
  await mountLocalActivations(ctx)
  await ctx.plugin(AttachmentLocal, { dshHome: resolve(directory, 'home') })
  await ctx.plugin(AgentLoop, { agents: [] })
  await ctx.plugin(AgentPresets, { default: 'oral-patient' })
  await ctx.plugin(SubagentRuntime)
  await ctx.plugin(Spawn, { providerName: 'spawn' })
  const js = new yaml.Type('tag:yaml.org,2002:js', { kind: 'scalar', construct: (value: string) => Function('process', `return (${value})`)(process) })
  const overlay = yaml.load(await readFile(resolve(root, 'apps/cli/config/examples/mcp-memory/oral-patient.cordis.yml'), 'utf8'), { schema: yaml.DEFAULT_SCHEMA.extend([js]) }) as any[]
  const presets = overlay.flatMap(row => row.insert ?? []).map(row => row.config)
  const parentPreset = presets.find(p => p.id === 'oral-patient')
  // Mount the shipped medical slice through the real Loader; unrelated terminal/web tools are outside this fixture.
  const unregisterParent = await ctx.agentPresets.register({ ...parentPreset, plugins: parentPreset.plugins.filter((p: any) => ['persona', 'medical-team'].includes(p.id)) })
  await ctx.agentPresets.register(presets.find(p => p.id === 'medical-role'))
  let reviews = 0, findings = 0
  const seen: any[] = []
  const respond = (options: any) => {
    const all = options.messages.flatMap((m: any) => m.content)
    const text = all.filter((b: any) => b.type === 'text').map((b: any) => b.text).join('\n')
    const role = text.match(/角色=(patient|retrieval|visual|reasoning|review)/)?.[1]
    if (!role) throw new Error('Expected a medical role prompt')
    expect(options.tools.map((t: any) => t.name)).toEqual(['structured_output'])
    const imageId = text.match(/CURRENT ORIGINAL image_id=([^;]+)/)![1]
    seen.push({ role, all, text, imageId })
    let value
    if (role === 'patient' && text.includes('checkpoint=')) {
      const message = all.find((b: any) => b.type === 'text' && b.text.startsWith('角色='))!.text
      const data = JSON.parse(message.slice(message.indexOf('\n{') + 1))
      const checkpoint = text.match(/checkpoint=(after_key1|after_key2|after_review)/)![1]
      const audit = [...data.run.outputs].reverse().find((o: any) => o.role === 'review')?.value
      value = { action: checkpoint === 'after_key1' ? (askAfterRetrieval ? 'ask' : 'visual') : checkpoint === 'after_key2' ? 'reasoning' : ({approve:'finish',patient:'ask',retrieval:'retrieve'}[audit.route] || audit.route),
        question: askAfterRetrieval || audit?.route === 'patient' ? '请补充检查结果？' : '', reason: '患者交流角色依据检索与审查控制流程', gaps: askAfterRetrieval ? [{field: 'prior_examination', why: '与病例检查证据比较', case_refs: data.run.retrievals.at(-1).cases.slice(0, 1).map((c: any) => ({case_id: c.case_id, version: c.version}))}] : [] }
    } else if (role === 'patient') value = { ...(seedPersonal ? { features: { site: '颊黏膜', color: '白色' } } : {}), summary: '患者提供原图', phenotype: ['白色斑块'], history: ['持续三个月'], patient_experiences: ['持续三个月'], missing: ['病理'], next_question: '有无病理报告？', red_flags: [] }
    if (role === 'retrieval') {
      expect(text).not.toContain('"decay_score":')
      expect(text).not.toContain('"final_score":')
      expect(text).not.toContain('"utility":')
      const message = all.find((b: any) => b.type === 'text' && b.text.startsWith('角色='))!.text
      const data = JSON.parse(message.slice(message.indexOf('\n{') + 1))
      const cases = data.run.retrievals.at(-1)?.cases ?? []
      value = { ranking: cases.map((c: any) => ({ case_id: c.case_id, version: c.version, relevance: c.memory_reference.record_id === 'native-a' ? 90 : c.memory_reference.record_id === 'native-b' ? 80 : 88,
        supports: ['当前证据可比较'], contradictions: ['病理尚缺'], reason: '本轮参考价值' })), transferable_lessons: [], limitations: ['病理需另核查'] }
    }
    if (role === 'visual') {
      findings++
      value = { findings: [{ id: 'f1', image_id: imageId, description: '可见白色区域', supporting_views: [imageId], certainty: 'clear', tags: ['白色斑块'] }],
        regions: [{ image_id: imageId, box: [100, 100, 600, 600], description: '可见区域', basis: ['当前原图'] }], quality_issues: [], mask_sensitivity: findings > 1 ? ['原图与裁剪图相互支持，遮挡不产生独立阳性证据'] : [] }
    }
    if (role === 'visual' && visualVerdict && !text.includes('独立观察基线：')) {
      const message = all.find((b: any) => b.type === 'text' && b.text.startsWith('角色='))!.text
      const data = JSON.parse(message.slice(message.indexOf('\n{') + 1))
      const source = data.run.rerankings.find((r: any) => r.key === 'key1').selected[0]
      value.regions[0].source_cases = [{ case_id: source.case_id, version: source.version }]
      const crop = text.match(/DERIVED crop; view_id=([^;]+)/)?.[1]
      if (crop && visualVerdict === 'new_information') {
        value.findings[0].description = '当前裁剪可见新增表面细节'
        value.findings[0].supporting_views = [crop]
        value.contributions = [{ case_id: source.case_id, version: source.version, view_id: crop, finding_id: 'f1' }]
      }
    }
    if (role === 'reasoning') value = { summary: '需医生复核', hypotheses: [{ name: '待查黏膜病变', support_ids: ['f1'], against_ids: [], missing: ['病理'] }], case_refs: [], next_steps: ['核查报告'], uncertainties: ['病理未知'], red_flags: [] }
    if (role === 'review' && text.includes('执行视觉记忆价值审查')) {
      const message = all.find((b: any) => b.type === 'text' && b.text.startsWith('角色='))!.text
      const data = JSON.parse(message.slice(message.indexOf('\n{') + 1))
      expect(data.run.doctor_review).toBeNull()
      expect(data.run.outputs.every((o: any) => o.role === 'visual')).toBe(true)
      expect(data).not.toHaveProperty('prior_reviews')
      value = { summary: '直接审查区域视觉判断', assessments: data.visual_value_tasks.map((t: any) => ({
        case_id: t.case_id, version: t.version, verdict: visualVerdict, reason: '对照区域判断与独立基线',
        review_steps: t.regions.map((r: any) => r.step), finding_refs: visualVerdict === 'new_information' ? t.regions.flatMap((r: any) => r.new_information_candidates.map((g: any) => ({ step: r.step, finding_id: g.finding_id }))) : [] })) }
    } else if (role === 'review') value = { route: reviewRoutes[Math.min(reviews++, reviewRoutes.length - 1)], issues: [], required_changes: '核查证据' }
    return toolCallResponse('role-' + seen.length, 'structured_output', value)
  }
  const script: any[] = [toolCallResponse('consult', 'medical_consult', {}), ...Array.from({ length: 36 }, () => (options: any) => {
    if (options.tools.some((t: any) => t.name === 'medical_consult')) return textResponse('辅助评估已完成，等待医生。')
    return respond(options)
  })]
  class VisualAdapter extends MockAdapter {
    override async resolveModel(provider: string, model: string) { return { ...await super.resolveModel(provider, model), inputModalities: imageCapable ? ['text', 'image'] as const : ['text'] as const } }
  }
  const adapter = new VisualAdapter(script)
  ctx.llm.registerAdapter(['mock'], adapter)
  const handle = await ctx.agents.create({ sessionId: brandString<SessionId>('synthetic-parent'), agentOptions: { provider: 'mock', model: 'mock' },
    setup: async agentCtx => { await ctx.agentPresets.mount(agentCtx, 'oral-patient') } })
  const png = execFileSync(process.env.ORAL_MEMORY_PYTHON || 'python3', ['-c', "from PIL import Image; import sys; Image.new('RGB',(80,60),(208,160,160)).save(sys.stdout.buffer,'PNG')"])
  const image = await ctx.attachments.saveImage({ data: png, mediaType: 'image/png' })
  handle.agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: '这是我的原始问题，请帮我评估口内白色区域。' }, { type: 'image', attachment: image }] }))
  await handle.agent.whenIdle()
  const events = handle.agent.session.snapshotEvents()
  if (imageCapable && seen.length === 0) throw new Error(JSON.stringify(events.filter(e => e.type === 'tool/result')))
  if (imageCapable && JSON.stringify(events.filter(e => e.type === 'tool/result')).includes('Error:')) throw new Error(JSON.stringify(events.filter(e => e.type === 'tool/result')))
  return { ctx, handle, seen, events, adapter, directory, unregisterParent }
}

function result(events: any[]) {
  const outputs = events.filter(e => e.type === 'tool/result')
  return JSON.stringify(outputs)
}

describe('image-aware medical team composition', () => {
  it('runs all five roles with original question/images, performs two retrievals and persists the reviewed workflow', async () => {
    const { ctx, handle, seen, events, directory, unregisterParent } = await fixture(['approve'], true, true)
    expect(seen.map(s => s.role)).toEqual(['patient', 'retrieval', 'patient', 'visual', 'visual', 'visual', 'visual', 'retrieval', 'patient', 'reasoning', 'review', 'patient'])
    for (const s of seen) {
      expect(s.text).toContain('这是我的原始问题')
      expect(s.all.some((b: any) => b.type === 'image')).toBe(true)
    }
    expect(seen[3].text).not.toContain('HISTORICAL REFERENCE')
    expect(seen[3].text).not.toContain('case-1')
    expect(seen[4].text).toContain('HISTORICAL REFERENCE case_id=case-1')
    expect(seen[4].all.filter((b: any) => b.type === 'image').length).toBeGreaterThan(1)
    expect(seen[5].text).toContain('DERIVED crop')
    expect(seen[5].text).toContain('DERIVED mask')
    expect(seen[1].text).toContain('"purpose":"visual"')
    expect(seen[1].text).toContain('"retrieval_channels":["patient_experience"]')
    expect(seen[7].text).toContain('"key":"key2"')
    expect(seen[7].text).toContain('"retrieval_channels":["lesion","patient_experience"]')
    expect(seen[0].text).toContain('"revision":1')
    expect(result(events)).toContain('awaiting_doctor')
    const { bridge } = await import('../harness-team.mjs')
    const state = await bridge({ ...process.env }, 'state', { encounter: 'visit' }, new AbortController().signal)
    expect(state.question).toContain('这是我的原始问题')
    expect(state.images).toHaveLength(1)
    expect(await readFile(resolve(directory, 'memory.sqlite3'))).not.toHaveLength(0)
    // The preset owns the tool across sessions. Disposing the Agent and unloading the preset releases it.
    await handle.dispose()
    await unregisterParent()
    expect(ctx.tools.definitions(scopeOf(handle.agent.ctx)).map(t => t.name)).not.toContain('medical_consult')
  }, 30000)

  it('routes missing information back to the patient and stops the consultation', async () => {
    const { seen, events } = await fixture(['patient'])
    expect(seen.filter(s => s.role === 'review')).toHaveLength(1)
    expect(result(events)).toContain('needs_input')
  }, 30000)

  it('revisits the requested role and bounds repeated audit failures', async () => {
    const { seen, events } = await fixture(['reasoning'])
    expect(seen.filter(s => s.role === 'review')).toHaveLength(3)
    expect(seen.filter(s => s.role === 'reasoning')).toHaveLength(3)
    expect(result(events)).toContain('needs_human')
  }, 30000)

  it('rebuilds and rechecks the requested visual evidence before returning to the reviewer', async () => {
    const { seen, events } = await fixture(['visual', 'approve'])
    expect(seen.filter(s => s.role === 'review')).toHaveLength(2)
    expect(seen.filter(s => s.role === 'visual')).toHaveLength(7)
    expect(result(events)).toContain('awaiting_doctor')
  }, 30000)

  it('uses original private memories directly, hides iteration scores from the model, and selects by M times D', async () => {
    const { seen, events } = await fixture(['approve'], true, false, true)
    const event = events.find(e => e.type === 'tool/result' && e.data.message.source.callId === 'consult') as any
    const completed = JSON.parse(event.data.message.content.find((b: any) => b.type === 'text').text)
    expect(completed.status).toBe('awaiting_doctor')
    const candidates = completed.retrievals[0].cases
    expect(candidates[0].memory_reference.record_id).toBe('native-a')
    const selected = completed.rerankings.at(-1).selected
    const best = candidates.find((c: any) => c.memory_reference.record_id === 'native-b')
    expect(selected).toHaveLength(1)
    expect(selected[0]).toMatchObject({ case_id: best.case_id, model_score: 80, decay_score: .8, final_score: 64 })
    expect(seen.find(s => s.role === 'visual' && s.text.includes('HISTORICAL REFERENCE'))!.text).toContain('HISTORICAL REFERENCE case_id=' + best.case_id)
    expect(completed.retrievals[1].cases).toHaveLength(2)
  }, 30000)

  it('lets the patient agent ask a case-grounded question before running visual or reasoning roles', async () => {
    const { seen, events } = await fixture(['approve'], true, false, true, true)
    expect(seen.map(s => s.role)).toEqual(['patient', 'retrieval', 'patient'])
    expect(result(events)).toContain('needs_input')
    expect(result(events)).toContain('请补充检查结果')
  }, 30000)

  it.each(['new_information', 'no_new_information', 'uncertain'])('audits regional visual value without doctor feedback: %s', async verdict => {
    const { seen, events, directory } = await fixture(['approve'], true, false, true, false, verdict)
    expect(seen.filter(s => s.role === 'review' && s.text.includes('执行视觉记忆价值审查'))).toHaveLength(1)
    expect(result(events)).toContain('awaiting_doctor')
    const inspect = `import sys,json; from pathlib import Path; sys.path.insert(0,'integrations/oral-memory'); from store import MemoryStore; from team_memory import MedicalProject; s=MemoryStore(Path(sys.argv[1])/'memory.sqlite3','synthetic-team','patient'); p=MedicalProject(s); r=p.list_runs('visit')[0]; print(json.dumps({'audit':r['visual_value_review'],'doctor':r['doctor_review'],'values':[dict(x) for x in s.db.execute("SELECT stage,value FROM stage_values WHERE record_id='native-b'")]})); p.close(); s.close()`
    const stored = JSON.parse(execFileSync(process.env.ORAL_MEMORY_PYTHON || 'python3', ['-c', inspect, directory], {cwd: root, encoding: 'utf8'}))
    expect(stored.doctor).toBeNull()
    expect(stored.audit.source).toBe('review_agent')
    expect(stored.values.find((x: any) => x.stage === 'visual').value).toBe(verdict === 'no_new_information' ? .7 : .8)
    expect(stored.values.find((x: any) => x.stage === 'diagnosis').value).toBe(.8)
  }, 30000)

  it('refuses a text-only model before any child sees a reduced input', async () => {
    const { seen, events } = await fixture(['approve'], false)
    expect(seen).toHaveLength(0)
    expect(result(events)).toContain('MODEL_DOES_NOT_SUPPORT_IMAGES')
  }, 30000)
})
