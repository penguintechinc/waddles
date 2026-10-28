import type { HTMLAttributes } from 'react';
import { cx } from './cx';

export type BadgeTone = 'gold' | 'sky' | 'green' | 'red' | 'gray';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
}

const TONE_CLASS: Record<BadgeTone, string> = {
  gold: 'badge-gold',
  sky: 'badge-sky',
  green: 'badge-green',
  red: 'badge-red',
  gray: 'badge-gray',
};

/**
 * Compact status/count pill. Colour is never the only signal: always include
 * text ("Active", "Failed"), not just a coloured dot.
 */
export function Badge({ tone = 'gray', className, children, ...rest }: BadgeProps) {
  return (
    <span className={cx('badge', TONE_CLASS[tone], className)} {...rest}>
      {children}
    </span>
  );
}
