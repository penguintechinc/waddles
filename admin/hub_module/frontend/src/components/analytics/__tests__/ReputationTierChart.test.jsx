import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import ReputationTierChart from '../ReputationTierChart';

describe('ReputationTierChart', () => {
  it('renders nothing without data', () => {
    const { container } = render(<ReputationTierChart data={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows empty message when there are no buckets and no stats', () => {
    render(<ReputationTierChart data={{}} />);
    expect(screen.getByText('No reputation data available.')).toBeInTheDocument();
    expect(screen.queryByText('Average')).not.toBeInTheDocument();
  });

  it('renders stats, percentages and tier colors', () => {
    const { container } = render(
      <ReputationTierChart
        data={{
          stats: { avg: 612, median: 600, min: 300, max: 850 },
          buckets: [
            { label: 'Very Good', count: 1500 },
            { label: 'Poor', count: 500 },
            { label: 'Mystery', count: 0 },
          ],
        }}
      />
    );
    expect(screen.getByText('Average')).toBeInTheDocument();
    expect(screen.getByText('612')).toBeInTheDocument();
    expect(screen.getByText('850')).toBeInTheDocument();
    expect(screen.getByText(/1,500/)).toHaveTextContent('(75.0%)');
    expect(screen.getByText(/^500 \(25\.0%\)/)).toBeInTheDocument();
    expect(container.querySelector('.bg-sky-500')).toHaveStyle({ width: '75.0%' });
    expect(container.querySelector('.bg-red-500')).toHaveStyle({ width: '25.0%' });
    expect(container.querySelector('.bg-navy-500')).toHaveStyle({ width: '0.0%' });
  });

  it('shows em-dash for missing stat fields and avoids divide-by-zero', () => {
    render(
      <ReputationTierChart data={{ stats: { avg: 500 }, buckets: [{ label: 'Good', count: 0 }] }} />
    );
    expect(screen.getAllByText('—')).toHaveLength(3);
    expect(screen.getByText(/^0 \(0\.0%\)/)).toBeInTheDocument();
  });

  it('renders stats when only median is defined', () => {
    render(<ReputationTierChart data={{ stats: { median: 650 }, buckets: [] }} />);
    expect(screen.getByText('650')).toBeInTheDocument();
  });
});
