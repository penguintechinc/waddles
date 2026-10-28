import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { DataTable, type Column } from '../DataTable';
import { axeViolations } from '../../../test/axe';

interface Row {
  id: number;
  name: string;
  role: string;
}

const COLUMNS: Column<Row>[] = [
  { key: 'name', header: 'Name', cell: (r) => r.name },
  { key: 'role', header: 'Role', cell: (r) => r.role },
  { key: 'actions', header: 'Actions', hideHeader: true, align: 'right', cell: (r) => <button type="button">Edit {r.name}</button> },
];

const ROWS: Row[] = [
  { id: 1, name: 'Ada', role: 'Admin' },
  { id: 2, name: 'Grace', role: 'Moderator' },
];

describe('DataTable', () => {
  it('renders a captioned table with column headers and rows', () => {
    render(<DataTable caption="Members" columns={COLUMNS} rows={ROWS} getRowId={(r) => r.id} />);
    const table = screen.getByRole('table', { name: 'Members' });
    expect(within(table).getAllByRole('columnheader')).toHaveLength(3);
    expect(within(table).getAllByRole('row')).toHaveLength(3);
    expect(within(table).getByText('Grace')).toBeInTheDocument();
    expect(within(table).getAllByRole('columnheader')[0]).toHaveAttribute('scope', 'col');
  });

  it('keeps hidden headers available to screen readers', () => {
    render(<DataTable caption="Members" columns={COLUMNS} rows={ROWS} getRowId={(r) => r.id} />);
    expect(screen.getByRole('columnheader', { name: 'Actions' })).toBeInTheDocument();
  });

  it('makes the scroll region keyboard-focusable and named', () => {
    render(<DataTable caption="Members" columns={COLUMNS} rows={ROWS} getRowId={(r) => r.id} />);
    const region = screen.getByRole('region', { name: 'Members' });
    expect(region).toHaveAttribute('tabindex', '0');
  });

  it('shows skeleton rows and aria-busy while loading', () => {
    render(<DataTable caption="Members" columns={COLUMNS} rows={undefined} loading getRowId={(r) => r.id} />);
    expect(screen.getByRole('table')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getAllByRole('row')).toHaveLength(6);
  });

  it('shows an error state with a working retry', () => {
    const onRetry = vi.fn();
    render(
      <DataTable
        caption="Members"
        columns={COLUMNS}
        rows={undefined}
        error={new Error('boom')}
        onRetry={onRetry}
        getRowId={(r) => r.id}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('boom');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('shows the empty state with its action when there are no rows', () => {
    render(
      <DataTable
        caption="Members"
        columns={COLUMNS}
        rows={[]}
        getRowId={(r) => r.id}
        emptyTitle="No members yet"
        emptyDescription="Invite someone to get started."
        emptyAction={<button type="button">Invite</button>}
      />,
    );
    expect(screen.getByText('No members yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Invite' })).toBeInTheDocument();
  });

  it('has no axe violations in data, loading, error and empty states', async () => {
    const data = render(<DataTable caption="Members" columns={COLUMNS} rows={ROWS} getRowId={(r) => r.id} />);
    expect(await axeViolations(data.container)).toEqual([]);
    data.unmount();
    const loading = render(<DataTable caption="Members" columns={COLUMNS} rows={undefined} loading getRowId={(r) => r.id} />);
    expect(await axeViolations(loading.container)).toEqual([]);
    loading.unmount();
    const failed = render(<DataTable caption="Members" columns={COLUMNS} rows={undefined} error="nope" getRowId={(r) => r.id} />);
    expect(await axeViolations(failed.container)).toEqual([]);
    failed.unmount();
    const empty = render(<DataTable caption="Members" columns={COLUMNS} rows={[]} getRowId={(r) => r.id} />);
    expect(await axeViolations(empty.container)).toEqual([]);
  });
});
