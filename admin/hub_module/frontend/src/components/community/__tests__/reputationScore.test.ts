import { describe, expect, it } from 'vitest';
import { MAX_SCORE, MIN_SCORE, scorePercent } from '../reputationScore';

describe('scorePercent', () => {
  it('maps the 300-850 scale onto 0-100 and clamps outside it', () => {
    expect(scorePercent(MIN_SCORE)).toBe(0);
    expect(scorePercent(MAX_SCORE)).toBe(100);
    expect(scorePercent(575)).toBe(50);
    expect(scorePercent(100)).toBe(0);
    expect(scorePercent(2000)).toBe(100);
  });
});
