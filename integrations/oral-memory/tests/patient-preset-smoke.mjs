/** Mount the configured patient persona and confirm MCP policy remains model-visible. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import yaml from 'js-yaml'
import { Context } from '@deepseek-ai/cordis'
import { createScope } from '@deepseek-ai/dsh-scope'
import SystemPrompt, { renderPrompt } from '@deepseek-ai/dsh-system-prompt'
import * as Persona from '@deepseek-ai/dsh-persona'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import * as NativeToolExecutor from '@deepseek-ai/dsh-tool-executor-native'
import * as McpClient from '@deepseek-ai/dsh-mcp-client'

const root = fileURLToPath(new URL('../../../', import.meta.url))
const overlay = yaml.load(readFileSync(resolve(root, 'apps/cli/config/examples/mcp-memory/oral-patient.cordis.yml'), 'utf8'), { schema: yaml.DEFAULT_SCHEMA.extend([new yaml.Type('tag:yaml.org,2002:js', { kind: 'scalar', construct: value => value })]) })
const registry = overlay.find(row => row.id === 'agent-preset-registry')
const preset = overlay.flatMap(row => row.insert ?? []).find(row => row.id === 'preset-oral-patient')
assert.equal(registry.config.default, preset.config.id)
assert.deepEqual(preset.config.plugins.map(row => row.id), ['persona', 'tool-fs', 'tool-web', 'tool-bash', 'tool-pwsh', 'tool-jobs', 'tool-present', 'compaction'])
const persona = preset.config.plugins.find(row => row.id === 'persona')
assert.equal(persona.config.complete, false)
const ctx = new Context()
const directory = await mkdtemp(resolve(tmpdir(), 'oral-preset-'))
try {
  await ctx.plugin(SystemPrompt, { personaPrefix: 'You are a coding agent.' })
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(NativeToolExecutor)
  await ctx.plugin(McpClient, {
    transport: 'stdio', serverName: 'oral_memory', command: process.env.ORAL_MEMORY_PYTHON || 'python3',
    args: [resolve(root, 'integrations/oral-memory/server.py')], cwd: root,
    env: { ORAL_MEMORY_DB: resolve(directory, 'memory.sqlite3'), ORAL_MEMORY_PATIENT_ID: 'synthetic-preset' },
    expandTemplates: false, failOnStartupError: true,
  })
  const key = {}
  const scope = createScope(ctx, key)
  const mounted = await scope.ctx.plugin(Persona, persona.config)
  const prompt = renderPrompt(await ctx.systemPrompt.assemble({ scope: key }))
  assert.ok(prompt.includes('你是口腔白斑辅助问询助手'))
  assert.ok(prompt.includes('优先级1=白斑表征相关性'))
  assert.ok(prompt.includes('patient_turn'))
  assert.ok(!prompt.includes('You are a coding agent.'))
  await mounted.dispose()
  assert.ok(renderPrompt(await ctx.systemPrompt.assemble({ scope: key })).includes('You are a coding agent.'))
  console.log('Patient persona, inherited MCP policy and disposal: passed')
} finally {
  await ctx.fiber.dispose()
  await rm(directory, { recursive: true, force: true })
}
