/**
 * HandsQueue / useHandAnnouncements: ordering, per-hand actions, acknowledged state and the polite live
 * region that announces raised/lowered hands (but not the initial load).
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { HandsQueue } from '../HandsQueue';
import type { RaisedHand } from '../../../services/callsApi';

const hand = (id: string, name: string, raisedAt: number, acknowledgedAt: number | null = null): RaisedHand => ({
  participantId: id,
  displayName: name,
  raisedAt,
  acknowledgedAt,
  acknowledgedBy: acknowledgedAt === null ? null : 'mod',
});

const noop = () => undefined;

describe('HandsQueue', () => {
  it('shows an empty message and no clear button when nobody has a hand up', () => {
    render(<HandsQueue hands={[]} onAcknowledge={noop} onReset={noop} />);
    expect(screen.getByText('No raised hands.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear all hands' })).not.toBeInTheDocument();
  });

  it('renders the queue in the given order with positions and acknowledge/clear actions', () => {
    const onAcknowledge = vi.fn();
    const onReset = vi.fn();
    render(<HandsQueue hands={[hand('a', 'Ada', 10), hand('b', 'Grace', 20, 25)]} onAcknowledge={onAcknowledge} onReset={onReset} />);
    const items = screen.getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('1.Ada');
    expect(items[1]).toHaveTextContent('2.Grace');
    expect(items[1]).toHaveTextContent('Acknowledged');
    expect(screen.queryByRole('button', { name: 'Acknowledge Grace' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge Ada' }));
    expect(onAcknowledge).toHaveBeenCalledWith('a');
    fireEvent.click(screen.getByRole('button', { name: 'Clear all hands' }));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it('announces changes politely, not the first snapshot', () => {
    const { rerender } = render(<HandsQueue hands={[hand('a', 'Ada', 10)]} onAcknowledge={noop} onReset={noop} />);
    const live = screen.getByTestId('hands-live-region');
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live).toHaveAttribute('role', 'status');
    expect(live).toBeEmptyDOMElement();

    rerender(<HandsQueue hands={[hand('a', 'Ada', 10), hand('b', 'Grace', 20)]} onAcknowledge={noop} onReset={noop} />);
    expect(live).toHaveTextContent('Grace raised a hand. 2 hands raised.');

    rerender(<HandsQueue hands={[hand('b', 'Grace', 20)]} onAcknowledge={noop} onReset={noop} />);
    expect(live).toHaveTextContent('Ada lowered a hand. 1 hand raised.');

    rerender(<HandsQueue hands={[hand('c', 'Linus', 30)]} onAcknowledge={noop} onReset={noop} />);
    expect(live).toHaveTextContent('Linus raised a hand. Grace lowered a hand. 1 hand raised.');
  });
});
