import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  Alert, Badge, Button, Card, CardBody, EmptyState, LoadingState, PageHeader, SelectField, TextField,
} from '../../components/ui';
import { getErrorMessage } from '../../components/ui';
import { tenantApi } from '../../services/api';
import {
  INGEST_PLATFORMS, LABEL_MAX_LEN, SOURCE_ID_MAX_LEN, tenantIngestSourcesApi, validateIngestSource,
} from '../../services/tenantIngestSourcesApi';

const EMPTY_FORM = { platform: 'twitch', sourceId: '', label: '' };

/**
 * Tenant admin: community ingest-source registry. Lists, adds, enables/disables, renames
 * and removes inbound event sources. The registry holds no credentials, so there is no
 * secret input here and none is ever rendered.
 */
export default function TenantIngestSources() {
  const { tenantSlug } = useParams();
  const qc = useQueryClient();
  const [communityId, setCommunityId] = useState('');
  const [form, setForm] = useState(EMPTY_FORM);
  const [errors, setErrors] = useState({});
  const [editing, setEditing] = useState(null);
  const [editLabel, setEditLabel] = useState('');
  const [confirmRemove, setConfirmRemove] = useState(null);

  const communitiesQuery = useQuery({
    queryKey: ['tenantIngestCommunities', tenantSlug ?? ''],
    queryFn: async () => (await tenantApi.getCommunities(tenantSlug, { limit: 100 })).data?.communities ?? [],
    retry: false,
  });
  const communities = useMemo(() => communitiesQuery.data ?? [], [communitiesQuery.data]);
  useEffect(() => {
    if (communityId === '' && communities.length > 0) setCommunityId(communities[0].publicId);
  }, [communities, communityId]);

  const cid = communityId === '' ? null : communityId;
  const listKey = ['tenantIngestSources', cid];
  const listQuery = useQuery({
    queryKey: listKey,
    queryFn: () => tenantIngestSourcesApi.list(cid),
    enabled: cid !== null,
    retry: false,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: listKey });

  const create = useMutation({
    mutationFn: () => tenantIngestSourcesApi.create(cid, form),
    onSuccess: () => { setForm(EMPTY_FORM); setErrors({}); refresh(); },
  });
  const update = useMutation({
    mutationFn: ({ id, patch }) => tenantIngestSourcesApi.update(cid, id, patch),
    onSuccess: () => { setEditing(null); refresh(); },
  });
  const remove = useMutation({
    mutationFn: (id) => tenantIngestSourcesApi.remove(cid, id),
    onSuccess: () => { setConfirmRemove(null); refresh(); },
  });

  const forbidden = (err) => err?.response?.status === 403;
  const errMsg = (err, fallback) =>
    forbidden(err) ? 'You do not have permission to manage ingest sources (tenant admin required).' : getErrorMessage(err, fallback);

  const onSubmit = (e) => {
    e.preventDefault();
    const v = validateIngestSource(form);
    setErrors(v);
    if (Object.keys(v).length === 0) create.mutate();
  };

  const sources = listQuery.data?.sources ?? [];

  return (
    <div className="p-6 max-w-4xl space-y-6">
      <PageHeader
        title="Ingest sources"
        description="Inbound event sources (chat channels, servers) that feed a community. No credentials are stored here."
      />

      {communitiesQuery.isLoading && <LoadingState label="Loading communities" />}
      {communitiesQuery.isError && (
        <div data-testid="ingest-communities-error">
          <Alert tone="danger">{errMsg(communitiesQuery.error, 'Failed to load communities')}</Alert>
        </div>
      )}
      {communitiesQuery.isSuccess && communities.length === 0 && (
        <EmptyState title="No communities" description="Create a community before registering ingest sources." />
      )}

      {communities.length > 0 && (
        <SelectField label="Community" value={communityId} onChange={(e) => setCommunityId(e.target.value)}>
          {communities.map((c) => (
            <option key={c.publicId} value={c.publicId}>{c.displayName || c.name}</option>
          ))}
        </SelectField>
      )}

      {cid !== null && (
        <>
          <Card>
            <CardBody>
              <form onSubmit={onSubmit} noValidate autoComplete="off" className="space-y-4" aria-label="Add ingest source">
                <h2 className="text-sm font-semibold">Add source</h2>
                <SelectField label="Platform" value={form.platform} error={errors.platform}
                  onChange={(e) => setForm({ ...form, platform: e.target.value })}>
                  {INGEST_PLATFORMS.map((p) => <option key={p} value={p}>{p}</option>)}
                </SelectField>
                <TextField label="Source ID" value={form.sourceId} error={errors.sourceId} maxLength={SOURCE_ID_MAX_LEN}
                  hint="Platform channel or server identifier. Not a token or password."
                  onChange={(e) => setForm({ ...form, sourceId: e.target.value })} required />
                <TextField label="Label" value={form.label} error={errors.label} maxLength={LABEL_MAX_LEN}
                  onChange={(e) => setForm({ ...form, label: e.target.value })} required />
                {create.isError && <Alert tone="danger">{errMsg(create.error, 'Failed to add ingest source')}</Alert>}
                <Button type="submit" disabled={create.isPending}>Add source</Button>
              </form>
            </CardBody>
          </Card>

          {listQuery.isLoading && <LoadingState label="Loading ingest sources" />}
          {listQuery.isError && (
            <div data-testid="ingest-list-error">
              <Alert tone="danger">{errMsg(listQuery.error, 'Failed to load ingest sources')}</Alert>
            </div>
          )}
          {listQuery.isSuccess && sources.length === 0 && (
            <EmptyState title="No ingest sources" description="Add a source above to start receiving events." />
          )}
          {update.isError && <Alert tone="danger">{errMsg(update.error, 'Failed to update ingest source')}</Alert>}
          {remove.isError && <Alert tone="danger">{errMsg(remove.error, 'Failed to remove ingest source')}</Alert>}

          {sources.length > 0 && (
            <ul className="space-y-3" aria-label="Ingest sources">
              {sources.map((s) => (
                <li key={s.id} data-testid={`ingest-source-${s.id}`}>
                  <Card>
                    <CardBody>
                      <div className="flex flex-wrap items-center gap-3">
                        <Badge tone="gray">{s.platform}</Badge>
                        {editing === s.id ? (
                          <TextField label="Label" hideLabel value={editLabel} maxLength={LABEL_MAX_LEN}
                            onChange={(e) => setEditLabel(e.target.value)} />
                        ) : (
                          <strong>{s.label}</strong>
                        )}
                        <code className="text-xs">{s.sourceId}</code>
                        <Badge tone={s.enabled ? 'green' : 'gray'}>{s.enabled ? 'Enabled' : 'Disabled'}</Badge>
                        <div className="ml-auto flex gap-2">
                          {editing === s.id ? (
                            <>
                              <Button size="sm" disabled={editLabel.trim() === '' || update.isPending}
                                onClick={() => update.mutate({ id: s.id, patch: { label: editLabel.trim() } })}>Save</Button>
                              <Button size="sm" variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>
                            </>
                          ) : (
                            <>
                              <Button size="sm" variant="secondary"
                                onClick={() => { setEditing(s.id); setEditLabel(s.label); }}>Edit</Button>
                              <Button size="sm" variant="secondary" disabled={update.isPending}
                                onClick={() => update.mutate({ id: s.id, patch: { enabled: !s.enabled } })}>
                                {s.enabled ? 'Disable' : 'Enable'}
                              </Button>
                              <Button size="sm" variant="danger" onClick={() => setConfirmRemove(s.id)}>Remove</Button>
                            </>
                          )}
                        </div>
                      </div>
                      {confirmRemove === s.id && (
                        <div role="alertdialog" aria-label="Confirm removal" className="mt-3 space-y-2">
                          <Alert tone="warning">Remove this source? Its workstream will be disabled. This cannot be undone.</Alert>
                          <Button size="sm" variant="danger" disabled={remove.isPending}
                            onClick={() => remove.mutate(s.id)}>Confirm remove</Button>{' '}
                          <Button size="sm" variant="secondary" onClick={() => setConfirmRemove(null)}>Cancel</Button>
                        </div>
                      )}
                    </CardBody>
                  </Card>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
