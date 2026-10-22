/** Lowest and highest score on the FICO-style reputation scale. */
export const MIN_SCORE = 300;
export const MAX_SCORE = 850;

/** Percentage of the 300-850 range, clamped for the progress bar. */
export function scorePercent(score: number): number {
  return Math.max(0, Math.min(100, ((score - MIN_SCORE) / (MAX_SCORE - MIN_SCORE)) * 100));
}
