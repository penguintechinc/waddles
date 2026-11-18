/** TenantScope: slug-keyed remount (no state bleed) and non-superadmin slug guard. */
import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const auth = { loading: false, scopes: [], tenantSlug: 'acme' };
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ ...auth, hasScope: (s) => auth.scopes.includes(s) }),
}));
import { TenantScope } from '../App';

function Stateful() {
  const [v, setV] = useState('');
  return <input aria-label="secret" value={v} onChange={(e) => setV(e.target.value)} />;
}
const api = vi.fn();
function Probe() {
  api();
  return <Stateful />;
}

function mount(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Link to="/tenant/other/x">go-other</Link>
      <Routes>
        <Route path="/tenant/:tenantSlug" element={<TenantScope />}>
          <Route path="x" element={<Probe />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  api.mockClear();
  Object.assign(auth, { loading: false, scopes: [], tenantSlug: 'acme' });
});

describe('TenantScope', () => {
  it('renders the page for the own tenant', () => {
    mount('/tenant/acme/x');
    expect(screen.getByLabelText('secret')).toBeTruthy();
  });

  it('non-superadmin on another slug: not-authorized, page never mounts', () => {
    mount('/tenant/other/x');
    expect(screen.getByTestId('tenant-not-authorized')).toBeTruthy();
    expect(api).not.toHaveBeenCalled();
  });

  it('a user holding only another scope is still fenced to their own tenant', () => {
    auth.scopes = ['tenant:admin'];
    mount('/tenant/other/x');
    expect(screen.getByTestId('tenant-not-authorized')).toBeTruthy();
  });

  it('a user with the tenants:admin scope and no role may view any slug', () => {
    auth.scopes = ['tenants:admin'];
    mount('/tenant/other/x');
    expect(screen.getByLabelText('secret')).toBeTruthy();
    expect(screen.queryByTestId('tenant-not-authorized')).toBeNull();
  });

  it('superadmin may view any slug and state resets when the slug changes', () => {
    auth.scopes = ['tenants:admin'];
    mount('/tenant/acme/x');
    fireEvent.change(screen.getByLabelText('secret'), { target: { value: 'cleartext' } });
    expect((screen.getByLabelText('secret')).value).toBe('cleartext');
    fireEvent.click(screen.getByText('go-other'));
    expect((screen.getByLabelText('secret')).value).toBe('');
  });
});
