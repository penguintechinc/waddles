import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PanelState, classifyPanelError } from '../PanelState';

const httpErr = (status: number, data?: unknown) => ({ response: { status, data } });

function setup(query: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  return render(
    <MemoryRouter>
      <PanelState
        query={{ data: undefined, isPending: false, isError: false, error: null, refetch: vi.fn(), ...query } as never}
        feature="Live streams"
        isEmpty={(d: string[]) => d.length === 0}
        emptyTitle="No streams configured for this community"
        {...extra}
      >
        {(d: string[]) => <p>{d.join(',')}</p>}
      </PanelState>
    </MemoryRouter>,
  );
}

describe('classifyPanelError', () => {
  it('maps 402, 503 / service_unavailable and others by status and code only', () => {
    expect(classifyPanelError(httpErr(402)).kind).toBe('upgrade');
    expect(classifyPanelError(httpErr(503, { error: 'service_unavailable' })).kind).toBe('unavailable');
    expect(classifyPanelError(httpErr(500, { error: { code: 'service_unavailable', message: 'x' } })).kind).toBe('unavailable');
    expect(classifyPanelError(httpErr(500)).kind).toBe('error');
    expect(classifyPanelError(new Error('boom')).kind).toBe('error');
    expect(classifyPanelError(null).kind).toBe('error');
  });
});

describe('PanelState', () => {
  it('shows loading', () => {
    setup({ isPending: true });
    expect(screen.getByTestId('loading-state')).toBeInTheDocument();
  });

  it('shows empty state with a connect CTA', () => {
    setup({ data: [] }, { emptyAction: { to: '/admin/1/connections', label: 'Connect Twitch' } });
    expect(screen.getByText('No streams configured for this community')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Connect Twitch' })).toHaveAttribute('href', '/admin/1/connections');
  });

  it('shows empty state without a CTA when none applies', () => {
    setup({ data: [] });
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('shows the plan message on 402', () => {
    setup({ isError: true, error: httpErr(402) });
    expect(screen.getByText('This feature requires a higher plan for this community')).toBeInTheDocument();
  });

  it('prefers the server message on 402', () => {
    setup({ isError: true, error: httpErr(402, { error: { code: 'plan_required', message: 'Upgrade to Pro' } }) });
    expect(screen.getByText('Upgrade to Pro')).toBeInTheDocument();
  });

  it('shows temporarily unavailable on 503 with retry', () => {
    const refetch = vi.fn();
    setup({ isError: true, error: httpErr(503, { error: 'service_unavailable' }), refetch });
    expect(screen.getByText('Live streams is temporarily unavailable')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refetch).toHaveBeenCalled();
  });

  it('shows a visible error with retry for other failures', () => {
    const refetch = vi.fn();
    setup({ isError: true, error: httpErr(500, { error: 'nope' }), refetch });
    expect(screen.getByText('We could not load Live streams')).toBeInTheDocument();
    expect(screen.getByText('nope')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refetch).toHaveBeenCalled();
  });

  it('renders children with data', () => {
    setup({ data: ['a', 'b'] });
    expect(screen.getByText('a,b')).toBeInTheDocument();
  });
});
