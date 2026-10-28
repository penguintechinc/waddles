import type { ReactNode } from 'react';
import { cx } from './cx';
import { EmptyState } from './EmptyState';
import { ErrorState } from './ErrorState';
import { Skeleton } from './Skeleton';

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  align?: 'left' | 'right' | 'center';
  className?: string;
  /** Visually hide the header text (e.g. an actions column) while keeping it for screen readers. */
  hideHeader?: boolean;
}

export interface DataTableProps<T> {
  /** Names the table for assistive tech and the scroll region. Required. */
  caption: string;
  columns: readonly Column<T>[];
  rows: readonly T[] | undefined;
  getRowId: (row: T) => string | number;
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  emptyTitle?: string;
  emptyDescription?: ReactNode;
  emptyAction?: ReactNode;
  className?: string;
}

const ALIGN: Record<NonNullable<Column<unknown>['align']>, string> = {
  left: 'text-left',
  right: 'text-right',
  center: 'text-center',
};

/**
 * Accessible data table with first-class loading, error and empty states.
 * Renders a captioned `<table>` with `scope="col"` headers inside a keyboard-focusable
 * scroll region so wide tables stay usable at 320px. Row interaction belongs in a
 * cell (a link or button), never on the `<tr>`.
 */
export function DataTable<T>({
  caption,
  columns,
  rows,
  getRowId,
  loading = false,
  error,
  onRetry,
  emptyTitle = 'Nothing to show yet',
  emptyDescription,
  emptyAction,
  className,
}: DataTableProps<T>) {
  const showError = error !== undefined && error !== null && !loading;
  const showEmpty = !loading && !showError && (rows?.length ?? 0) === 0;

  return (
    <div className={cx('card overflow-hidden', className)}>
      {showError ? (
        <ErrorState error={error} {...(onRetry ? { onRetry } : {})} />
      ) : showEmpty ? (
        <EmptyState
          title={emptyTitle}
          {...(emptyDescription ? { description: emptyDescription } : {})}
          {...(emptyAction ? { action: emptyAction } : {})}
        />
      ) : (
        <div
          role="region"
          aria-label={caption}
          // tabIndex makes the scrollable region keyboard-reachable (axe: scrollable-region-focusable)
          tabIndex={0}
          className="overflow-x-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus"
        >
          <table aria-busy={loading || undefined}>
            <caption className="sr-only">{caption}</caption>
            <thead>
              <tr>
                {columns.map((col) => (
                  <th key={col.key} scope="col" className={cx(ALIGN[col.align ?? 'left'], col.className)}>
                    {col.hideHeader ? <span className="sr-only">{col.header}</span> : col.header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading
                ? Array.from({ length: 5 }, (_, r) => (
                    <tr key={`skeleton-${r}`}>
                      {columns.map((col) => (
                        <td key={col.key}>
                          <Skeleton className="h-4" />
                        </td>
                      ))}
                    </tr>
                  ))
                : (rows ?? []).map((row) => (
                    <tr key={getRowId(row)}>
                      {columns.map((col) => (
                        <td key={col.key} className={cx(ALIGN[col.align ?? 'left'], col.className)}>
                          {col.cell(row)}
                        </td>
                      ))}
                    </tr>
                  ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
