/** Deny inherited global tools in medical evidence roles; runtime-local structured_output remains available. */
export const name = 'medical-role-isolation'
export const inject = ['tools']
export function apply(ctx) {
  ctx.effect(() => ctx.tools.restrict({ allow: [] }))
}
