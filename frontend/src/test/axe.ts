import axe from 'axe-core'

/**
 * axe-core's accessibility violations in what is rendered now, one readable line per offending element; empty when
 * there are none. Tests assert `toEqual([])`, so any violation, of any impact, fails.
 *
 * The test harness's own address bar (data-testid="browser", test/render.tsx) is not part of the app and is left
 * out. jsdom lays nothing out and has no canvas, so axe cannot judge colour contrast here (it could only answer
 * "incomplete"): that one rule is off, and the manual audit covers contrast.
 */
export async function axeViolations(root: Element = document.body): Promise<string[]> {
  const results = await axe.run(
    { include: [root], exclude: ['[data-testid="browser"]'] },
    { resultTypes: ['violations'], rules: { 'color-contrast': { enabled: false } } },
  )
  return results.violations.flatMap((violation) =>
    violation.nodes.map((node) => `${violation.id} (${violation.impact}): ${violation.help} -- ${node.target.join(' ')}`),
  )
}
