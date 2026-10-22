import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { NotAvailableYet, ServedFeature } from '../NotAvailableYet';
import { UNSERVED_FEATURES, findUnservedFeature, isFeatureServed } from '../../../lib/unservedFeatures';
import { axeViolations } from '../../../test/axe';

describe('unserved feature registry helpers', () => {
  it('reports registered features as unserved and unknown keys as served', () => {
    expect(isFeatureServed('mirror-groups')).toBe(false);
    expect(findUnservedFeature('mirror-groups')?.label).toBe('Mirror groups');
    expect(isFeatureServed('no-such-feature-registered')).toBe(true);
    expect(findUnservedFeature('constructor')).toBeUndefined();
  });

  it('keeps every entry well-formed (label + METHOD /api/v1/... endpoints)', () => {
    for (const [key, feature] of Object.entries(UNSERVED_FEATURES)) {
      expect(feature.label.trim(), key).not.toBe('');
      for (const endpoint of feature.endpoints) {
        expect(endpoint, key).toMatch(/^(GET|POST|PUT|PATCH|DELETE) \/api\/v1\//);
      }
    }
  });
});

describe('NotAvailableYet', () => {
  it('says the feature is unavailable (not broken) and lists the missing endpoints', () => {
    render(<NotAvailableYet feature="custom-domains" />);
    const state = screen.getByTestId('not-available-yet');
    expect(state).toHaveAttribute('data-feature', 'custom-domains');
    expect(within(state).getByRole('heading', { name: "Custom domains isn't available yet" })).toBeInTheDocument();
    expect(within(state).getByText(/Nothing is wrong on your side/i)).toBeInTheDocument();
    expect(within(state).getByText('GET /api/v1/admin/:communityId/domains')).toBeInTheDocument();
  });

  it('accepts a custom description and tolerates an unregistered key', () => {
    render(<NotAvailableYet feature="shiny-new-thing" description="Ask an admin." className="py-4" />);
    expect(screen.getByRole('heading', { name: "shiny-new-thing isn't available yet" })).toBeInTheDocument();
    expect(screen.getByText('Ask an admin.')).toBeInTheDocument();
    expect(screen.queryByText(/Missing server endpoints/)).not.toBeInTheDocument();
  });

  it('has no axe violations', async () => {
    const { container } = render(<NotAvailableYet feature="bot-detection" />);
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe('ServedFeature', () => {
  it('renders the placeholder instead of children while the feature is unserved', () => {
    render(
      <ServedFeature feature="mirror-groups">
        <p>real page</p>
      </ServedFeature>,
    );
    expect(screen.queryByText('real page')).not.toBeInTheDocument();
    expect(screen.getByTestId('not-available-yet')).toBeInTheDocument();
  });

  it('applies fallbackClassName to the placeholder only', () => {
    render(
      <ServedFeature feature="mirror-groups" fallbackClassName="py-3">
        <p>real page</p>
      </ServedFeature>,
    );
    expect(screen.getByTestId('not-available-yet-body')).toHaveClass('py-3');
  });

  it('renders children once the feature is no longer registered as unserved', () => {
    render(
      <ServedFeature feature="feature-the-server-now-has">
        <p>real page</p>
      </ServedFeature>,
    );
    expect(screen.getByText('real page')).toBeInTheDocument();
    expect(screen.queryByTestId('not-available-yet')).not.toBeInTheDocument();
  });
});
