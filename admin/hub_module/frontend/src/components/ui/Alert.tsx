import type { ReactNode } from 'react';
import {
  CheckCircleIcon,
  ExclamationCircleIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { cx } from './cx';
import { IconButton } from './IconButton';

export type AlertTone = 'success' | 'warning' | 'danger' | 'info';

export interface AlertProps {
  tone?: AlertTone;
  title?: ReactNode;
  children?: ReactNode;
  /** When provided a dismiss button (with an accessible name) is rendered. */
  onDismiss?: () => void;
  className?: string;
}

const TONE: Record<AlertTone, { cls: string; Icon: typeof CheckCircleIcon }> = {
  success: { cls: 'alert-success', Icon: CheckCircleIcon },
  warning: { cls: 'alert-warning', Icon: ExclamationTriangleIcon },
  danger: { cls: 'alert-danger', Icon: ExclamationCircleIcon },
  info: { cls: 'alert-info', Icon: InformationCircleIcon },
};

/**
 * Inline message banner. Danger/warning use `role="alert"` (interrupts) while
 * success/info use `role="status"` (polite), matching how urgent each is.
 */
export function Alert({ tone = 'info', title, children, onDismiss, className }: AlertProps) {
  const { cls, Icon } = TONE[tone];
  const urgent = tone === 'danger' || tone === 'warning';
  return (
    <div role={urgent ? 'alert' : 'status'} className={cx('alert', cls, className)}>
      <Icon aria-hidden="true" className="mt-0.5 h-5 w-5 shrink-0" />
      <div className="min-w-0 flex-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children ? <div className={cx(title ? 'mt-0.5' : undefined, 'text-fg-secondary')}>{children}</div> : null}
      </div>
      {onDismiss ? (
        <IconButton label="Dismiss message" icon={<XMarkIcon />} onClick={onDismiss} className="-my-2 -mr-2 h-8 w-8" />
      ) : null}
    </div>
  );
}
