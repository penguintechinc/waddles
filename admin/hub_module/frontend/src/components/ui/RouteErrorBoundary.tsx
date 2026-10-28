import type { ReactNode } from 'react';
import { ErrorBoundary, type FallbackProps } from 'react-error-boundary';
import { Button } from './Button';
import { ErrorState } from './ErrorState';

/** Fallback shown when a route throws while rendering (or its lazy chunk fails to load). */
function RouteErrorFallback({ error, resetErrorBoundary }: FallbackProps) {
  console.error('[RouteErrorBoundary] Render failed', { name: error instanceof Error ? error.name : 'unknown' });
  return (
    <div className="mx-auto max-w-lg py-16">
      <ErrorState
        title="This page hit a problem"
        message="Something went wrong while showing this page. Reloading usually fixes it."
        onRetry={resetErrorBoundary}
      />
      <div className="flex justify-center">
        <Button variant="ghost" onClick={() => window.location.reload()}>
          Reload the app
        </Button>
      </div>
    </div>
  );
}

export interface RouteErrorBoundaryProps {
  children: ReactNode;
  /** Changing this value (e.g. the pathname) clears a previous error on navigation. */
  resetKey?: string;
}

/**
 * Catches render-time errors under a route so one broken page never blanks the
 * whole app. Wraps `react-error-boundary` (hooks-era replacement for class boundaries).
 */
export function RouteErrorBoundary({ children, resetKey }: RouteErrorBoundaryProps) {
  return (
    <ErrorBoundary FallbackComponent={RouteErrorFallback} {...(resetKey ? { resetKeys: [resetKey] } : {})}>
      {children}
    </ErrorBoundary>
  );
}
