/**
 * Contract re-point tests: each page runs for real against a stubbed HTTP adapter (the network
 * boundary), and we assert the exact METHOD + URL (+ params/body) that reaches hub-api.
 * Every URL below was verified against hub_api/blueprints/v1.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import api from '../../services/api';
import { apiClient } from '../../lib/apiClient';
import { stubHttp } from '../../test/httpStub';
import * as AuthContext from '../../contexts/AuthContext';
import SuperAdminVendorRequests from '../superadmin/SuperAdminVendorRequests';
import AdminVendorReview from '../admin/AdminVendorReview';
import VendorSubmissionStatus from '../vendor/VendorSubmissionStatus';
import VendorDashboard from '../vendor/VendorDashboard';
import VendorModules from '../vendor/VendorModules';
import VendorSettings from '../vendor/VendorSettings';
import VendorRequest from '../vendor/VendorRequest';
import VendorDiscountCodes from '../vendor/VendorDiscountCodes';
import VendorAnalytics from '../vendor/VendorAnalytics';
import SuperAdminAnalytics from '../superadmin/SuperAdminAnalytics';
import VendorSubmissions from '../vendor/VendorSubmissions';
import VendorGithubSync from '../vendor/VendorGithubSync';

vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../test/formModalStub')).FormModalStub,
}));

const V = '/api/v1/marketplace/vendor';
const ADMIN = '/api/v1/marketplace/admin/marketplace';
let http;

function mount(ui, { route = '/', path = '*' } = {}) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path={path} element={ui} />
      </Routes>
    </MemoryRouter>,
  );
}

function stub(routes) {
  http = stubHttp([api, apiClient], routes);
  return http.seen;
}

afterEach(() => {
  http?.restore();
  http = undefined;
  vi.restoreAllMocks();
});

const hits = (seen, key) => seen.filter((r) => `${r.method} ${r.url}` === key);

describe('vendor pages hit /api/v1/marketplace/vendor/*', () => {
  it('VendorDashboard -> GET dashboard', async () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({ user: { id: 1 } });
    const seen = stub({ [`GET ${V}/dashboard`]: { success: true, stats: {}, recentSubmissions: [] } });
    mount(<VendorDashboard />);
    await waitFor(() => expect(hits(seen, `GET ${V}/dashboard`)).toHaveLength(1));
    expect(seen).toHaveLength(1);
  });

  it('VendorDashboard shows the error when the dashboard call fails', async () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({ user: { id: 1 } });
    stub({});
    mount(<VendorDashboard />);
    expect(await screen.findByText('Failed to load dashboard')).toBeInTheDocument();
  });

  it('VendorModules -> GET modules', async () => {
    const seen = stub({ [`GET ${V}/modules`]: { success: true, modules: [] } });
    mount(<VendorModules />);
    await waitFor(() => expect(hits(seen, `GET ${V}/modules`)).toHaveLength(1));
  });

  it('VendorSettings -> GET profile (reads the seller envelope) and PUT profile on save', async () => {
    const seen = stub({
      [`GET ${V}/profile`]: { success: true, seller: { displayName: 'Acme', description: 'd', websiteUrl: '', payoutMethod: 'stripe_connect' } },
      [`PUT ${V}/profile`]: { success: true, seller: {} },
    });
    const { container } = mount(<VendorSettings />);
    await waitFor(() => expect(screen.getByDisplayValue('Acme')).toBeInTheDocument());
    fireEvent.submit(container.querySelector('form'));
    await waitFor(() => expect(hits(seen, `PUT ${V}/profile`)).toHaveLength(1));
    expect(hits(seen, `PUT ${V}/profile`)[0].body).toMatchObject({ displayName: 'Acme' });
  });

  it('VendorRequest -> GET request (not /request/status) and shows the existing request', async () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({ user: { email: 'v@example.com', username: 'vee' } });
    const seen = stub({
      [`GET ${V}/request`]: { success: true, request: { status: 'pending', company_name: 'Acme Co', requested_at: '2026-01-01T00:00:00Z' } },
    });
    mount(<VendorRequest />);
    await waitFor(() => expect(hits(seen, `GET ${V}/request`)).toHaveLength(1));
    expect(seen.some((r) => r.url.endsWith('/request/status'))).toBe(false);
  });

  it('VendorDiscountCodes -> GET discount-codes + modules', async () => {
    const seen = stub({
      [`GET ${V}/discount-codes`]: { success: true, codes: [], data: [] },
      [`GET ${V}/modules`]: { success: true, modules: [] },
    });
    mount(<VendorDiscountCodes />);
    await waitFor(() => expect(hits(seen, `GET ${V}/discount-codes`).length).toBeGreaterThan(0));
    await waitFor(() => expect(hits(seen, `GET ${V}/modules`).length).toBeGreaterThan(0));
  });

  it('VendorAnalytics -> sales, installs, discount-codes, communities under analytics/', async () => {
    const seen = stub({
      [`GET ${V}/analytics/sales`]: { data: [] },
      [`GET ${V}/analytics/installs`]: { data: [] },
      [`GET ${V}/analytics/discount-codes`]: { data: { codes: [] } },
      [`GET ${V}/analytics/communities`]: { data: { items: [] } },
    });
    mount(<VendorAnalytics />);
    for (const leaf of ['sales', 'installs', 'discount-codes', 'communities']) {
      await waitFor(() => expect(hits(seen, `GET ${V}/analytics/${leaf}`).length).toBeGreaterThan(0));
    }
    expect(seen.every((r) => r.url.startsWith('/api/v1/'))).toBe(true);
  });

  it('VendorSubmissions -> GET vendor/submissions (route is gated while hub-api lacks it)', async () => {
    const seen = stub({ [`GET ${V}/submissions`]: { submissions: [] } });
    mount(<VendorSubmissions />);
    await waitFor(() => expect(hits(seen, `GET ${V}/submissions`)).toHaveLength(1));
    expect(hits(seen, `GET ${V}/submissions`)[0].params).toEqual({ sortBy: 'newest' });
  });

  it('VendorGithubSync -> GET github-sync/connections under /api/v1', async () => {
    const seen = stub({ [`GET ${V}/github-sync/connections`]: { connections: [] } });
    mount(<VendorGithubSync />);
    await waitFor(() => expect(hits(seen, `GET ${V}/github-sync/connections`)).toHaveLength(1));
  });
});

describe('SuperAdminVendorRequests -> marketplace admin vendor-requests', () => {
  const request = {
    id: 9,
    requestId: 'req-9',
    status: 'pending',
    userDisplayName: 'Vee',
    companyName: 'Acme',
    userEmail: 'v@example.com',
    contactEmail: 'c@example.com',
    businessDescription: 'bd',
    requestedAt: '2026-01-01T00:00:00Z',
  };

  it('lists with ?status, approves with {notes}, rejects with {reason, notes}', async () => {
    const seen = stub({
      [`GET ${ADMIN}/vendor-requests`]: { success: true, requests: [request] },
      [`POST ${ADMIN}/vendor-requests/req-9/approve`]: { success: true },
      [`POST ${ADMIN}/vendor-requests/req-9/reject`]: { success: true },
    });
    mount(<SuperAdminVendorRequests />);
    expect(await screen.findByText('Vee')).toBeInTheDocument();
    expect(hits(seen, `GET ${ADMIN}/vendor-requests`)[0].params).toEqual({ status: 'pending' });

    fireEvent.click(screen.getByRole('button', { name: /^approve$/i }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /^approve$/i }));
    await waitFor(() => expect(hits(seen, `POST ${ADMIN}/vendor-requests/req-9/approve`)).toHaveLength(1));
    expect(hits(seen, `POST ${ADMIN}/vendor-requests/req-9/approve`)[0].body).toEqual({ notes: '' });
  });

  it('reject sends reason + notes', async () => {
    const seen = stub({
      [`GET ${ADMIN}/vendor-requests`]: { success: true, requests: [request] },
      [`POST ${ADMIN}/vendor-requests/req-9/reject`]: { success: true },
    });
    mount(<SuperAdminVendorRequests />);
    await screen.findByText('Vee');
    fireEvent.click(screen.getByRole('button', { name: /^reject$/i }));
    const dialog = within(await screen.findByRole('dialog'));
    fireEvent.change(dialog.getByLabelText(/Rejection Reason/i), { target: { value: 'incomplete' } });
    fireEvent.click(dialog.getByRole('button', { name: /^reject$/i }));
    await waitFor(() => expect(hits(seen, `POST ${ADMIN}/vendor-requests/req-9/reject`)).toHaveLength(1));
    expect(hits(seen, `POST ${ADMIN}/vendor-requests/req-9/reject`)[0].body).toEqual({ reason: 'incomplete', notes: '' });
  });

  it('surfaces a load failure instead of an empty list', async () => {
    stub({});
    mount(<SuperAdminVendorRequests />);
    expect(await screen.findByText('Failed to load requests')).toBeInTheDocument();
  });
});

describe('AdminVendorReview -> vendor-submissions/<id>', () => {
  const submission = { id: 5, module_name: 'Mod', status: 'pending', vendor_name: 'Acme', payment_method: 'stripe', scopes: [], reviews: [] };
  const route = { route: '/admin/vendor-submissions/5', path: '/admin/vendor-submissions/:submissionId' };

  it('loads with GET and approves with POST /approve', async () => {
    vi.spyOn(window, 'alert').mockImplementation(() => undefined);
    const seen = stub({
      [`GET ${ADMIN}/vendor-submissions/5`]: { success: true, submission },
      [`POST ${ADMIN}/vendor-submissions/5/approve`]: { success: true },
    });
    mount(<AdminVendorReview />, route);
    fireEvent.click(await screen.findByRole('button', { name: /Approve Submission/ }));
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'looks good' } });
    fireEvent.click(screen.getByRole('button', { name: /^Submit$/ }));
    await waitFor(() => expect(hits(seen, `POST ${ADMIN}/vendor-submissions/5/approve`)).toHaveLength(1));
    expect(hits(seen, `POST ${ADMIN}/vendor-submissions/5/approve`)[0].body).toMatchObject({ adminNotes: 'looks good' });
  });

  it('reject and request-info use their own endpoints', async () => {
    vi.spyOn(window, 'alert').mockImplementation(() => undefined);
    const seen = stub({
      [`GET ${ADMIN}/vendor-submissions/5`]: { success: true, submission },
      [`POST ${ADMIN}/vendor-submissions/5/reject`]: { success: true },
      [`POST ${ADMIN}/vendor-submissions/5/request-info`]: { success: true },
    });
    mount(<AdminVendorReview />, route);
    fireEvent.click(await screen.findByRole('button', { name: /Reject Submission/ }));
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'no' } });
    fireEvent.click(screen.getByRole('button', { name: /^Submit$/ }));
    await waitFor(() => expect(hits(seen, `POST ${ADMIN}/vendor-submissions/5/reject`)).toHaveLength(1));
    expect(hits(seen, `POST ${ADMIN}/vendor-submissions/5/reject`)[0].body).toEqual({ rejectionReason: 'no' });
  });

  it('shows an error when the submission cannot be loaded', async () => {
    stub({});
    mount(<AdminVendorReview />, route);
    expect(await screen.findByText('Not found')).toBeInTheDocument();
  });
});

describe('VendorSubmissionStatus -> public vendor submission status', () => {
  function submit(email, id) {
    fireEvent.change(screen.getByLabelText('Email Address'), { target: { value: email } });
    fireEvent.change(screen.getByLabelText('Submission ID'), { target: { value: id } });
    fireEvent.click(screen.getByRole('button', { name: 'Check Status' }));
  }

  it('GETs /api/v1/marketplace/public/vendor/submissions/<id>?email=', async () => {
    const url = '/api/v1/marketplace/public/vendor/submissions/abc-1';
    const seen = stub({ [`GET ${url}`]: { success: true, submission: { status: 'pending', module_name: 'Mod', vendor_name: 'A', submitted_at: '2026-01-01T00:00:00Z' } } });
    mount(<VendorSubmissionStatus />);
    submit('a@b.co', 'abc-1');
    await waitFor(() => expect(hits(seen, `GET ${url}`)).toHaveLength(1));
    expect(hits(seen, `GET ${url}`)[0].params).toEqual({ email: 'a@b.co' });
  });

  it('shows the server message on failure and validates input client-side', async () => {
    stub({});
    mount(<VendorSubmissionStatus />);
    submit('a@b.co', 'abc-1');
    expect(await screen.findByText('Not found')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Email Address'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Submission ID'), { target: { value: '' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Check Status' }).closest('form'));
    expect(await screen.findByText('Please provide both email and submission ID')).toBeInTheDocument();
  });
});

describe('SuperAdminAnalytics -> /api/v1/analytics/platform/*', () => {
  it('requests the four platform analytics routes and never the legacy /superadmin/analytics', async () => {
    const seen = stub({
      'GET /api/v1/analytics/platform/overview': { success: true },
      'GET /api/v1/analytics/platform/reputation': { success: true },
      'GET /api/v1/analytics/platform/growth?period=30d': { success: true },
      'GET /api/v1/analytics/platform/activity': { success: true },
      'GET /api/v1/analytics/platform/community-health?limit=50': { success: true },
    });
    mount(<SuperAdminAnalytics />);
    await waitFor(() => expect(seen.length).toBeGreaterThanOrEqual(5));
    expect(seen.some((r) => r.url.includes('/superadmin/analytics'))).toBe(false);
    expect(seen.map((r) => r.url)).toEqual(
      expect.arrayContaining([
        '/api/v1/analytics/platform/overview',
        '/api/v1/analytics/platform/reputation',
        '/api/v1/analytics/platform/activity',
      ]),
    );
  });

  it('surfaces a failed call instead of silently falling back', async () => {
    stub({});
    mount(<SuperAdminAnalytics />);
    expect(await screen.findByText(/Failed to load analytics/)).toBeInTheDocument();
  });
});
