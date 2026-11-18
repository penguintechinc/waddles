/**
 * Analytics pages: empty data -> "No data yet"/"No events yet"; an undeployed analytics backend
 * (503 ANALYTICS_UNAVAILABLE) -> neutral info note, never the raw `service_unavailable` token and
 * never a red alert; 402 -> per-widget plan notice; one failing widget never blanks the page; a
 * real 500 -> error with Retry.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

vi.mock('../../services/api', () => ({
  analyticsApi: {
    getPlatformOverview: vi.fn(),
    getPlatformReputation: vi.fn(),
    getPlatformGrowth: vi.fn(),
    getPlatformActivity: vi.fn(),
    getCommunityHealth: vi.fn(),
    getMyStats: vi.fn(),
    getMyReputation: vi.fn(),
    getMemberStats: vi.fn(),
    getMemberReputation: vi.fn(),
  },
}));

import { analyticsApi } from '../../services/api';
import { classifyAnalyticsError } from '../../components/analytics/classifyAnalyticsError';
import PlatformAnalytics from '../platform/PlatformAnalytics';
import SuperAdminAnalytics from '../superadmin/SuperAdminAnalytics';
import MyAnalytics from '../dashboard/MyAnalytics';
import AdminMemberAnalytics from '../admin/AdminMemberAnalytics';

const httpError = (status, code, message) => ({
  response: { status, data: { success: false, error: { code, message } } },
});
const unavailable = () =>
  httpError(503, 'ANALYTICS_UNAVAILABLE', 'Analytics are not available on this deployment yet');
const planGate = () =>
  httpError(402, 'FEATURE_NOT_ENABLED', 'Community health analytics requires a Professional plan or higher');
const serverError = () => httpError(500, 'INTERNAL_ERROR', 'Database exploded');

const OK = {
  overview: { total_users: 5, active_users_30d: 3, total_communities: 2, avg_reputation: 500 },
  reputation: { stats: { total: 4 }, histogram: [{ range: '500-549', count: 4 }] },
  growth: { buckets: [{ label: 'd1', new_users: 2, new_communities: 1 }] },
  activity: { active_24h: 1, active_7d: 2, active_30d: 3, active_90d: 4, inactive: 1 },
  health: { communities: [{ id: 1, name: 'Alpha', member_count: 3, health_score: 70, bot_score_grade: 'A' }] },
};

function mockPlatform(overrides = {}) {
  const resolve = (v) => (v instanceof Error || v?.response ? Promise.reject(v) : Promise.resolve({ data: v }));
  const m = { ...OK, ...overrides };
  analyticsApi.getPlatformOverview.mockImplementation(() => resolve(m.overview));
  analyticsApi.getPlatformReputation.mockImplementation(() => resolve(m.reputation));
  analyticsApi.getPlatformGrowth.mockImplementation(() => resolve(m.growth));
  analyticsApi.getPlatformActivity.mockImplementation(() => resolve(m.activity));
  analyticsApi.getCommunityHealth.mockImplementation(() => resolve(m.health));
}

beforeEach(() => vi.resetAllMocks());

describe('classifyAnalyticsError', () => {
  it.each([
    [unavailable(), 'unavailable'],
    [httpError(503, undefined, 'x'), 'unavailable'],
    [httpError(502, undefined, 'x'), 'unavailable'],
    [planGate(), 'feature-disabled'],
    [httpError(502, 'ANALYTICS_UPSTREAM_ERROR', 'Upstream returned HTTP 500'), 'error'],
    [serverError(), 'error'],
    [httpError(404, 'ERROR_404', 'Member not found'), 'error'],
    [new Error('Network Error'), 'error'],
  ])('%j -> %s', (err, kind) => {
    expect(classifyAnalyticsError(err).kind).toBe(kind);
  });

  it('404 is not-found only when the caller opts in', () => {
    expect(classifyAnalyticsError(httpError(404, 'ERROR_404', 'x'), { notFound: true }).kind).toBe('not-found');
  });

  it('never leaks the raw service_unavailable token', () => {
    const f = classifyAnalyticsError(
      { response: { status: 503, data: { error: 'service_unavailable', service: 'analytics-core' } } },
    );
    expect(f.kind).toBe('unavailable');
    expect(f.message).not.toMatch(/service_unavailable/);
  });
});

describe.each([
  ['PlatformAnalytics', PlatformAnalytics],
  ['SuperAdminAnalytics', SuperAdminAnalytics],
])('%s', (_name, Page) => {
  it('renders data when everything succeeds', async () => {
    mockPlatform();
    render(<Page />);
    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('empty data -> "No data yet" / "No events yet", no error', async () => {
    mockPlatform({
      overview: { total_users: 0, active_users_30d: 0, total_communities: 0, avg_reputation: 0 },
      reputation: { stats: { total: 0 }, histogram: [] },
      growth: { buckets: [] },
      activity: { active_24h: 0, active_7d: 0, active_30d: 0, active_90d: 0, inactive: 0 },
      health: { communities: [] },
    });
    render(<Page />);
    await waitFor(() => expect(screen.getAllByText('No data yet').length).toBeGreaterThan(0));
    expect(screen.getByText('No events yet')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('503 ANALYTICS_UNAVAILABLE everywhere -> neutral info, no raw string, no alert', async () => {
    for (const fn of Object.values(analyticsApi)) fn.mockRejectedValue(unavailable());
    render(<Page />);
    await waitFor(() => expect(screen.getAllByTestId('analytics-unavailable').length).toBe(5));
    expect(screen.queryByText(/service_unavailable/)).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getAllByText("Analytics aren't available on this deployment yet.").length).toBe(5);
  });

  it('402 on community health -> per-widget notice, other widgets still render', async () => {
    mockPlatform({ health: planGate() });
    render(<Page />);
    expect(await screen.findByTestId('analytics-feature-notice')).toHaveTextContent('Professional plan');
    expect(screen.getByText('Reputation Distribution')).toBeInTheDocument();
    expect(screen.getByText('500-549')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('one rejected call does not blank the page', async () => {
    mockPlatform({ overview: unavailable() });
    render(<Page />);
    expect(await screen.findByTestId('analytics-unavailable')).toBeInTheDocument();
    expect(screen.getByText('Alpha')).toBeInTheDocument();
    expect(screen.getByText('Platform Growth')).toBeInTheDocument();
    expect(screen.getByText('Activity Breakdown')).toBeInTheDocument();
  });

  it('real 500 -> error with the message and a working Retry', async () => {
    mockPlatform({ health: serverError() });
    render(<Page />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Database exploded');
    mockPlatform();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('MyAnalytics', () => {
  it('all-zero stats -> "No activity yet", not zero cards', async () => {
    analyticsApi.getMyStats.mockResolvedValue({ data: { total_messages: 0, total_watch_minutes: 0, communities: [] } });
    analyticsApi.getMyReputation.mockResolvedValue({ data: { communities: [] } });
    render(<MyAnalytics />);
    expect(await screen.findByText('No activity yet')).toBeInTheDocument();
    expect(screen.queryByText('Total Messages')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('both calls 503 ANALYTICS_UNAVAILABLE -> one neutral note', async () => {
    analyticsApi.getMyStats.mockRejectedValue(unavailable());
    analyticsApi.getMyReputation.mockRejectedValue(unavailable());
    render(<MyAnalytics />);
    expect(await screen.findAllByTestId('analytics-unavailable')).toHaveLength(1);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText(/service_unavailable/)).toBeNull();
  });

  it('402 on one call -> notice, the other still renders', async () => {
    analyticsApi.getMyStats.mockResolvedValue({ data: { total_messages: 12, communities: [] } });
    analyticsApi.getMyReputation.mockRejectedValue(planGate());
    render(<MyAnalytics />);
    expect(await screen.findByTestId('analytics-feature-notice')).toBeInTheDocument();
    expect(screen.getByText('Total Messages')).toBeInTheDocument();
  });

  it('one failing call keeps the other visible', async () => {
    analyticsApi.getMyStats.mockRejectedValue(unavailable());
    analyticsApi.getMyReputation.mockResolvedValue({ data: { global_reputation: 640, communities: [] } });
    render(<MyAnalytics />);
    expect(await screen.findByTestId('analytics-unavailable')).toBeInTheDocument();
    expect(screen.getByText('640')).toBeInTheDocument();
  });

  it('real 500 on both -> error with Retry', async () => {
    analyticsApi.getMyStats.mockRejectedValue(serverError());
    analyticsApi.getMyReputation.mockRejectedValue(serverError());
    render(<MyAnalytics />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Database exploded');
    analyticsApi.getMyStats.mockResolvedValue({ data: { total_messages: 3, communities: [] } });
    analyticsApi.getMyReputation.mockResolvedValue({ data: { global_reputation: 600, communities: [] } });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Total Messages')).toBeInTheDocument();
  });
});

describe('AdminMemberAnalytics', () => {
  const renderPage = () =>
    render(
      <MemoryRouter initialEntries={['/admin/1/members/7/analytics']}>
        <Routes>
          <Route path="/admin/:communityId/members/:userId/analytics" element={<AdminMemberAnalytics />} />
        </Routes>
      </MemoryRouter>,
    );

  it('member with no data -> "No analytics for this member yet"', async () => {
    analyticsApi.getMemberStats.mockResolvedValue({ data: { total_messages: 0, total_watch_minutes: 0 } });
    analyticsApi.getMemberReputation.mockResolvedValue({ data: {} });
    renderPage();
    expect(await screen.findByText('No analytics for this member yet')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('upstream 404 (no rows) -> same empty state', async () => {
    analyticsApi.getMemberStats.mockRejectedValue(httpError(404, 'NOT_FOUND', 'No stats for user'));
    analyticsApi.getMemberReputation.mockRejectedValue(httpError(404, 'NOT_FOUND', 'No stats for user'));
    renderPage();
    expect(await screen.findByText('No analytics for this member yet')).toBeInTheDocument();
  });

  it('unknown member (blueprint 404) -> real not-found message, not an alert', async () => {
    analyticsApi.getMemberStats.mockRejectedValue(httpError(404, 'ERROR_404', 'Member not found'));
    analyticsApi.getMemberReputation.mockRejectedValue(httpError(404, 'ERROR_404', 'Member not found'));
    renderPage();
    expect(await screen.findByTestId('analytics-not-found')).toHaveTextContent('Member not found');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('503 ANALYTICS_UNAVAILABLE -> neutral info, back link kept', async () => {
    analyticsApi.getMemberStats.mockRejectedValue(unavailable());
    analyticsApi.getMemberReputation.mockRejectedValue(unavailable());
    renderPage();
    expect(await screen.findByTestId('analytics-unavailable')).toBeInTheDocument();
    expect(screen.getByText('Back to Members')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('real 500 -> error with Retry', async () => {
    analyticsApi.getMemberStats.mockRejectedValue(serverError());
    analyticsApi.getMemberReputation.mockRejectedValue(serverError());
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Database exploded');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('data present -> renders stats', async () => {
    analyticsApi.getMemberStats.mockResolvedValue({ data: { username: 'zed', total_messages: 9 } });
    analyticsApi.getMemberReputation.mockResolvedValue({ data: { reputation: 700 } });
    renderPage();
    expect(await screen.findByText('zed')).toBeInTheDocument();
  });
});
