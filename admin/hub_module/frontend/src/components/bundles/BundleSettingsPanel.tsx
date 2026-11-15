import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import BundleSettingsForm from './BundleSettingsForm';
import { describeError, settingsKeys } from './settingsHelpers';
import {
  BundleAdminError,
  NO_CONFIG_CODE,
  bundleAdminApi,
  type ConfigFieldError,
} from '../../services/bundleAdminApi';

const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : 'never');

interface Props {
  communityId: number;
  appId: string;
  canEdit: boolean;
  onClose: () => void;
}

/**
 * Detail panel for one activated bundle: schema-driven form, save/reset, version +
 * "last changed", and history. Admins edit; moderators get a read-only form.
 */
export default function BundleSettingsPanel({ communityId, appId, canEdit, onClose }: Props) {
  const qc = useQueryClient();
  const descriptor = useQuery({
    queryKey: settingsKeys.descriptor(communityId, appId),
    queryFn: () => bundleAdminApi.descriptor(communityId, appId),
  });
  const schema = descriptor.data?.configSchema ?? null;
  const config = useQuery({
    queryKey: settingsKeys.config(communityId, appId),
    queryFn: () => bundleAdminApi.getConfig(communityId, appId),
    enabled: schema !== null,
  });
  const history = useQuery({
    queryKey: settingsKeys.history(communityId, appId),
    queryFn: () => bundleAdminApi.history(communityId, appId),
    enabled: schema !== null,
  });

  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [fieldErrors, setFieldErrors] = useState<ConfigFieldError[]>([]);
  const [conflict, setConflict] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (config.data) setDraft(config.data.config);
  }, [config.data]);

  const save = useMutation({
    mutationFn: (value: Record<string, unknown>) =>
      bundleAdminApi.saveConfig(communityId, appId, value, config.data?.version ?? 0),
    onMutate: () => {
      setFieldErrors([]);
      setConflict(false);
      setSaved(false);
    },
    onSuccess: (data) => {
      console.debug(`[BundleSettingsPanel] Save { appId: "${appId}", version: ${data.version} }`);
      qc.setQueryData(settingsKeys.config(communityId, appId), data);
      void qc.invalidateQueries({ queryKey: settingsKeys.history(communityId, appId) });
      setSaved(true);
    },
    onError: (err) => {
      if (err instanceof BundleAdminError) {
        console.warn(`[BundleSettingsPanel] SaveFailed { appId: "${appId}", status: ${err.status} }`);
        if (err.status === 409) setConflict(true);
        if (err.status === 422) setFieldErrors(err.fieldErrors);
      }
    },
  });

  const reload = async () => {
    setConflict(false);
    setFieldErrors([]);
    await config.refetch();
    void history.refetch();
  };

  const noConfig =
    schema === null ||
    (config.error instanceof BundleAdminError && config.error.code === NO_CONFIG_CODE);
  const loadError = descriptor.error ?? (noConfig ? null : config.error);

  return (
    <section
      aria-label={`Settings for ${appId}`}
      data-testid="bundle-settings-panel"
      className="card p-4 sm:p-6"
    >
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-gold-400">{appId}</h2>
          {config.data && (
            <p className="text-sm text-fg-muted" data-testid="config-meta">
              Version {config.data.version} · Last changed {fmt(config.data.createdAt)}
            </p>
          )}
        </div>
        <button type="button" onClick={onClose} className="btn btn-secondary text-sm">
          Close
        </button>
      </div>

      {descriptor.isLoading && <p className="text-fg-muted">Loading…</p>}
      {loadError && (
        <p role="alert" className="text-red-300">
          {describeError(loadError)}
        </p>
      )}
      {!descriptor.isLoading && !loadError && noConfig && (
        <p data-testid="no-settings" className="text-fg-muted">
          This app has no configurable settings.
        </p>
      )}

      {conflict && (
        <div role="alert" className="mb-4 rounded border border-amber-600 bg-amber-900/30 p-3 text-amber-200">
          Someone else saved these settings since you opened them.{' '}
          <button type="button" onClick={() => void reload()} className="underline">
            Reload latest
          </button>
        </div>
      )}
      {save.isError && !conflict && fieldErrors.length === 0 && (
        <p role="alert" className="mb-4 text-red-300">{describeError(save.error)}</p>
      )}
      {fieldErrors.length > 0 && (
        <p role="alert" className="mb-4 text-red-300">The server rejected some values. See the highlighted fields.</p>
      )}
      {saved && <p role="status" className="mb-4 text-emerald-300">Settings saved.</p>}
      {!canEdit && schema && (
        <p className="mb-4 text-sm text-fg-muted">Read-only: only community admins can change settings.</p>
      )}

      {schema && config.data && (
        <BundleSettingsForm
          schema={schema}
          value={draft}
          readOnly={!canEdit}
          serverErrors={fieldErrors}
          onChange={setDraft}
          onSubmit={(v) => save.mutate(v)}
        >
          {canEdit ? (
            <div className="mt-4 flex gap-2">
              <button type="submit" disabled={save.isPending} className="btn btn-primary">
                {save.isPending ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setDraft(config.data.config);
                  setFieldErrors([]);
                }}
                className="btn btn-secondary"
              >
                Reset
              </button>
            </div>
          ) : (
            <span />
          )}
        </BundleSettingsForm>
      )}

      {history.data && history.data.length > 0 && (
        <details className="mt-6">
          <summary className="cursor-pointer text-sm text-fg-muted">History ({history.data.length})</summary>
          <ol className="mt-2 space-y-1 text-sm" data-testid="config-history">
            {history.data.map((h) => (
              <li key={h.version}>
                v{h.version} · {fmt(h.createdAt)}
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}
