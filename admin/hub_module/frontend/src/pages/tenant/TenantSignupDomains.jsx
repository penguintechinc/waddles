import { useCallback, useEffect, useState } from 'react';
import { tenantApi } from '../../services/api';
import { Alert, Button, Card, CardBody, TextField } from '../../components/ui';
import { serverErrorMessage } from '../../lib/reputationPolicy';

/** "a.com, b.com" -> ['a.com', 'b.com'] (server re-validates and normalises). */
const parseDomains = (text) => text.split(/[\s,]+/).map((d) => d.trim()).filter(Boolean);

/**
 * Tenant signup email-domain restriction. When non-empty, EVERY signup into the tenant (email,
 * OAuth or SSO) must verify an address in one of these domains before it gets access. Server:
 * GET/PUT /api/v1/tenant/:slug/signup-domains (PUT needs tenant.signup_domains:write).
 */
function TenantSignupDomains({ tenantSlug }) {
  const [data, setData] = useState(null);
  const [text, setText] = useState('');
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await tenantApi.getSignupDomains(tenantSlug);
      setData(res.data);
      setText((res.data?.domains ?? []).join(', '));
    } catch (err) {
      setError(serverErrorMessage(err, 'Failed to load signup domain settings'));
    }
  }, [tenantSlug]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSave(e) {
    e.preventDefault();
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const res = await tenantApi.updateSignupDomains(tenantSlug, parseDomains(text));
      setData(res.data);
      setText((res.data?.domains ?? []).join(', '));
      setSaved(true);
    } catch (err) {
      setError(
        err?.response?.status === 403
          ? 'You do not have permission to change the signup domain restriction.'
          : serverErrorMessage(err, 'Failed to save signup domains'),
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="mb-6" data-testid="tenant-signup-domains">
      <CardBody>
        <h2 className="text-lg font-semibold text-gold-400">Signup email domains</h2>
        <p className="mt-1 text-sm text-fg-muted">
          Restrict who can join this tenant. When set, every new account (email, OAuth or SSO)
          must verify an email address in one of these domains before it can sign in. Leave
          empty for no restriction.
        </p>
        {!data && !error && <p className="mt-3 text-sm text-fg-muted">Loading…</p>}
        <form onSubmit={handleSave} noValidate className="mt-4 space-y-3">
          <TextField
            label="Allowed domains (comma-separated)"
            value={text}
            placeholder="example.com, example.org"
            onChange={(e) => {
              setSaved(false);
              setText(e.target.value);
            }}
          />
          {data?.platformDomains?.length > 0 && (
            <p className="text-xs text-fg-muted" data-testid="signup-domains-effective">
              The platform also limits signups to {data.platformDomains.join(', ')}. Effective:{' '}
              {data.effectiveDomains.length ? data.effectiveDomains.join(', ') : 'no domain can sign up'}.
            </p>
          )}
          {error && <Alert tone="danger">{error}</Alert>}
          {saved && <Alert tone="success">Signup domains saved.</Alert>}
          <Button type="submit" variant="primary" loading={saving} disabled={!data && !error}>
            Save signup domains
          </Button>
        </form>
      </CardBody>
    </Card>
  );
}

export default TenantSignupDomains;
