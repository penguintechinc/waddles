import { cx } from './cx';

export interface SkeletonProps {
  className?: string | undefined;
}

/** Decorative placeholder block; hidden from assistive tech (the parent announces loading). */
export function Skeleton({ className }: SkeletonProps) {
  return <div aria-hidden="true" className={cx('skeleton h-4 w-full', className)} />;
}

/** A stack of text-line skeletons with a ragged last line, for list/paragraph loading. */
export function SkeletonLines({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div aria-hidden="true" className={cx('space-y-2.5', className)}>
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} className={i === lines - 1 && lines > 1 ? 'w-2/3' : undefined} />
      ))}
    </div>
  );
}
