import { useState } from 'react';
import type { FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  Alert, Badge, Button, Card, CardBody, ErrorState, LoadingState, PageHeader, SelectField, TextField,
} from '../../components/ui';
import { getErrorMessage } from '../../components/ui';
import { isNotEntitled, maskKeyRef, tenantKmsApi } from '../../services/tenantKmsApi';
import type { KmsRewrap } from '../../services/tenantKmsApi';

type Pending = 'rotate' | 'remove' | null;

/** True only when the rewrap fully succeeded: ok flag set and no failed versions. */
function rewrapClean(rw: KmsRewrap | null): boolean {
  return rw === null || (rw.ok === true && rw.failedVersions.length === 0);
}

/** Failed-version list carried by a 409 REWRAP_INCOMPLETE error body, if any. */
function incompleteFailures(err: unknown): number[] | null {
  const r = (err as { response?: { status?: number; data?: { error?: { code?: unknown; details?: { failedVersions?: unknown } } } } })?.response;
  if (r?.status !== 409 || r.data?.error?.code !== 'REWRAP_INCOMPLETE') return null;
  const fv = r.data.error.details?.failedVersions;
  return Array.isArray(fv) ? (fv as number[]) : [];
}

const CONFIRM_WORD: Record<Exclude<Pending, null>, string> = { rotate: 'ROTATE', remove: 'REMOVE' };

/** Rewrap outcome summary: counts only. */
function RewrapSummary({ rewrap }: { rewrap: KmsRewrap }) {
  return (
    <p data-testid="kms-rewrap">
      Re-wrapped {rewrap.rewrapped} of {rewrap.total} data keys ({rewrap.alreadyCurrent} already current
      {rewrap.failedVersions.length > 0 ? `, ${rewrap.failedVersions.length} failed` : ''}).
    </p>
  );
}

/**
 * Tenant settings -> Encryption keys (BYOK). Enterprise-only external KMS:
 * record a customer key (pending), activate it (re-wraps data keys), or
 * remove it to return to the platform key. Only masked identifiers are
 * rendered; inputs are cleared after submit and no key material is handled.
 */
