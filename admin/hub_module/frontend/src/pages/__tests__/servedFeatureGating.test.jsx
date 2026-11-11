/**
 * Pages that would otherwise show broken screens while hub-api lacks their routes render the
 * ServedFeature fallback ("not available yet") and never fire the dead calls.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { FRONTEND_ROOT } from '../../../scripts/lib/backend-routes.mjs';
import api from '../../services/api';
import { apiClient } from '../../lib/apiClient';
import { stubHttp } from '../../test/httpStub';
import * as AuthContext from '../../contexts/AuthContext';
import { ServedFeature } from '../../components/ui';
import { UNSERVED_FEATURES } from '../../lib/unservedFeatures';
import AdminHome from '../admin/AdminHome';
import AccountSettings from '../dashboard/AccountSettings';
import AdminMusicProviders from '../admin/AdminMusicProviders';
import AdminRadioStations from '../admin/AdminRadioStations';

let http;
afterEach(() => {
  http?.restore();
  vi.restoreAllMocks();
});

function at(ui, route, path) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path={path} element={ui} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('App.jsx route gates', () => {
  const app = readFileSync(join(FRONTEND_ROOT, 'src/App.jsx'), 'utf8');
  const gated = [...app.matchAll(/<ServedFeature feature="([^"]+)"><(\w+) \/><\/ServedFeature>/g)].map((m) => [m[2], m[1]]);

  it('wraps every contract-gap page in a registered ServedFeature', () => {
    const byPage = Object.fromEntries(gated.map(([page, feature]) => [page, feature]));
    for (const page of [
      'AdminConnectedPlatforms', 'AdminSecurity', 'AdminMusicDashboard', 'AdminAIKnowledge',
      'VendorGithubSync', 'VendorSubmissions',
    ]) {
      expect(byPage[page], `${page} must be gated`).toBeDefined();
      expect(UNSERVED_FEATURES[byPage[page]], `${page} -> ${byPage[page]} must be in unservedFeatures.json`).toBeDefined();
    }
  });

  it.each(gated)('%s renders the fallback, not the page, for %s', (_page, feature) => {
    const Boom = () => {
      throw new Error('page must not mount while its routes are unserved');
    };
    render(
      <ServedFeature feature={feature}>
        <Boom />
      </ServedFeature>,
    );
    expect(screen.getByTestId('not-available-yet')).toHaveAttribute('data-feature', feature);
  });
});

describe('AdminHome', () => {
  it('counts members and browser sources; domains show "not available yet" with no call', async () => {
    http = stubHttp([api, apiClient], {
      'GET /api/v1/admin/7/members': { pagination: { total: 12 } },
      'GET /api/v1/admin/7/browser-sources': { sources: [{}, {}] },
    });
    at(<AdminHome />, '/admin/7', '/admin/:communityId');
    expect(await screen.findByText('12')).toBeInTheDocument();
    expect(screen.getAllByTestId('not-available-yet').map((n) => n.getAttribute('data-feature'))).toEqual(['custom-domains']);
    expect(http.seen.map((r) => r.url).sort()).toEqual(['/api/v1/admin/7/browser-sources', '/api/v1/admin/7/members']);
  });
});

describe('AdminHome resilience', () => {
  it('tolerates empty payloads (counts fall back to 0)', async () => {
    http = stubHttp([api, apiClient], {
      'GET /api/v1/admin/7/members': {},
      'GET /api/v1/admin/7/browser-sources': {},
    });
    at(<AdminHome />, '/admin/7', '/admin/:communityId');
    expect((await screen.findAllByText('0')).length).toBeGreaterThanOrEqual(2);
  });

  it('logs and still renders when a served call fails', async () => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    http = stubHttp([api, apiClient], {});
    at(<AdminHome />, '/admin/7', '/admin/:communityId');
    await waitFor(() => expect(log).toHaveBeenCalledWith('Failed to fetch stats:', expect.any(Error)));
    expect(await screen.findByText('Community Admin')).toBeInTheDocument();
  });
});

describe('AccountSettings', () => {
  it('gates the personal OAuth credentials section and never calls /user/oauth/credentials', async () => {
    vi.spyOn(AuthContext, 'useAuth').mockReturnValue({ user: { id: 1, username: 'p', email: 'p@example.com' } });
    http = stubHttp([api, apiClient], {
      'GET /api/v1/auth/me': { success: true, user: { linkedPlatforms: [] } },
      'GET /api/v1/user/passkeys': { credentials: [] },
    });
    at(<AccountSettings />, '/dashboard/settings', '/dashboard/settings');
    await waitFor(() => expect(screen.getByTestId('not-available-yet')).toHaveAttribute('data-feature', 'user-oauth-credentials'));
    await waitFor(() => expect(http.seen.length).toBeGreaterThan(0));
    expect(http.seen.some((r) => r.url.includes('/user/oauth/credentials'))).toBe(false);
    expect(screen.queryByText('Add Credential')).not.toBeInTheDocument();
  });
});

describe('AdminMusicProviders', () => {
  it('hides the unserved enable toggle and explains why', async () => {
    http = stubHttp([api, apiClient], {
      'GET /api/v1/admin/7/music/providers': { providers: { spotify: { connected: true, enabled: true } } },
    });
    at(<AdminMusicProviders />, '/admin/7/music/providers', '/admin/:communityId/music/providers');
    expect(await screen.findByTestId('not-available-yet')).toHaveAttribute('data-feature', 'music-provider-config');
    await waitFor(() => expect(http.seen).toHaveLength(1));
    expect(screen.queryByText('Enable Provider')).not.toBeInTheDocument();
  });
});

describe('AdminRadioStations', () => {
  it('hides Test Stream / Set Default (unserved) but keeps the served list + delete', async () => {
    http = stubHttp([api, apiClient], {
      'GET /api/v1/admin/7/music/radio-stations': { stations: [{ id: 1, name: 'Chill', provider: 'custom', isDefault: false, streamUrl: 'https://x/y' }] },
    });
    at(<AdminRadioStations />, '/admin/7/music/radio', '/admin/:communityId/music/radio');
    expect(await screen.findByText('Chill')).toBeInTheDocument();
    expect(screen.getByTestId('not-available-yet')).toHaveAttribute('data-feature', 'radio-station-actions');
    expect(screen.queryByText('Test Stream')).not.toBeInTheDocument();
    expect(screen.queryByText('Set Default')).not.toBeInTheDocument();
    expect(screen.getByTitle('Delete station')).toBeInTheDocument();
    expect(within(document.body).getAllByTestId('not-available-yet')).toHaveLength(1);
  });
});
