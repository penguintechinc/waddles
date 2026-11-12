import { useCallback, useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { adminApi } from '../../services/api';
import { Alert, Button, Card, CardBody, LoadingState, PageHeader, SwitchField, TextField } from '../../components/ui';
import { validateCommunityPolicy, serverErrorMessage } from '../../lib/reputationPolicy';

/**
 * Community reputation settings: soft-ban and hard-ban floors (each with its own toggle,
 * both off by default) and the at-risk threshold. Thin view over the admin reputation config API.
 */
function CommunityReputationPolicy() {
  const { communityId } = useParams();
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
      const d = (await adminApi.getReputationConfig(communityId)).data;
      setForm({
        softEnabled: !!d.softBanEnabled,
        soft: String(d.softBanFloor ?? 550),
        hardEnabled: !!d.hardBanEnabled,
        hard: String(d.hardBanFloor ?? 400),
        atRisk: String(d.atRiskThreshold ?? 600),
      });
    } catch (err) {
      setLoadError({ status: err?.response?.status, message: serverErrorMessage(err, 'Failed to load reputation settings') });
    }
  }, [communityId]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSubmit(e) {
    e.preventDefault();
    setSaved(false);
    setSaveError(null);
    const found = validateCommunityPolicy(form);
    setErrors(found);
    if (Object.keys(found).length) return;
    setSaving(true);
    try {
      await adminApi.updateReputationConfig(communityId, {
        softBanEnabled: form.softEnabled,
        softBanFloor: Number(form.soft),
        hardBanEnabled: form.hardEnabled,
        hardBanFloor: Number(form.hard),
        atRiskThreshold: Number(form.atRisk),
      });
      setSaved(true);
    } catch (err) {
      setSaveError({ status: err?.response?.status, message: serverErrorMessage(err, 'Failed to save') });
    }
    setSaving(false);
  }

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const toggle = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.checked }));

  return (
    <div className="p-6 max-w-2xl">
      <PageHeader
        title="Community reputation settings"
        description="Score floors on the 300-850 scale. Hard-ban floor must be lower than the soft-ban floor, which must be lower than the at-risk threshold."
        actions={<Link to={`/admin/${communityId}/reputation/bans`} className="btn btn-secondary">Soft-banned and at-risk members</Link>}
      />
      {!form && !loadError && <LoadingState label="Loading reputation settings" />}
      {loadError && (
        <div data-testid="rep-load-error">
          <Alert tone="danger">
            {loadError.status === 403
              ? `You do not have permission to manage reputation settings for this community. ${loadError.message}`
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
                label="Soft ban"
                description="Members scoring below the soft-ban floor cannot win games, raffles or giveaways. They stay in the community. Off by default."
                checked={form.softEnabled}
                onChange={toggle('softEnabled')}
              />
              <TextField label="Soft-ban floor" type="number" min={300} max={850} value={form.soft} onChange={set('soft')} error={errors.soft} />
              <SwitchField
                label="Hard ban"
                description="Members scoring below the hard-ban floor are banned from this community. Off by default."
                checked={form.hardEnabled}
                onChange={toggle('hardEnabled')}
              />
              <TextField label="Hard-ban floor" type="number" min={300} max={850} value={form.hard} onChange={set('hard')} error={errors.hard} />
              <TextField
                label="At-risk threshold"
                type="number"
                min={300}
                max={850}
                value={form.atRisk}
                onChange={set('atRisk')}
                hint="Members below this score are listed as at risk."
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

export default CommunityReputationPolicy;
