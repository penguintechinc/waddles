import type { ReactNode } from 'react';
import { EmptyState } from './EmptyState';

export interface UpgradeNoticeProps {
  /** Human name of the gated feature, e.g. "Polls". Used when the server sent no message. */
  feature: string;
  /** The API's own 402 message, when it sent one. Presentation only; never parsed for logic. */
  serverMessage?: string | undefined;
  /** Optional next step (a link to plan/allocation settings); omitted when none applies. */
  action?: ReactNode;
  className?: string;
}

/**
 * The one friendly "Upgrade to Premium" state shown wherever hub-api answers a licensed-feature 402
 * (`error.code === 'FEATURE_NOT_ENABLED'`). A gated widget or page renders this instead of an error
 * or a blank/broken panel; the server stays the only authority on what is gated.
 */
export function UpgradeNotice({ feature, serverMessage, action, className }: UpgradeNoticeProps) {
  return (
    <div data-testid="panel-upgrade" data-feature={feature}>
      <EmptyState
        title="Upgrade to Premium for this feature"
        description={serverMessage ?? `${feature} is not included in this community's current plan.`}
        testId="upgrade-notice"
        {...(action ? { action } : {})}
        {...(className ? { className } : {})}
      />
    </div>
  );
}

/** True when a thrown request error is a licensed-feature 402 (status only; the body is never parsed). */
export function isLicensedFeatureError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false;
  const response = (error as { response?: unknown }).response;
  return typeof response === 'object' && response !== null && (response as { status?: unknown }).status === 402;
}
