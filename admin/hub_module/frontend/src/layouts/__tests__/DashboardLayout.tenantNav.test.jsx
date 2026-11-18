/** Nav gating: super admin sees Tenants; tenant:admin scope sees the Tenant Admin section; plain user sees neither. */
import { render } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let capturedProps = {};
const navigateMock = vi.hoisted(() => vi.fn());
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig()),
  useNavigate: () => navigateMock,
}));
vi.mock('@penguintechinc/react-libs', () => ({
  SidebarMenu: (props) => {
    capturedProps = props;
    return <div data-testid="sidebar-menu" />;
  },
  SidebarMenuTrigger: () => <button />,
}));
vi.mock('../../components/GlobalBanner', () => ({ default: () => null }));
vi.mock('../../components/VendorRequestFooter', () => ({ default: () => null }));
vi.mock('../../lib/useFeatureFlag', () => ({ useFeatureFlag: () => false }));

const mockAuth = {};
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => mockAuth }));

import DashboardLayout from '../DashboardLayout';

function setAuth({ isSuperAdmin = false, scopes = [], tenantSlug = null } = {}) {
  Object.assign(mockAuth, {
    user: { username: 'u' },
    logout: vi.fn(),
    isSuperAdmin,
    isVendor: false,
    isAnalyticsConsumer: false,
    isCommunityAdmin: () => false,
    hasScope: (s) => scopes.includes(s),
    tenantSlug,
  });
}

function renderNav(path = '/dashboard') {
  capturedProps = {};
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<DashboardLayout />} />
      </Routes>
    </MemoryRouter>,
  );
  return capturedProps.categories;
}

const find = (cats, key) => cats.find((c) => c.key === key);

/** Resolves a sidebar token href back to its real route by capturing what onNavigate forwards. */
function realHref(token) {
  navigateMock.mockClear();
  capturedProps.onNavigate(token);
  return navigateMock.mock.calls[0][0];
}

describe('DashboardLayout tenant navigation', () => {
  beforeEach(() => setAuth());

  it('super admin sees a Tenants item linking /superadmin/tenants', () => {
    setAuth({ isSuperAdmin: true });
    const item = find(renderNav(), 'super-admin').items.find((i) => i.name === 'Tenants');
    expect(realHref(item.href)).toBe('/superadmin/tenants');
  });

  it('tenant admin sees the Tenant Admin section built from the auth-context slug', () => {
    setAuth({ scopes: ['tenant:admin'], tenantSlug: 'acme' });
    const cats = renderNav();
    const section = find(cats, 'tenant-admin');
    expect(section.items.map((i) => realHref(i.href))).toEqual(
      expect.arrayContaining(['/tenant/acme', '/tenant/acme/settings', '/tenant/acme/admins']),
    );
    expect(find(cats, 'super-admin')).toBeUndefined();
  });

  it('tenant admin sees Community tiers linking /tenant/<slug>/community-tiers; plain user does not', () => {
    setAuth({ scopes: ['tenant:admin'], tenantSlug: 'acme' });
    const item = find(renderNav(), 'tenant-admin').items.find((i) => i.name === 'Community tiers');
    expect(realHref(item.href)).toBe('/tenant/acme/community-tiers');
    setAuth();
    expect(find(renderNav(), 'tenant-admin')).toBeUndefined();
  });

  it('tenant admin sees License linking /tenant/<slug>/license; without the scope it is hidden', () => {
    setAuth({ scopes: ['tenant:admin'], tenantSlug: 'acme' });
    const item = find(renderNav(), 'tenant-admin').items.find((i) => i.name === 'License');
    expect(realHref(item.href)).toBe('/tenant/acme/license');
    setAuth({ scopes: ['compliance.kms:admin'], tenantSlug: 'acme' });
    expect(find(renderNav(), 'tenant-admin').items.map((i) => i.name)).not.toContain('License');
  });

  it('opens the tenant section on /tenant/ paths', () => {
    setAuth({ scopes: ['tenant:admin'], tenantSlug: 'acme' });
    renderNav('/tenant/acme/settings');
    expect(capturedProps.activeGroupKey).toBe('tenant-admin');
  });

  it('plain user sees neither', () => {
    const cats = renderNav();
    expect(find(cats, 'tenant-admin')).toBeUndefined();
    expect(find(cats, 'super-admin')).toBeUndefined();
  });

  it('tenant:admin scope without a tenant slug shows no tenant section', () => {
    setAuth({ scopes: ['tenant:admin'], tenantSlug: null });
    expect(find(renderNav(), 'tenant-admin')).toBeUndefined();
  });

  it('a slug without the scope shows no tenant section', () => {
    setAuth({ scopes: [], tenantSlug: 'acme' });
    expect(find(renderNav(), 'tenant-admin')).toBeUndefined();
  });

  describe('privacy / kms / ingest-sources items', () => {
    const names = (cats) => (find(cats, 'tenant-admin')?.items ?? []).map((i) => i.name);

    it('tenant:admin sees Data Privacy and Ingest sources but not KMS', () => {
      setAuth({ scopes: ['tenant:admin'], tenantSlug: 'acme' });
      const cats = renderNav();
      const section = find(cats, 'tenant-admin');
      const byName = Object.fromEntries(section.items.map((i) => [i.name, i]));
      expect(realHref(byName['Data Privacy'].href)).toBe('/tenant/acme/privacy');
      expect(realHref(byName['Ingest sources'].href)).toBe('/tenant/acme/ingest-sources');
      expect(names(cats)).not.toContain('Encryption keys (BYOK)');
    });

    it('tenant:admin plus compliance.kms:admin sees KMS', () => {
      setAuth({ scopes: ['tenant:admin', 'compliance.kms:admin'], tenantSlug: 'acme' });
      const cats = renderNav();
      const item = find(cats, 'tenant-admin').items.find((i) => i.name === 'Encryption keys (BYOK)');
      expect(realHref(item.href)).toBe('/tenant/acme/kms');
    });

    it('compliance.kms:admin alone sees the group with only the KMS item', () => {
      setAuth({ scopes: ['compliance.kms:admin'], tenantSlug: 'acme' });
      expect(names(renderNav())).toEqual(['Encryption keys (BYOK)']);
    });

    it('compliance.kms:admin without a slug shows nothing', () => {
      setAuth({ scopes: ['compliance.kms:admin'], tenantSlug: null });
      expect(find(renderNav(), 'tenant-admin')).toBeUndefined();
    });

    it('plain user sees none of the new items', () => {
      expect(names(renderNav())).toEqual([]);
    });

    it('super admin without tenant scopes gets no tenant section', () => {
      setAuth({ isSuperAdmin: true, tenantSlug: 'acme' });
      expect(find(renderNav(), 'tenant-admin')).toBeUndefined();
    });

    it('opens the group on /tenant/<slug>/kms', () => {
      setAuth({ scopes: ['compliance.kms:admin'], tenantSlug: 'acme' });
      renderNav('/tenant/acme/kms');
      expect(capturedProps.activeGroupKey).toBe('tenant-admin');
    });
  });
});
