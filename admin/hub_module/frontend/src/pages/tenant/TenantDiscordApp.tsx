import { useState } from 'react';
import type { FormEvent } from 'react';
import { useParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';

import { Alert, Badge, Button, Card, CardBody, LoadingState, PageHeader, TextField } from '../../components/ui';
import { getErrorMessage } from '../../components/ui';
import { tenantDiscordInstallApi } from '../../services/tenantDiscordInstallApi';

const DEFAULT_TENANT_SLUG = 'default';
const CALLBACK_PATH = '/api/v1/discord/install/callback';

/** Pulls the standardized error `code` out of hub-api's `{ error: { code, message } }` envelope. */
function errorCode(err: unknown): string | null {
  const data = (err as { response?: { data?: { error?: { code?: unknown } } } })?.response?.data;
  const code = data?.error?.code;
  return typeof code === 'string' ? code : null;
}

/**
 * Tenant settings -> "Discord app (your own bot)". Lets a tenant admin supply
 * their own Discord application credentials, then sends them through Discord's
 * authorize flow. Secret and token are write-only: never prefilled, never
 * echoed, cleared after submit. All provider logic lives in hub-api.
 */
export default function TenantDiscordApp() {
  const { tenantSlug } = useParams<{ tenantSlug: string }>();
  const isDefaultTenant = tenantSlug === DEFAULT_TENANT_SLUG;
  const [applicationId, setApplicationId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [botToken, setBotToken] = useState('');

  const statusQuery = useQuery({
    queryKey: ['tenantDiscordInstallStatus', tenantSlug ?? ''],
    queryFn: () => tenantDiscordInstallApi.getStatus(),
    enabled: !isDefaultTenant,
    retry: false,
  });

  const install = useMutation({
    mutationFn: () =>
      tenantDiscordInstallApi.authorize({
        applicationId: applicationId.trim(),
        clientSecret,
        botToken: botToken === '' ? null : botToken,
      }),
    onSuccess: (authorizeUrl) => {
      window.location.assign(authorizeUrl);
    },
    onSettled: () => {
      // Write-only secrets never linger in component state after a submit.
      setClientSecret('');
      setBotToken('');
    },
  });

  if (isDefaultTenant) {
    return (
      <div className="p-6 max-w-2xl">
        <PageHeader title="Discord app (your own bot)" />
        <Alert tone="info">
          The default tenant uses the shared Waddles Discord app and cannot install its own.
        </Alert>
      </div>
    );
  }

  const status = statusQuery.data;
  const redirectUri = status?.redirectUri ?? `${window.location.origin}${CALLBACK_PATH}`;
  const canSubmit = applicationId.trim() !== '' && clientSecret !== '' && !install.isPending;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    install.mutate();
  };

  const code = install.isError ? errorCode(install.error) : null;
  const message = install.isError
    ? code === 'TENANT_DISCORD_APP_MISSING'
      ? 'No Discord app is configured for this tenant yet. Enter your application ID and client secret to install it.'
      : getErrorMessage(install.error, 'Failed to start the Discord install')
    : null;

  return (
    <div className="p-6 max-w-2xl space-y-6">
      <PageHeader
        title="Discord app (your own bot)"
        description="Run Waddles through your own Discord application so the bot appears under your own name and branding."
      />

      <Card>
        <CardBody>
          <h2 className="text-sm font-semibold mb-2">Connection status</h2>
          {statusQuery.isLoading && <LoadingState label="Loading connection status" />}
          {statusQuery.isError && (
            <div data-testid="discord-status-error">
              <Alert tone="danger">{getErrorMessage(statusQuery.error, 'Failed to load connection status')}</Alert>
            </div>
          )}
          {status && (
            <div data-testid="discord-status" className="space-y-1 text-sm">
              <Badge tone={status.installed ? 'green' : 'gray'}>
                {status.installed ? 'Installed' : 'Not installed'}
              </Badge>
              {status.applicationId && <p>Application ID: <code>{status.applicationId}</code></p>}
              {status.connectionStatus && <p>Connection: {status.connectionStatus}</p>}
              {status.updatedAt && <p>Last updated: {status.updatedAt}</p>}
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardBody>
          <form onSubmit={onSubmit} noValidate autoComplete="off" className="space-y-5">
            <div data-testid="discord-redirect-uri">
              <p className="text-sm">
                Before installing, add this <strong>Redirect URI</strong> under OAuth2 in your app on the Discord
                Developer Portal, exactly as shown:
              </p>
              <code className="block mt-1 break-all">{redirectUri}</code>
            </div>
            <TextField
              label="Application ID"
              value={applicationId}
              onChange={(e) => setApplicationId(e.target.value)}
              autoComplete="off"
              required
            />
            <TextField
              label="Client secret"
              type="password"
              value={clientSecret}
              onChange={(e) => setClientSecret(e.target.value)}
              autoComplete="new-password"
              hint="Write-only. Never shown again after you submit."
              required
            />
            <TextField
              label="Bot token"
              type="password"
              value={botToken}
              onChange={(e) => setBotToken(e.target.value)}
              autoComplete="new-password"
              hint="Optional. Write-only. Never shown again after you submit."
            />
            {message && (
              <div data-testid="discord-install-error">
                <Alert tone="danger">{message}</Alert>
              </div>
            )}
            <Button type="submit" variant="primary" loading={install.isPending} disabled={!canSubmit}>
              Install to Discord
            </Button>
          </form>
        </CardBody>
      </Card>
    </div>
  );
}
