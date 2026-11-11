import type { ReactNode } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { EmptyState } from './EmptyState';
import { ErrorState } from './ErrorState';
import { LoadingState } from './LoadingState';

/** Which visible state a failed panel request maps to. */
export type PanelErrorKind = 'upgrade' | 'unavailable' | 'error';

export interface PanelErrorInfo {
  kind: PanelErrorKind;
  /** HTTP status when the failure was an HTTP response. */
  status?: number;
  /** Server-provided error code (e.g. `service_unavailable`), if any. */
  code?: string;
  /** Server-provided message, if any. Presentation only; never parsed for logic. */
  serverMessage?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function str(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value : undefined;
}

/**
 * Maps a thrown request error to a panel state using only the HTTP status and the
 * response's own error code/message. No plan or tier logic lives here: the server
 * decides what is gated (402) or down (503) and the panel just renders that verdict.
 */
export function classifyPanelError(error: unknown): PanelErrorInfo {
  const response = isRecord(error) ? error['response'] : undefined;
  const status = isRecord(response) && typeof response['status'] === 'number' ? response['status'] : undefined;
  const data = isRecord(response) ? response['data'] : undefined;
  let code: string | undefined;
  let serverMessage: string | undefined;
  if (isRecord(data)) {
    const envelope = data['error'];
    code = str(envelope) ?? (isRecord(envelope) ? str(envelope['code']) : undefined) ?? str(data['code']);
    serverMessage = (isRecord(envelope) ? str(envelope['message']) : undefined) ?? str(data['message']);
  }
  const info = { ...(status !== undefined ? { status } : {}), ...(code ? { code } : {}), ...(serverMessage ? { serverMessage } : {}) };
  if (status === 402) return { kind: 'upgrade', ...info };
  if (status === 503 || code === 'service_unavailable') return { kind: 'unavailable', ...info };
  return { kind: 'error', ...info };
}

export interface PanelErrorStateProps {
  /** Human name of the feature, used in the "<Feature> is temporarily unavailable" text. */
  feature: string;
  error: unknown;
  onRetry?: () => void;
  className?: string;
}

/**
 * Visible failure state for one dashboard panel: 402 -> plan message, 503 -> temporarily
 * unavailable, anything else -> error with retry. Never renders nothing.
 */
export function PanelErrorState({ feature, error, onRetry, className }: PanelErrorStateProps) {
  const info = classifyPanelError(error);
  const retry = onRetry ? { onRetry } : {};
  const cls = className ? { className } : {};
  if (info.kind === 'upgrade') {
    return (
      <div data-testid="panel-upgrade" data-feature={feature}>
        <EmptyState
          title={info.serverMessage ?? 'This feature requires a higher plan for this community'}
          description={info.serverMessage ? undefined : `${feature} is not included in this community's current plan.`}
          testId="panel-upgrade-body"
          {...cls}
        />
      </div>
    );
  }
  if (info.kind === 'unavailable') {
    return (
      <div data-testid="panel-unavailable" data-feature={feature}>
        <ErrorState title={`${feature} is temporarily unavailable`} message="Please try again in a moment." {...retry} {...cls} />
      </div>
    );
  }
  return (
    <div data-testid="panel-error" data-feature={feature}>
      <ErrorState title={`We could not load ${feature}`} error={error} {...retry} {...cls} />
    </div>
  );
}

export interface PanelEmptyAction {
  to: string;
  label: string;
}

export interface PanelStateProps<TData> {
  query: Pick<UseQueryResult<TData>, 'data' | 'isPending' | 'isError' | 'error' | 'refetch'>;
  /** Feature name for messages, e.g. "Live streams". */
  feature: string;
  /** True when the API answered but has nothing to show (nothing configured/connected). */
  isEmpty: (data: TData) => boolean;
  emptyTitle: string;
  emptyDescription?: ReactNode;
  /** Link to the page that fixes the empty state (e.g. connection settings); omit when none applies. */
  emptyAction?: PanelEmptyAction;
  className?: string;
  children: (data: TData) => ReactNode;
}

/**
 * Shared wrapper giving every dashboard panel the same visible states, driven by the API
 * response: loading, 402 plan message, 503 unavailable, other error with retry, empty with
 * a CTA, or the data.
 */
export function PanelState<TData>({
  query,
  feature,
  isEmpty,
  emptyTitle,
  emptyDescription,
  emptyAction,
  className,
  children,
}: PanelStateProps<TData>) {
  if (query.isPending) return <LoadingState label={`Loading ${feature}`} />;
  if (query.isError) {
    return (
      <PanelErrorState
        feature={feature}
        error={query.error}
        onRetry={() => void query.refetch()}
        {...(className ? { className } : {})}
      />
    );
  }
  const data = query.data as TData;
  if (isEmpty(data)) {
    return (
      <div data-testid="panel-empty" data-feature={feature}>
        <EmptyState
          title={emptyTitle}
          headingLevel={4}
          testId="panel-empty-body"
          {...(emptyDescription ? { description: emptyDescription } : {})}
          {...(className ? { className } : {})}
          {...(emptyAction
            ? {
                action: (
                  <Link to={emptyAction.to} className="btn btn-primary">
                    {emptyAction.label}
                  </Link>
                ),
              }
            : {})}
        />
      </div>
    );
  }
  return <>{children(data)}</>;
}
