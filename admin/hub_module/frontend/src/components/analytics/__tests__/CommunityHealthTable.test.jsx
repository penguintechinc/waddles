/**
 * Tests for the platform-admin community health table: empty state, row
 * formatting, health/bot-grade badge colours, and click-to-sort behaviour.
 */
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

import CommunityHealthTable from '../CommunityHealthTable';

const DATA = [
  { id: 1, name: 'Alpha', member_count: 1234, health_score: 80, bot_score_grade: 'a' },
  { id: 2, name: 'Bravo', member_count: 50, health_score: 60, bot_score_grade: 'C' },
  { id: 3, name: 'Charlie', member_count: 9000, health_score: 20, bot_score_grade: 'f' },
  { id: 4, name: null, member_count: null, health_score: null, bot_score_grade: null },
];

function names() {
  return within(screen.getByRole('table'))
    .getAllByRole('row')
    .slice(1)
    .map((row) => within(row).getAllByRole('cell')[0].textContent);
}

describe('CommunityHealthTable', () => {
  it.each([
    ['null', null],
    ['undefined', undefined],
    ['an empty array', []],
  ])('shows the empty state for %s data', (_label, data) => {
    render(<CommunityHealthTable data={data} />);
    expect(screen.getByText('No community health data available.')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('formats members, falls back for missing values and names unnamed communities', () => {
    render(<CommunityHealthTable data={DATA} />);
    expect(screen.getByText((1234).toLocaleString())).toBeInTheDocument();
    expect(screen.getByText('Community #4')).toBeInTheDocument();
    const missing = within(screen.getByText('Community #4').closest('tr')).getAllByText('—');
    expect(missing).toHaveLength(3);
  });

  it('colours health scores by threshold and uppercases bot grades', () => {
    render(<CommunityHealthTable data={DATA} />);
    expect(screen.getByText('80')).toHaveClass('text-emerald-300');
    expect(screen.getByText('60')).toHaveClass('text-gold-300');
    expect(screen.getByText('20')).toHaveClass('text-red-300');
    expect(screen.getByText('A')).toHaveClass('text-emerald-300');
    expect(screen.getByText('C')).toHaveClass('text-gold-300');
    expect(screen.getByText('F')).toHaveClass('text-red-300');
  });

  it.each([
    ['B', 'text-sky-300'],
    ['D', 'text-orange-300'],
  ])('colours bot grade %s with %s', (grade, cls) => {
    render(<CommunityHealthTable data={[{ id: 9, name: 'X', member_count: 1, health_score: 75, bot_score_grade: grade }]} />);
    expect(screen.getByText(grade)).toHaveClass(cls);
    expect(screen.getByText('75')).toHaveClass('text-emerald-300');
  });

  it('colours a score of exactly 50 as mid-range', () => {
    render(<CommunityHealthTable data={[{ id: 9, name: 'X', member_count: 1, health_score: 50, bot_score_grade: 'A' }]} />);
    expect(screen.getByText('50')).toHaveClass('text-gold-300');
  });

  it('sorts by health descending by default, with missing scores last', () => {
    render(<CommunityHealthTable data={DATA} />);
    expect(names()).toEqual(['Alpha', 'Bravo', 'Charlie', 'Community #4']);
  });

  it('toggles direction on a repeat click of the same column', () => {
    render(<CommunityHealthTable data={DATA} />);
    fireEvent.click(screen.getByText(/^Health/));
    expect(names()).toEqual(['Charlie', 'Bravo', 'Alpha', 'Community #4']);
    fireEvent.click(screen.getByText(/^Health/));
    expect(names()).toEqual(['Alpha', 'Bravo', 'Charlie', 'Community #4']);
  });

  it('switches column with a descending sort first', () => {
    render(<CommunityHealthTable data={DATA} />);
    fireEvent.click(screen.getByText(/^Members/));
    expect(names()).toEqual(['Charlie', 'Alpha', 'Bravo', 'Community #4']);
    fireEvent.click(screen.getByText(/^Community$/));
    expect(names()[0]).toBe('Charlie');
    expect(names()).toEqual(['Charlie', 'Bravo', 'Alpha', 'Community #4']);
  });

  it('keeps stable order for ties and sorts by bot grade', () => {
    const tied = [
      { id: 1, name: 'One', member_count: 5, health_score: 10, bot_score_grade: 'B' },
      { id: 2, name: 'Two', member_count: 5, health_score: 10, bot_score_grade: 'A' },
    ];
    render(<CommunityHealthTable data={tied} />);
    expect(names()).toEqual(['One', 'Two']);
    fireEvent.click(screen.getByText(/^Bot Grade/));
    expect(names()).toEqual(['One', 'Two']);
    fireEvent.click(screen.getByText(/^Bot Grade/));
    expect(names()).toEqual(['Two', 'One']);
  });

  it('shows the sort arrow on the active column only', () => {
    render(<CommunityHealthTable data={DATA} />);
    expect(screen.getByText('↓')).toBeInTheDocument();
    expect(screen.getAllByText('↕')).toHaveLength(3);
    fireEvent.click(screen.getByText(/^Health/));
    expect(screen.getByText('↑')).toBeInTheDocument();
  });
});
