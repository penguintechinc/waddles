/**
 * Tests for the bot-detection score widgets: the grade badge (colour, size,
 * tooltip, placeholder) and the dashboard card (load / error / empty states,
 * score bar, premium link vs upgrade nudge).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import BotScoreBadge from '../BotScoreBadge';
import BotScoreCard from '../BotScoreCard';
import { adminApi } from '../../services/api';

vi.mock('../../services/api', () => ({
  adminApi: { getBotScore: vi.fn() },
}));

describe('BotScoreBadge', () => {
  it('renders a neutral placeholder when there is no grade', () => {
    render(<BotScoreBadge grade={null} className="extra" />);
    const badge = screen.getByTitle('No bot score data');
    expect(badge).toHaveTextContent('—');
    expect(badge).toHaveClass('bg-gray-300', 'extra');
  });

  it.each([
    ['A', 'bg-emerald-500', 'Excellent - Very low bot activity'],
    ['B', 'bg-green-500', 'Good - Minimal bot activity'],
    ['C', 'bg-sky-500', 'Fair - Some bot activity detected'],
    ['D', 'bg-yellow-500', 'Poor - Significant bot activity'],
    ['F', 'bg-red-500', 'Critical - High bot activity'],
  ])('grade %s gets its colour and tooltip', (grade, colour, tooltip) => {
    render(<BotScoreBadge grade={grade} />);
    const badge = screen.getByText(grade);
    expect(badge).toHaveClass(colour);
    expect(badge).toHaveAttribute('title', tooltip);
  });

  it('appends the rounded score to the tooltip only when showScore is set', () => {
    const { rerender } = render(<BotScoreBadge grade="B" score={87.6} showScore />);
    expect(screen.getByText('B')).toHaveAttribute('title', 'Good - Minimal bot activity (88/100)');

    rerender(<BotScoreBadge grade="B" score={87.6} />);
    expect(screen.getByText('B')).toHaveAttribute('title', 'Good - Minimal bot activity');

    rerender(<BotScoreBadge grade="B" showScore />);
    expect(screen.getByText('B')).toHaveAttribute('title', 'Good - Minimal bot activity');
  });

  it.each([
    ['sm', 'text-xs'],
    ['md', 'text-sm'],
    ['lg', 'text-base'],
    ['huge', 'text-sm'],
  ])('size %s maps to %s (unknown sizes fall back to md)', (size, cls) => {
    render(<BotScoreBadge grade="A" size={size} />);
    expect(screen.getByText('A')).toHaveClass(cls);
  });

  it('falls back to the C colour and an empty tooltip for an unknown grade', () => {
    render(<BotScoreBadge grade="Z" />);
    const badge = screen.getByText('Z');
    expect(badge).toHaveClass('bg-sky-500');
    expect(badge).toHaveAttribute('title', '');
  });
});

describe('BotScoreCard', () => {
  const DATA = {
    score: 87.4,
    grade: 'A',
    suspectedBotCount: 12,
    highConfidenceBotCount: 3,
    lastCalculatedAt: '2026-03-04T10:30:00Z',
  };

  function mount(props = {}) {
    return render(
      <MemoryRouter>
        <BotScoreCard communityId={5} {...props} />
      </MemoryRouter>,
    );
  }

  beforeEach(() => {
    vi.clearAllMocks();
    adminApi.getBotScore.mockResolvedValue({ data: { success: true, data: DATA } });
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows a spinner first, then the score, bar, counts and timestamp', async () => {
    const { container } = mount();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();

    expect(await screen.findByText('87/100')).toBeInTheDocument();
    expect(adminApi.getBotScore).toHaveBeenCalledWith(5);
    expect(screen.getByText('Bot Detection Score')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(container.querySelector('.h-full')).toHaveStyle({ width: '87%' });
    expect(container.querySelector('.h-full')).toHaveClass('bg-emerald-500');
    const expected = new Date(DATA.lastCalculatedAt).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
    expect(screen.getByText(`Last calculated: ${expected}`)).toBeInTheDocument();
  });

  it.each([
    ['B', 'bg-green-500'],
    ['C', 'bg-sky-500'],
    ['D', 'bg-yellow-500'],
    ['F', 'bg-red-500'],
    ['?', 'bg-navy-700'],
  ])('colours the score bar for grade %s as %s', async (grade, cls) => {
    adminApi.getBotScore.mockResolvedValue({ data: { success: true, data: { ...DATA, grade } } });
    const { container } = mount();
    await screen.findByText('87/100');
    expect(container.querySelector('.h-full')).toHaveClass(cls);
  });

  it('links to the details page for premium communities', async () => {
    mount({ isPremium: true });
    const link = await screen.findByRole('link', { name: /View Details/ });
    expect(link).toHaveAttribute('href', '/admin/5/bot-detection');
    expect(screen.queryByText('Upgrade for detailed analysis')).not.toBeInTheDocument();
  });

  it('nudges non-premium communities to upgrade', async () => {
    mount();
    expect(await screen.findByText('Upgrade for detailed analysis')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('shows dashes and no bar when the API reports no data', async () => {
    adminApi.getBotScore.mockResolvedValue({ data: { success: false } });
    const { container } = mount();
    await waitFor(() => expect(screen.getAllByText('-')).toHaveLength(2));
    expect(container.querySelector('.h-full')).not.toBeInTheDocument();
    expect(screen.queryByText(/Last calculated/)).not.toBeInTheDocument();
  });

  it('renders zero percent when the payload has no score', async () => {
    adminApi.getBotScore.mockResolvedValue({
      data: { success: true, data: { grade: 'C', suspectedBotCount: 0, highConfidenceBotCount: 0 } },
    });
    const { container } = mount();
    await screen.findByText('0/100');
    expect(container.querySelector('.h-full')).toHaveStyle({ width: '0%' });
    expect(screen.getAllByText('0').length).toBeGreaterThanOrEqual(2);
  });

  it('shows an error message and logs when the request fails', async () => {
    adminApi.getBotScore.mockRejectedValue(new Error('boom'));
    mount();
    expect(await screen.findByText('Failed to load bot score')).toBeInTheDocument();
    expect(console.error).toHaveBeenCalledWith('Bot score error:', expect.any(Error));
  });

  it('refetches when the community changes', async () => {
    const { rerender } = mount();
    await screen.findByText('87/100');
    rerender(
      <MemoryRouter>
        <BotScoreCard communityId={9} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(adminApi.getBotScore).toHaveBeenCalledWith(9));
  });
});
