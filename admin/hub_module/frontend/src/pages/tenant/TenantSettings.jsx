import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { tenantApi } from '../../services/api';
import { Alert, Button, Card, CardBody, LoadingState, PageHeader, TextField } from '../../components/ui';
import { serverErrorMessage } from '../../lib/reputationPolicy';

/**
 * Tenant settings: editable key/value pairs for one tenant.
 * Thin view over GET/PUT /api/v1/tenant/:slug/settings; the server is authoritative
 * (scope tenant:admin, URL slug must match the session tenant).
 */
function TenantSettings() {
  const { tenantSlug } = useParams();
  const [rows, setRows] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [saved, setSaved] = useState(false);
  const [keyError, setKeyError] = useState(null);

  const seq = useRef(0);
  const load = useCallback(async () => {
    const mine = ++seq.current;
    setLoadError(null);
    setRows(null);
    try {
      const res = await tenantApi.getSettings(tenantSlug);
      if (mine !== seq.current) return; // stale response from a previous tenant/slug
      setRows((res.data?.settings ?? []).map((s) => ({ key: s.key, value: s.value ?? '' })));
    } catch (err) {
      if (mine !== seq.current) return;
      setLoadError({ status: err?.response?.status, message: serverErrorMessage(err, 'Failed to load tenant settings') });
    }
  }, [tenantSlug]);

  useEffect(() => {
    load();
    return () => {
      seq.current += 1;
    };
  }, [load]);

  const setValue = (index, value) => {
    setSaved(false);
    setRows((r) => r.map((row, i) => (i === index ? { ...row, value } : row)));
  };

  const setKey = (index, key) => {
    setSaved(false);
    setRows((r) => r.map((row, i) => (i === index && row.isNew ? { ...row, key } : row)));
  };

  const addRow = () => {
    setSaved(false);
    setRows((r) => [...r, { key: '', value: '', isNew: true }]);
  };

  async function handleSubmit(e) {
    e.preventDefault();
    setSaved(false);
    setSaveError(null);
    const keys = rows.map((r) => r.key.trim());
    if (keys.some((k) => k === '')) {
      setKeyError('Every setting needs a key.');
      return;
    }
    if (new Set(keys).size !== keys.length) {
      setKeyError('Setting keys must be unique.');
      return;
    }
    setKeyError(null);
    setSaving(true);
    try {
      await tenantApi.updateSettings(
        tenantSlug,
        rows.map((r) => ({ key: r.key.trim(), value: r.value })),
      );
      setRows((r) => r.map((row) => ({ ...row, key: row.key.trim(), isNew: false })));
      setSaved(true);
    } catch (err) {
      setSaveError({ status: err?.response?.status, message: serverErrorMessage(err, 'Failed to save') });
    }
    setSaving(false);
  }

  return (
    <div className="p-6 max-w-2xl">
      <PageHeader title="Tenant settings" description="Key/value settings for this tenant." />
      {!rows && !loadError && <LoadingState label="Loading tenant settings" />}
      {loadError && (
        <div data-testid="tenant-settings-load-error">
          <Alert tone="danger">
            {loadError.status === 403
              ? `You do not have permission to manage tenant settings. ${loadError.message}`
              : loadError.message}
          </Alert>
          {loadError.status !== 403 && <Button className="mt-3" onClick={load}>Retry</Button>}
        </div>
      )}
      {rows && (
        <Card>
          <CardBody>
            <form onSubmit={handleSubmit} noValidate className="space-y-5">
              {rows.length === 0 && <p className="text-sm text-fg-muted">No settings yet.</p>}
              {rows.map((row, i) => (
                <div key={row.isNew ? `new-${i}` : row.key} className="grid grid-cols-2 gap-3">
                  <TextField
                    label={row.isNew ? 'New setting key' : `Key ${row.key}`}
                    value={row.key}
                    readOnly={!row.isNew}
                    onChange={(e) => setKey(i, e.target.value)}
                  />
                  <TextField
                    label={`Value ${row.key || i + 1}`}
                    value={row.value}
                    onChange={(e) => setValue(i, e.target.value)}
                  />
                </div>
              ))}
              {keyError && <Alert tone="danger">{keyError}</Alert>}
              {saveError && (
                <div data-testid="tenant-settings-save-error">
                  <Alert tone="danger">
                    {saveError.status === 403
                      ? `You do not have permission to change these settings. ${saveError.message}`
                      : saveError.message}
                  </Alert>
                </div>
              )}
              {saved && <Alert tone="success">Tenant settings saved.</Alert>}
              <div className="flex gap-3">
                <Button type="button" onClick={addRow}>Add setting</Button>
                <Button type="submit" variant="primary" loading={saving}>Save settings</Button>
              </div>
            </form>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

export default TenantSettings;
