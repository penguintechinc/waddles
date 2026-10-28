/**
 * Design-token contrast guard. Parses `tokens.css` and asserts the WCAG 2.2 AA
 * thresholds for every text/background and control-boundary pairing the system
 * relies on, so a token tweak that breaks legibility fails CI instead of
 * shipping. Thresholds: 4.5:1 body text, 3:1 UI component boundaries (1.4.11).
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

// Read from disk (vitest runs from the package root): vitest stubs CSS imports,
// even `?raw`, to an empty string.
const css = readFileSync('src/styles/tokens.css', 'utf8');

/** Reads `--c-<name>: R G B;` into [r, g, b]. */
function token(name: string): [number, number, number] {
  const match = css.match(new RegExp(`--c-${name}:\\s*(\\d+)\\s+(\\d+)\\s+(\\d+)`));
  if (!match) throw new Error(`token --c-${name} not found in tokens.css`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

/** WCAG relative luminance. */
function luminance([r, g, b]: [number, number, number]): number {
  const [lr, lg, lb] = [r, g, b].map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb;
}

/** WCAG contrast ratio between two colors. */
function contrast(a: [number, number, number], b: [number, number, number]): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

const WHITE: [number, number, number] = [255, 255, 255];
const SURFACES = ['app', 'surface', 'raised'] as const;

describe('tokens.css contrast', () => {
  it('parses every token the test depends on', () => {
    for (const name of ['app', 'surface', 'raised', 'field', 'fg', 'fg-secondary', 'fg-muted', 'line-input', 'focus']) {
      expect(token(name)).toHaveLength(3);
    }
  });

  describe.each(SURFACES)('text on %s', (surface) => {
    it.each(['fg', 'fg-secondary', 'fg-muted', 'accent-fg', 'interactive', 'success-fg', 'warning-fg', 'danger-fg', 'info-fg'])(
      '%s meets 4.5:1',
      (fg) => {
        expect(contrast(token(fg), token(surface))).toBeGreaterThanOrEqual(4.5);
      },
    );
  });

  it('placeholder text (fg-muted) is legible on the field fill', () => {
    expect(contrast(token('fg-muted'), token('field'))).toBeGreaterThanOrEqual(4.5);
  });

  it.each([
    ['accent (primary CTA)', 'accent'],
    ['accent-hover', 'accent-hover'],
    ['success fill', 'success'],
  ])('inverse text on %s meets 4.5:1', (_label, fill) => {
    expect(contrast(token('fg-inverse'), token(fill))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(['danger', 'danger-hover'])('white text on %s fill meets 4.5:1', (fill) => {
    expect(contrast(WHITE, token(fill))).toBeGreaterThanOrEqual(4.5);
  });

  it('control borders are >=3:1 against the surface they sit on (WCAG 1.4.11)', () => {
    expect(contrast(token('line-input'), token('surface'))).toBeGreaterThanOrEqual(3);
    expect(contrast(token('line-input'), token('app'))).toBeGreaterThanOrEqual(3);
  });

  it('the focus ring is >=3:1 against every surface (WCAG 2.4.11)', () => {
    for (const surface of SURFACES) {
      expect(contrast(token('focus'), token(surface))).toBeGreaterThanOrEqual(3);
    }
  });

  it('surfaces are strictly ordered by luminance (app < surface < raised)', () => {
    expect(luminance(token('app'))).toBeLessThan(luminance(token('surface')));
    expect(luminance(token('surface'))).toBeLessThan(luminance(token('raised')));
  });
});
