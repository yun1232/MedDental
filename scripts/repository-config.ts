/** Locate repository configuration while supporting independently supplied fixture workspaces. */
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'

/** Return an absolute config path; externally supplied workspaces may keep their own root layout. */
export function repositoryConfig(root: string, filename: string): string {
  const consolidated = resolve(root, 'config', filename)
  return existsSync(consolidated) ? consolidated : resolve(root, filename)
}
