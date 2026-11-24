import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { UpgradeNotice, isLicensedFeatureError } from '../UpgradeNotice';

describe('isLicensedFeatureError', () => {
  it('is true only for an HTTP 402 response', () => {
    expect(isLicensedFeatureError({ response: { status: 402 } })).toBe(true);
    expect(isLicensedFeatureError({ response: { status: 402, data: { error: { code: 'FEATURE_NOT_ENABLED' } } } })).toBe(true);
    expect(isLicensedFeatureError({ response: { status: 500 } })).toBe(false);
    expect(isLicensedFeatureError({ response: { status: 401 } })).toBe(false);
    expect(isLicensedFeatureError(new Error('402'))).toBe(false);
    expect(isLicensedFeatureError(null)).toBe(false);
    expect(isLicensedFeatureError(undefined)).toBe(false);
  });
});

describe('UpgradeNotice', () => {
  it('shows the friendly upgrade heading and a default description naming the feature', () => {
    render(<UpgradeNotice feature="Polls" />);
    expect(screen.getByRole('heading', { name: 'Upgrade to Premium for this feature' })).toBeInTheDocument();
    expect(screen.getByText("Polls is not included in this community's current plan.")).toBeInTheDocument();
    expect(screen.getByTestId('panel-upgrade')).toHaveAttribute('data-feature', 'Polls');
  });

  it('prefers the server message and renders an optional action', () => {
    render(<UpgradeNotice feature="Forms" serverMessage="Community forms are not enabled for this plan" action={<a href="/plans">See plans</a>} />);
    expect(screen.getByText('Community forms are not enabled for this plan')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'See plans' })).toBeInTheDocument();
  });
});
