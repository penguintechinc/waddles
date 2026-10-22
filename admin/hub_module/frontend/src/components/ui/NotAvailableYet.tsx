import type { ReactNode } from 'react';
import { findUnservedFeature, isFeatureServed } from '../../lib/unservedFeatures';
import { EmptyState } from './EmptyState';

export interface NotAvailableYetProps {
  /** Key in `UNSERVED_FEATURES`; supplies the label and the missing endpoints. */
  feature: string;
  /** Overrides the default explanation. */
  description?: ReactNode;
  className?: string;
}

/**
 * Visible "not available yet" state for a feature whose hub-api route has not been
 * ported. Honest by design: it says the feature is unavailable (not broken), and lists
 * the missing endpoints for whoever is picking up the backend port.
 */
export function NotAvailableYet({ feature, description, className }: NotAvailableYetProps) {
  const entry = findUnservedFeature(feature);
  const label = entry?.label ?? feature;
  return (
    <div data-testid="not-available-yet" data-feature={feature}>
      <EmptyState
        testId="not-available-yet-body"
        title={`${label} isn't available yet`}
        description={
          description ?? (
            <>
              This part of the platform is still being moved to the new server API. Nothing is wrong on your
              side; it will appear here once the server supports it.
            </>
          )
        }
        {...(className ? { className } : {})}
        action={
          entry ? (
            <details className="max-w-md text-left text-xs text-fg-muted">
              <summary className="cursor-pointer rounded text-fg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus">
                Missing server endpoints
              </summary>
              <ul className="mt-2 space-y-1 font-mono">
                {entry.endpoints.map((endpoint) => (
                  <li key={endpoint}>{endpoint}</li>
                ))}
              </ul>
            </details>
          ) : undefined
        }
      />
    </div>
  );
}

export interface ServedFeatureProps {
  /** Key in `UNSERVED_FEATURES`. */
  feature: string;
  children: ReactNode;
}

/**
 * Renders `children` when hub-api serves `feature`, otherwise `NotAvailableYet`.
 * Removing the feature from `UNSERVED_FEATURES` is all it takes to switch the page on.
 */
export function ServedFeature({ feature, children }: ServedFeatureProps) {
  return isFeatureServed(feature) ? <>{children}</> : <NotAvailableYet feature={feature} />;
}
