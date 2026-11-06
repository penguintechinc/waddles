import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import AccessGrantWarning from '../AccessGrantWarning';

const SUMMARY = {
  tableAccess: [
    {
      table: 'community_members',
      columns: ['role'],
      ops: ['read', 'write'],
      reason: 'Mirror Discord role to community role',
    },
  ],
};

describe('AccessGrantWarning', () => {
  it('renders nothing when there is no access grant', () => {
    const { container } = render(<AccessGrantWarning summary={{ egress: [] }} />);
    expect(container).toBeEmptyDOMElement();
    const { container: c2 } = render(<AccessGrantWarning summary={null} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it('shows table, columns, ops and reason in a bold red alert', () => {
    render(<AccessGrantWarning summary={SUMMARY} />);
    const alert = screen.getByTestId('access-grant-warning');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert.className).toContain('border-red-500');
    expect(screen.getByTestId('access-grant-warning-title').className).toContain('text-red-500');
    expect(screen.getByTestId('access-grant-warning-title').className).toContain('font-bold');
    const entry = screen.getByTestId('access-grant-entry');
    expect(entry.className).toContain('text-red-500');
    expect(entry.className).toContain('font-bold');
    expect(entry).toHaveTextContent('community_members');
    expect(entry).toHaveTextContent('columns: role');
    expect(entry).toHaveTextContent('ops: read, write');
    expect(entry).toHaveTextContent('Reason: Mirror Discord role to community role');
  });

  it('lists every entry', () => {
    const two = {
      tableAccess: [
        ...SUMMARY.tableAccess,
        { table: 'community_members', columns: ['is_active'], ops: ['read'], reason: 'r2' },
      ],
    };
    render(<AccessGrantWarning summary={two} />);
    expect(screen.getAllByTestId('access-grant-entry')).toHaveLength(2);
  });

  it('reports the acknowledgement only when requireAck is set', () => {
    const onAck = vi.fn();
    const { rerender } = render(<AccessGrantWarning summary={SUMMARY} />);
    expect(screen.queryByTestId('access-grant-ack')).toBeNull();
    rerender(<AccessGrantWarning summary={SUMMARY} requireAck onAckChange={onAck} />);
    fireEvent.click(screen.getByTestId('access-grant-ack'));
    expect(onAck).toHaveBeenCalledWith(true);
  });
});