export default function TenantKms() {
  const { tenantSlug = '' } = useParams<{ tenantSlug: string }>();
  const qc = useQueryClient();
  const [provider, setProvider] = useState('');
  const [keyRef, setKeyRef] = useState('');
  const [region, setRegion] = useState('');
  const [principal, setPrincipal] = useState('');
  const [pending, setPending] = useState<Pending>(null);
  const [typed, setTyped] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [rewrap, setRewrap] = useState<KmsRewrap | null>(null);

  const statusQuery = useQuery({
    queryKey: ['tenantKms', tenantSlug],
    queryFn: () => tenantKmsApi.getStatus(tenantSlug),
    retry: false,
  });

  const done = (msg: string, rw: KmsRewrap | null = null) => {
    if (rewrapClean(rw)) {
      setNotice(msg);
      setWarning(null);
    } else {
      setNotice(null);
      setWarning(
        'Some data keys could not be re-wrapped and are still protected by the previous key. '
        + 'Do NOT revoke or delete the old/external key yet; retry, then confirm zero failures.',
      );
    }
    setRewrap(rw);
    setPending(null);
    setTyped('');
    void qc.invalidateQueries({ queryKey: ['tenantKms', tenantSlug] });
  };

  const configure = useMutation({
    mutationFn: () =>
      tenantKmsApi.configure(tenantSlug, {
        provider: provider || (statusQuery.data?.supportedProviders[0] ?? ''),
        keyRef: keyRef.trim(),
        region: region.trim() || null,
        principal: principal.trim() || null,
      }),
    onMutate: () => { setNotice(null); setWarning(null); setRewrap(null); },
    onSuccess: () => done('Key recorded as pending. Grant access on your side, then activate it.'),
    onSettled: () => {
      setKeyRef('');
      setRegion('');
      setPrincipal('');
    },
  });
  const activate = useMutation({
    mutationFn: () => tenantKmsApi.activate(tenantSlug),
    onMutate: () => { setNotice(null); setWarning(null); setRewrap(null); },
    onSuccess: (r) => done('External key activated.', r.rewrap),
  });
  const remove = useMutation({
    mutationFn: () => tenantKmsApi.remove(tenantSlug),
    onMutate: () => { setNotice(null); setWarning(null); setRewrap(null); },
    onError: (err) => {
      const failed = incompleteFailures(err);
      if (failed !== null) {
        setWarning(
          `Removal incomplete: ${failed.length} data key(s) could not be re-wrapped and are still protected by your `
          + 'external key. Do NOT revoke or delete the external key yet; the configuration was kept so they stay readable.',
        );
      }
    },
    onSuccess: (r) => done('External key removed; data keys are back on the platform key.', r.rewrap),
  });

  const actionError = [configure, activate, remove].find((m) => m.isError)?.error;
  const incomplete = actionError !== undefined && incompleteFailures(actionError) !== null;
  const notEntitled = actionError !== undefined && isNotEntitled(actionError);

  if (statusQuery.isLoading) return <div className="p-6"><LoadingState label="Loading encryption settings" /></div>;
  if (statusQuery.isError) {
    const status = (statusQuery.error as { response?: { status?: number } })?.response?.status;
    return (
      <div className="p-6 max-w-3xl">
        <PageHeader title="Encryption keys (BYOK)" />
        {status === 403 ? (
          <Alert tone="warning">You do not have permission to manage encryption keys for this tenant.</Alert>
        ) : (
          <ErrorState error={statusQuery.error} title="Failed to load encryption settings" />
        )}
      </div>
    );
  }

  const status = statusQuery.data;
  if (!status) return null;
  const config = status.config;
  const providers = status.supportedProviders;
  const selected = provider || providers[0] || '';
  const principalHint = status.platformPrincipals[selected];
  const busy = configure.isPending || activate.isPending || remove.isPending;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!selected || keyRef.trim() === '' || busy) return;
    if (config && config.status === 'active') {
      setPending('rotate');
      return;
    }
    configure.mutate();
  };

  const confirmOk = pending !== null && typed === CONFIRM_WORD[pending];
  const runConfirmed = () => {
    if (!confirmOk) return;
    if (pending === 'rotate') configure.mutate();
    else remove.mutate();
    setPending(null);
    setTyped('');
  };

  return (
    <div className="p-6 max-w-3xl space-y-6">
      <PageHeader
        title="Encryption keys (BYOK)"
        description="By default your data is protected by the platform-managed key. Enterprise tenants can bring their own key from an external KMS."
      />

      {notEntitled && (
        <div data-testid="kms-upgrade">
          <Alert tone="info" title="Enterprise feature">
            Bringing your own key requires the Enterprise plan. Upgrade to enable external KMS; you can still view
            your status and return to the platform key at any time.
          </Alert>
        </div>
      )}
      {actionError !== undefined && !notEntitled && !incomplete && (
        <div data-testid="kms-action-error">
          <Alert tone="danger">
            {(actionError as { response?: { status?: number } })?.response?.status === 403
              ? 'You do not have permission to do that.'
              : getErrorMessage(actionError, 'The key operation failed')}
          </Alert>
        </div>
      )}
      {warning && <div data-testid="kms-warning"><Alert tone="danger" title="Do not revoke the old key yet">{warning}</Alert></div>}
      {notice && <div data-testid="kms-notice"><Alert tone="success">{notice}</Alert></div>}
      {rewrap && <RewrapSummary rewrap={rewrap} />}

      <Card>
        <CardBody>
          <h2 className="text-sm font-semibold mb-2">Current configuration</h2>
          {config ? (
            <div data-testid="kms-config" className="space-y-1 text-sm">
              <Badge tone={config.status === 'active' ? 'green' : config.status === 'pending' ? 'gold' : 'red'}>
                {config.status}
              </Badge>
              <p>Provider: {config.provider}</p>
              <p>Key: <code data-testid="kms-keyref">{maskKeyRef(config.keyRef)}</code></p>
              {config.region && <p>Region: {config.region}</p>}
              <p>External ID: <code>{config.externalId}</code></p>
              {config.lastVerifiedAt && <p>Last verified: {config.lastVerifiedAt}</p>}
              {config.lastErrorCode && <p>Last error: {config.lastErrorCode}</p>}
            </div>
          ) : (
            <p data-testid="kms-empty" className="text-sm">
              No external key configured. Your data uses the platform-managed key.
            </p>
          )}
          <p className="text-sm mt-2">Data keys: {status.keys.length}</p>
        </CardBody>
      </Card>

      <Card>
        <CardBody>
          <form onSubmit={submit} noValidate autoComplete="off" className="space-y-5">
            <h2 className="text-sm font-semibold">{config ? 'Replace external key' : 'Configure external key'}</h2>
            <SelectField label="Provider" value={selected} onChange={(e) => setProvider(e.target.value)}>
              {providers.map((p) => <option key={p} value={p}>{p}</option>)}
            </SelectField>
            {principalHint && (
              <p className="text-sm" data-testid="kms-principal-hint">
                Grant this identity access to your key: <code>{principalHint}</code>
              </p>
            )}
            <TextField label="Key identifier" value={keyRef} onChange={(e) => setKeyRef(e.target.value)} autoComplete="off" required />
            <TextField label="Region" value={region} onChange={(e) => setRegion(e.target.value)} autoComplete="off" />
            <TextField label="Principal" value={principal} onChange={(e) => setPrincipal(e.target.value)} autoComplete="off" />
            <Button type="submit" variant="primary" loading={configure.isPending} disabled={busy || keyRef.trim() === '' || !selected}>
              Save key
            </Button>
          </form>
        </CardBody>
      </Card>

      {config && (
        <div className="flex gap-3">
          {config.status === 'pending' && (
            <Button variant="primary" loading={activate.isPending} disabled={busy} onClick={() => activate.mutate()}>
              Activate key
            </Button>
          )}
          <Button variant="danger" disabled={busy} onClick={() => { setPending('remove'); setTyped(''); }}>
            Remove external key
          </Button>
        </div>
      )}

      {pending && (
        <Card>
          <CardBody>
            <div role="dialog" aria-label="Confirm" className="space-y-3">
              <p className="text-sm">
                {pending === 'remove'
                  ? 'This re-wraps every data key back to the platform key and removes your external key.'
                  : 'This replaces the active external key and re-wraps data keys.'}{' '}
                Type <strong>{CONFIRM_WORD[pending]}</strong> to confirm.
              </p>
              <TextField label="Confirmation" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
              <div className="flex gap-3">
                <Button variant="danger" disabled={!confirmOk} onClick={runConfirmed}>Confirm</Button>
                <Button onClick={() => { setPending(null); setTyped(''); }}>Cancel</Button>
              </div>
            </div>
          </CardBody>
        </Card>
      )}
    </div>
  );
}
