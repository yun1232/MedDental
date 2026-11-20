/** Exercise the real DSH MCP bridge, prompt assembly, and tool execution. */
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Context } from '@deepseek-ai/cordis'
import SystemPrompt, { renderPrompt } from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import * as NativeToolExecutor from '@deepseek-ai/dsh-tool-executor-native'
import { executeToolFixture } from '@deepseek-ai/dsh-tool-testkit'
import * as McpClient from '@deepseek-ai/dsh-mcp-client'

const root = fileURLToPath(new URL('../', import.meta.url))
const directory = await mkdtemp(resolve(tmpdir(), 'oral-memory-dsh-'))
const ctx = new Context()
try {
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(NativeToolExecutor)
  const memory = await ctx.plugin(McpClient, {
    transport: 'stdio', serverName: 'oral_memory', command: process.env.ORAL_MEMORY_PYTHON || 'python3',
    args: [resolve(root, 'server.py')], cwd: root,
    env: { ORAL_MEMORY_DB: resolve(directory, 'memory.sqlite3'), ORAL_MEMORY_PATIENT_ID: 'synthetic-dsh-smoke' },
    toolCallTimeoutMs: 10000, startupTimeoutMs: 10000, closeTimeoutMs: 5000,
    expandTemplates: false, failOnStartupError: true,
  })
  const prompt = renderPrompt(await ctx.systemPrompt.assemble())
  assert.ok(prompt.includes('优先级1=白斑表征相关性'))
  const result = await executeToolFixture(ctx, {
    name: 'mcp__oral_memory__instructions', arguments: {},
    callId: 'oral-memory-smoke', signal: new AbortController().signal,
  })
  assert.equal(result.isError, false)
  assert.equal(result.value.structuredContent.patient_id, 'synthetic-dsh-smoke')
  const retrieved = await executeToolFixture(ctx, {
    name: 'mcp__oral_memory__retrieve', arguments: {
      lesion_query: { color: '白色' }, care_query: '', experience_query: '', doctor_query: '', top_k: 5,
    }, callId: 'oral-memory-retrieve', signal: new AbortController().signal,
  })
  assert.equal(retrieved.isError, false)
  assert.equal(retrieved.value.structuredContent.status, 'no_match')
  const turn = await executeToolFixture(ctx, {
    name: 'mcp__oral_memory__patient_turn', arguments: {
      encounter_id: 'smoke-visit', event_id: 'smoke-turn', expected_revision: 0,
      message: '合成患者描述', intent: 'provide_information', observations: [{
        field: 'symptom_duration', status: 'known', value: '约一月', source_ref: 'synthetic/smoke', memory_refs: [],
      }], writes: [], doctor_answers: [],
    }, callId: 'oral-patient-turn', signal: new AbortController().signal,
  })
  assert.equal(turn.isError, false)
  assert.equal(turn.value.structuredContent.next_action.field, 'lesion_location')
  assert.equal(turn.value.structuredContent.saved_memories.length, 1)
  const state = await executeToolFixture(ctx, {
    name: 'mcp__oral_memory__case_state', arguments: { encounter_id: 'smoke-visit' },
    callId: 'oral-case-state', signal: new AbortController().signal,
  })
  assert.equal(state.isError, false)
  assert.equal(state.value.structuredContent.revision, 1)
  assert.equal(state.value.structuredContent.state.fields.symptom_duration.status, 'known')
  await memory.dispose()
  assert.ok(!renderPrompt(await ctx.systemPrompt.assemble()).includes('优先级1=白斑表征相关性'))
  process.stdout.write('DSH MCP discovery, prompt, memory, patient turn, case state and unload: passed\n')
} finally {
  await ctx.fiber.dispose()
  await rm(directory, { recursive: true, force: true })
}
