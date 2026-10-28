import { Suspense } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { LoadingState } from './LoadingState';
import { RouteErrorBoundary } from './RouteErrorBoundary';

/**
 * The single place a layout renders its routed page. Wraps `<Outlet />` in an
 * error boundary (a throwing page never blanks the shell) and a Suspense
 * boundary (lazy route chunks show a loading state inside the layout instead of
 * flashing the whole app). The boundary resets on navigation.
 */
export function RouteOutlet() {
  const { pathname } = useLocation();
  return (
    <RouteErrorBoundary resetKey={pathname}>
      <Suspense fallback={<LoadingState label="Loading page" />}>
        <Outlet />
      </Suspense>
    </RouteErrorBoundary>
  );
}
