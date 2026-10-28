import type { ReactNode } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';
import { EmptyState } from './EmptyState';
import { ErrorState } from './ErrorState';
import { LoadingState } from './LoadingState';

export interface QueryBoundaryProps<TData> {
  query: Pick<UseQueryResult<TData>, 'data' | 'isPending' | 'isError' | 'error' | 'refetch'>;
  /** Return true when `data` is present but has nothing to show. */
  isEmpty?: (data: TData) => boolean;
  /** Rendered instead of the default empty state. */
  empty?: ReactNode;
  loadingLabel?: string;
  errorTitle?: string;
  /** Receives the resolved, non-empty data. */
  children: (data: TData) => ReactNode;
}

/**
 * Renders the right state for a TanStack Query result: loading, error (with
 * retry), empty, or the data. One component so every page handles all four the
 * same way instead of forgetting the unhappy paths.
 */
export function QueryBoundary<TData>({
  query,
  isEmpty,
  empty,
  loadingLabel,
  errorTitle,
  children,
}: QueryBoundaryProps<TData>) {
  if (query.isPending) return <LoadingState {...(loadingLabel ? { label: loadingLabel } : {})} />;
  if (query.isError) {
    return (
      <ErrorState
        error={query.error}
        {...(errorTitle ? { title: errorTitle } : {})}
        onRetry={() => void query.refetch()}
      />
    );
  }
  const data = query.data as TData;
  if (isEmpty?.(data)) return <>{empty ?? <EmptyState title="Nothing here yet" />}</>;
  return <>{children(data)}</>;
}
