/** Tests for the ingest-source API client: UUID-only ids in paths/DTOs, numeric ids rejected. */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from '../../lib/apiClient';
import { tenantIngestSourcesApi } from '../tenantIngestSourcesApi';

const COMMUNITY = '9c3d7e10-2b4a-4d8f-8e65-1a7f4b9c0d22';
const SOURCE = '5b8f0a52-6c1d-4f6e-9a57-0d3b6f1c2e41';
const WIRE = {
  id: SOURCE,
  platform: 'twitch',
  sourceId: 'chan_1',
  label: 'Main',
  enabled: true,
  communityId: COMMUNITY,
  createdAt: '2026-11-17T00:00:00+00:00',
  updatedAt: '2026-11-17T00:00:00+00:00',
};

describe('tenantIngestSourcesApi', () => {
  afterEach(() => vi.restoreAllMocks());

  it('list GETs the community-UUID path and maps UUID ids + UUID cursor', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { success: true, ingestSources: [WIRE], meta: { limit: 50, nextCursor: SOURCE } },
    });
    const page = await tenantIngestSourcesApi.list(COMMUNITY, { cursor: SOURCE });
    expect(get).toHaveBeenCalledWith(`/api/v1/communities/${COMMUNITY}/ingest-sources`, { params: { cursor: SOURCE } });
    expect(page.sources[0]).toEqual(WIRE);
    expect(typeof page.sources[0]?.id).toBe('string');
    expect(page.nextCursor).toBe(SOURCE);
  });

  it('create POSTs only platform/sourceId/label', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: { success: true, ingestSource: WIRE } });
    const created = await tenantIngestSourcesApi.create(COMMUNITY, { platform: 'twitch', sourceId: ' chan_1 ', label: ' Main ' });
    expect(post).toHaveBeenCalledWith(`/api/v1/communities/${COMMUNITY}/ingest-sources`, {
      platform: 'twitch',
      sourceId: 'chan_1',
      label: 'Main',
    });
    expect(created.id).toBe(SOURCE);
  });

  it('update PATCHes and remove DELETEs the source-UUID path', async () => {
    const patch = vi.spyOn(apiClient, 'patch').mockResolvedValue({ data: { success: true, ingestSource: WIRE } });
    const del = vi.spyOn(apiClient, 'delete').mockResolvedValue({ data: '' });
    await tenantIngestSourcesApi.update(COMMUNITY, SOURCE, { enabled: false });
    await tenantIngestSourcesApi.remove(COMMUNITY, SOURCE);
    expect(patch).toHaveBeenCalledWith(`/api/v1/communities/${COMMUNITY}/ingest-sources/${SOURCE}`, { enabled: false });
    expect(del).toHaveBeenCalledWith(`/api/v1/communities/${COMMUNITY}/ingest-sources/${SOURCE}`);
  });

  it('rejects a response whose id is numeric (the old, enumerable shape)', async () => {
    vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { success: true, ingestSources: [{ ...WIRE, id: 7 }], meta: { limit: 50, nextCursor: null } },
    });
    await expect(tenantIngestSourcesApi.list(COMMUNITY)).rejects.toThrow();
  });

  it('encodes a communityId / source id that contains path characters', async () => {
    const del = vi.spyOn(apiClient, 'delete').mockResolvedValue({ data: null });
    await tenantIngestSourcesApi.remove('a/b?x=1', 'c/d');
    expect(del).toHaveBeenCalledWith('/api/v1/communities/a%2Fb%3Fx%3D1/ingest-sources/c%2Fd');
  });
});
