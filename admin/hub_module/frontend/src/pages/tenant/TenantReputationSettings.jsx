import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { tenantApi } from '../../services/api';
import { Alert, Button, Card, CardBody, LoadingState, PageHeader, SwitchField, TextField } from '../../components/ui';
import { validateTenantPolicy, serverErrorMessage } from '../../lib/reputationPolicy';

/**
 * Tenant reputation settings: tenant-wide auto-ban toggle, floor and at-risk threshold.
 * Thin view over GET/PUT /api/v1/tenant/:slug/reputation/config; the server is authoritative.
 */
function TenantReputationSettings() {
  const { tenantSlug } = useParams();
  const [form, setForm] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [saved, setSaved] = useState(false);
  const [errors, setErrors] = useState({});

  const load = useCallback(async () => {
    setLoadError(null);
    setForm(null);
    try {
      const res = await tenantApi.getReputationConfig(tenantSlug);
      const d = res.data;
      setForm({
        enabled: !!d.tenantAutoBanEnabled,
        floor: String(d.tenantAutoBanFloor ?? 350),
        atRisk: String(d.tenantAtRiskThreshold ?? 450),
      });
    } catch (err) {
      setLoadError({ status: err?.response?.status, message: serverErrorMessage(err, 'Failed to load reputation settings') });
    }
  }, [tenantSlug]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSubmit(e) {
    e.preventDefault();
    setSaved(false);
    setSaveError(null);
    const found = validateTenantPolicy(form);
    setErrors(found);
    if (Object.keys(found).length) return;
    setSaving(true);
    try {
      await tenantApi.updateReputationConfig(tenantSlug, {
        tenantAutoBanEnabled: form.enabled,
        tenantAutoBanFloor: Number(form.floor),
        tenantAtRiskThreshold: Number(form.atRisk),
      });
      setSaved(true);
    } catch (err) {
      setSaveError({ status: err?.response?.status, message: serverErrorMessage(err, 'Failed to save') });
    }
    setSaving(false);
  }

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  return (
    <div className="p-6 max-w-2xl">
      <PageHeader
        title="Tenant reputation settings"
        description="Tenant-wide reputation policy on the 300-850 scale. Communities set their own ban floors separately."
      />
      {!form && !loadError && <LoadingState label="Loading reputation settings" />}
      {loadError && (
        <div data-testid="rep-load-error">
          <Alert tone="danger">
            {loadError.status === 403
              ? `You do not have permission to manage tenant reputation settings. ${loadError.message}`
              : loadError.message}
          </Alert>
          {loadError.status !== 403 && <Button className="mt-3" onClick={load}>Retry</Button>}
        </div>
      )}
      {form && (
        <Card>
          <CardBody>
            <form onSubmit={handleSubmit} noValidate className="space-y-5">
              <SwitchField
                label="Tenant auto-ban"
                description="When on, members whose score falls below the floor are banned across the tenant. Off by default."
                checked={form.enabled}
                onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
              />
              <TextField
                label="Auto-ban floor"
                type="number"
                min={300}
                max={850}
                value={form.floor}
                onChange={set('floor')}
                hint="Must be 300-850 and lower than the at-risk threshold."
                error={errors.floor}
              />
              <TextField
                label="At-risk threshold"
                type="number"
                min={300}
                max={850}
                value={form.atRisk}
                onChange={set('atRisk')}
                hint="Members below this score are flagged as at risk."
                error={errors.atRisk}
              />
              {saveError && (
                <div data-testid="rep-save-error"><Alert tone="danger">
                  {saveError.status === 403
                    ? `You do not have permission to change these settings. ${saveError.message}`
                    : saveError.message}
                </Alert></div>
              )}
              {saved && <Alert tone="success">Reputation settings saved.</Alert>}
              <Button type="submit" variant="primary" loading={saving}>Save settings</Button>
            </form>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

export default TenantReputationSettings;
