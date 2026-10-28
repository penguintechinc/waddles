import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryBoundary } from '../QueryBoundary';
import { Alert } from '../Alert';
import { Badge } from '../Badge';
import { Card, CardBody, CardHeader } from '../Card';
import { EmptyState } from '../EmptyState';
import { ErrorState } from '../ErrorState';
import { LoadingState } from '../LoadingState';
import { PageHeader } from '../PageHeader';
import { RouteErrorBoundary } from '../RouteErrorBoundary';
import { SkipLink } from '../SkipLink';
import { MAIN_CONTENT_ID } from '../constants';
import { Skeleton, SkeletonLines } from '../Skeleton';
import { Spinner } from '../Spinner';
import { getErrorMessage } from '../errors';
import { cx } from '../cx';
import { axeViolations } from '../../../test/axe';

describe('getErrorMessage', () => {
  it('prefers the hub-api body error, then message, then detail', () => {
    expect(getErrorMessage({ response: { data: { error: 'Nope' } } })).toBe('Nope');
    expect(getErrorMessage({ response: { data: { message: 'Hmm' } } })).toBe('Hmm');
    expect(getErrorMessage({ response: { data: { detail: 'Why' } } })).toBe('Why');
  });

  it('reads the nested hub-api error_response envelope', () => {
    const envelope = { response: { status: 409, data: { success: false, error: { code: 'CONFLICT', message: 'Already a member' } } } };
    expect(getErrorMessage(envelope)).toBe('Already a member');
    // A nested error with no message still falls through to the status mapping.
    expect(getErrorMessage({ response: { status: 403, data: { error: { code: 'X' } } } })).toMatch(/permission/i);
  });

  it('maps 403/404 without a body to friendly text', () => {
    expect(getErrorMessage({ response: { status: 403 } })).toMatch(/permission/i);
    expect(getErrorMessage({ response: { status: 404 } })).toMatch(/could not find/i);
  });

  it('falls back to Error.message, strings and the default', () => {
    expect(getErrorMessage(new Error('boom'))).toBe('boom');
    expect(getErrorMessage('plain')).toBe('plain');
    expect(getErrorMessage(undefined)).toMatch(/something went wrong/i);
    expect(getErrorMessage({}, 'custom')).toBe('custom');
    expect(getErrorMessage('   ', 'blank')).toBe('blank');
  });
});

describe('cx', () => {
  it('drops falsy fragments', () => {
    expect(cx('a', false, null, undefined, 'b')).toBe('a b');
  });
});

