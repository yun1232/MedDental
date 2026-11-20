/** Resolve a pinned npm archive from the workspace lock. */
import { join } from 'node:path'
import { readFile } from 'node:fs/promises'
import { load as parseYaml } from 'js-yaml'
import type { RuntimeNpmArchive } from './primary-runtime/prepare.ts'
export async function runtimeNpmArchive(name: string, version: string, lockfile = join(import.meta.dirname, '../pnpm-lock.yaml')): Promise<RuntimeNpmArchive> {
  const lock = parseYaml(await readFile(lockfile, 'utf8')) as { packages?: Record<string, { resolution?: { integrity?: string } }> }
  const integrity = lock.packages?.[`${name}@${version}`]?.resolution?.integrity
  if (typeof integrity !== 'string' || !/^sha512-[A-Za-z0-9+/]+={0,2}$/u.test(integrity)) {
    throw new Error(`runtime npm lock: missing SHA-512 integrity for ${name}@${version}`)
  }
  return { name, version, url: `https://registry.npmjs.org/${name}/-/${name.split('/').at(-1)}-${version}.tgz`, integrity }
}
