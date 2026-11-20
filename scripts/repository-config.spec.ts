import { mkdtempSync, mkdirSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import ts from 'typescript'
import { expect, it, onTestFinished } from 'vitest'
import { repositoryConfig } from './repository-config.ts'

it('uses consolidated repository rules while accepting an external workspace layout', () => {
  const root = mkdtempSync(join(tmpdir(), 'meddental-config-'))
  onTestFinished(() => rmSync(root, { recursive: true, force: true }))
  writeFileSync(join(root, 'tsconfig.host.json'), '{}')
  expect(repositoryConfig(root, 'tsconfig.host.json')).toBe(join(root, 'tsconfig.host.json'))
  mkdirSync(join(root, 'config'))
  writeFileSync(join(root, 'config/tsconfig.host.json'), '{}')
  expect(repositoryConfig(root, 'tsconfig.host.json')).toBe(join(root, 'config/tsconfig.host.json'))
})

it('resolves real workspace source imports through the discoverable root solution', () => {
  const root = resolve(import.meta.dirname, '..')
  const parsed = ts.getParsedCommandLineOfConfigFile(join(root, 'tsconfig.json'), {}, {
    ...ts.sys,
    onUnRecoverableConfigFileDiagnostic: error => { throw new Error(ts.flattenDiagnosticMessageText(error.messageText, '\n')) },
  })!
  expect(parsed.errors).toEqual([])
  expect(parsed.fileNames).toEqual([])
  for (const ref of parsed.projectReferences ?? []) {
    expect(existsSync(ref.path)).toBe(true)
    const face = ts.getParsedCommandLineOfConfigFile(ref.path, {}, {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic: error => { throw new Error(ts.flattenDiagnosticMessageText(error.messageText, '\n')) },
    })!
    expect(face.errors).toEqual([])
    expect(face.options.rootDir).toBe(root)
  }
  for (const [name, target] of [
    ['@deepseek-ai/dsh-tools', 'packages/core/tools/src/index.ts'],
    ['@deepseek-ai/dsh-mcp-client', 'packages/mcp/mcp-client/src/index.ts'],
    ['@deepseek-ai/cordis', 'vendor/cordis/src/index.ts'],
  ]) {
    const resolved = ts.resolveModuleName(name!, join(root, 'scripts/source-probe.ts'), parsed.options, ts.sys)
    expect(resolved.resolvedModule?.resolvedFileName).toBe(join(root, target!))
  }
})