describe('Spinner / LoadingState / Skeleton', () => {
  it('announces loading through a status region', () => {
    render(<Spinner label="Fetching bundles" />);
    expect(screen.getByRole('status')).toHaveTextContent('Fetching bundles');
  });

  it('LoadingState shows a caption and one status region', () => {
    render(<LoadingState label="Loading members" fullPage />);
    expect(screen.getAllByRole('status')).toHaveLength(1);
    expect(screen.getByText('Loading members…')).toBeInTheDocument();
  });

  it('skeletons are hidden from assistive tech', () => {
    const { container } = render(
      <>
        <Skeleton />
        <SkeletonLines lines={3} />
      </>,
    );
    expect(container.querySelectorAll('[aria-hidden="true"]').length).toBeGreaterThan(0);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});

describe('Alert', () => {
  it('uses role=alert for urgent tones and role=status otherwise', () => {
    const { rerender } = render(<Alert tone="danger">Failed</Alert>);
    expect(screen.getByRole('alert')).toHaveTextContent('Failed');
    rerender(<Alert tone="warning">Careful</Alert>);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    rerender(<Alert tone="success">Saved</Alert>);
    expect(screen.getByRole('status')).toHaveTextContent('Saved');
    rerender(<Alert>FYI</Alert>);
    expect(screen.getByRole('status')).toHaveTextContent('FYI');
  });

  it('renders an accessible dismiss button only when dismissible', () => {
    const onDismiss = vi.fn();
    const { rerender } = render(<Alert title="Done">All good</Alert>);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    rerender(
      <Alert title="Done" onDismiss={onDismiss}>
        All good
      </Alert>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss message' }));
    expect(onDismiss).toHaveBeenCalled();
  });
});

describe('EmptyState / ErrorState', () => {
  it('EmptyState renders title, description and action', () => {
    render(<EmptyState title="No bundles" description="Install one." action={<button type="button">Browse</button>} icon={<svg />} />);
    expect(screen.getByRole('heading', { name: 'No bundles' })).toBeInTheDocument();
    expect(screen.getByText('Install one.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Browse' })).toBeInTheDocument();
  });

  it('ErrorState derives its message from the error and offers retry', () => {
    const onRetry = vi.fn();
    render(<ErrorState error={{ response: { data: { error: 'Quota exceeded' } } }} onRetry={onRetry} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Quota exceeded');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('ErrorState prefers an explicit message and hides retry when none is given', () => {
    render(<ErrorState message="Custom copy" title="Oops" />);
    expect(screen.getByText('Custom copy')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('has no axe violations', async () => {
    const { container } = render(
      <>
        <EmptyState title="Empty" description="Nothing" />
        <ErrorState error="bad" onRetry={() => undefined} />
        <LoadingState />
        <Alert tone="danger" title="Bad" onDismiss={() => undefined}>
          Details
        </Alert>
      </>,
    );
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe('Card / Badge', () => {
  it('Card renders header (with configurable heading level), body and actions', () => {
    render(
      <Card as="section" aria-label="Sync">
        <CardHeader title="Role sync" description="Mirror roles" headingLevel={3} actions={<button type="button">Edit</button>} />
        <CardBody>Body</CardBody>
      </Card>,
    );
    expect(screen.getByRole('region', { name: 'Sync' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 3, name: 'Role sync' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
  });

  it('Card supports the interactive affordance class', () => {
    render(
      <Card interactive data-testid="c">
        x
      </Card>,
    );
    expect(screen.getByTestId('c')).toHaveClass('card', 'card-interactive');
  });

  it('Badge maps tone to class', () => {
    render(<Badge tone="green">Active</Badge>);
    expect(screen.getByText('Active')).toHaveClass('badge', 'badge-green');
  });
});

describe('PageHeader / SkipLink', () => {
  it('renders a single h1, description, actions and a breadcrumb trail', () => {
    render(
      <MemoryRouter>
        <PageHeader
          title="Bundles"
          description="Manage apps"
          actions={<button type="button">Add</button>}
          breadcrumbs={[{ label: 'Admin', to: '/admin/1' }, { label: 'Bundles' }]}
        />
      </MemoryRouter>,
    );
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Admin' })).toHaveAttribute('href', '/admin/1');
    expect(screen.getByText('Bundles', { selector: '[aria-current="page"]' })).toBeInTheDocument();
  });

  it('omits the breadcrumb nav when there are no crumbs', () => {
    render(
      <MemoryRouter>
        <PageHeader title="Home" />
      </MemoryRouter>,
    );
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });

  it('SkipLink targets the main landmark id', () => {
    render(<SkipLink />);
    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute('href', `#${MAIN_CONTENT_ID}`);
  });
});

describe('QueryBoundary', () => {
  const base = { refetch: vi.fn() };

  it('shows loading while pending', () => {
    render(
      <QueryBoundary query={{ ...base, data: undefined, isPending: true, isError: false, error: null } as never} loadingLabel="Loading apps">
        {() => <p>never</p>}
      </QueryBoundary>,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Loading apps');
  });

  it('shows an error with retry wired to refetch', () => {
    const refetch = vi.fn();
    render(
      <QueryBoundary query={{ refetch, data: undefined, isPending: false, isError: true, error: new Error('down') } as never} errorTitle="Apps unavailable">
        {() => <p>never</p>}
      </QueryBoundary>,
    );
    expect(screen.getByText('Apps unavailable')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('down');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(refetch).toHaveBeenCalled();
  });

  it('shows the empty node when isEmpty says so, default otherwise', () => {
    const query = { ...base, data: [] as string[], isPending: false, isError: false, error: null } as never;
    const { rerender } = render(
      <QueryBoundary query={query} isEmpty={(d: string[]) => d.length === 0} empty={<p>Custom empty</p>}>
        {() => <p>never</p>}
      </QueryBoundary>,
    );
    expect(screen.getByText('Custom empty')).toBeInTheDocument();
    rerender(
      <QueryBoundary query={query} isEmpty={(d: string[]) => d.length === 0}>
        {() => <p>never</p>}
      </QueryBoundary>,
    );
    expect(screen.getByText('Nothing here yet')).toBeInTheDocument();
  });

  it('renders children with the data', () => {
    render(
      <QueryBoundary query={{ ...base, data: ['a', 'b'], isPending: false, isError: false, error: null } as never}>
        {(d: string[]) => <p>{d.join(',')}</p>}
      </QueryBoundary>,
    );
    expect(screen.getByText('a,b')).toBeInTheDocument();
  });
});

describe('RouteErrorBoundary', () => {
  function Boom(): never {
    throw new Error('render failed');
  }

  it('contains a render error and offers recovery', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(
      <RouteErrorBoundary resetKey="/a">
        <Boom />
      </RouteErrorBoundary>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(/hit a problem/i);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reload the app' })).toBeInTheDocument();
    spy.mockRestore();
  });

  it('renders children when nothing throws', () => {
    render(
      <RouteErrorBoundary>
        <p>fine</p>
      </RouteErrorBoundary>,
    );
    expect(screen.getByText('fine')).toBeInTheDocument();
  });
});
