import axe, { type Result } from 'axe-core';

/**
 * Runs axe-core against a rendered subtree and returns readable violation lines.
 * `color-contrast` is skipped because jsdom has no layout engine; contrast is
 * enforced separately by `styles/__tests__/tokens.test.ts`. `region` is skipped
 * because component-level fixtures are not full pages.
 */
export async function axeViolations(container: Element): Promise<string[]> {
  const results = await axe.run(container, {
    rules: {
      'color-contrast': { enabled: false },
      region: { enabled: false },
    },
  });
  return results.violations.map(
    (v: Result) => `${v.id} (${v.impact ?? 'n/a'}): ${v.help} -> ${v.nodes.map((n) => n.target.join(' ')).join(' | ')}`,
  );
}
