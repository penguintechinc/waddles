import { afterEach, describe, expect, it } from 'vitest';
import { apiClient } from '../../lib/apiClient';
import { stubHttp } from '../../test/httpStub';
import {
  createKnowledgeSource,
  deleteKnowledgeSource,
  fetchKnowledgeSources,
  reindexKnowledgeSource,
  updateKnowledgeSource,
  type KnowledgeSourceForm,
} from '../aiKnowledgeApi';

const BASE = '/api/v1/admin/ai-knowledge/sources';
let http: ReturnType<typeof stubHttp> | undefined;
afterEach(() => http?.restore());

const FORM: KnowledgeSourceForm = {
  name: 'Wiki',
  sourceType: 'github_wiki',
  url: 'https://github.com/o/r',
  branch: 'main',
  docsPath: '/docs',
  refreshInterval: 'daily',
  token: 'ghp_x',
};

function row(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 4,
    source_name: 'Wiki',
    source_type: 'github_wiki',
    source_url: 'https://github.com/o/r',
    branch: 'main',
    docs_path: '/docs',
    refresh_interval: 'weekly',
    is_active: true,
    last_indexed_at: null,
    indexed_page_count: 3,
    index_errors: null,
    ...over,
  };
}

describe('aiKnowledgeApi (served /api/v1/admin/ai-knowledge routes)', () => {
  it('lists with ?communityId and derives status from index_errors / last_indexed_at', async () => {
    http = stubHttp([apiClient], {
      [`GET ${BASE}`]: {
        sources: [
          row(),
          row({ id: 5, last_indexed_at: '2026-01-01T00:00:00Z' }),
          row({ id: 6, index_errors: 'boom', source_url: null, branch: null, docs_path: null, refresh_interval: null, indexed_page_count: null }),
        ],
      },
    });
    const sources = await fetchKnowledgeSources(7);
    expect(http.seen[0]).toMatchObject({ method: 'GET', url: BASE, params: { communityId: 7 } });
    expect(sources.map((s) => s.status)).toEqual(['pending', 'indexed', 'error']);
    expect(sources[0]).toMatchObject({ id: 4, name: 'Wiki', sourceType: 'github_wiki', pageCount: 3, refreshInterval: 'weekly' });
    expect(sources[2]).toMatchObject({ url: null, branch: '', docsPath: '', pageCount: 0, refreshInterval: 'weekly' });
  });

  it('rejects a malformed payload loudly', async () => {
    http = stubHttp([apiClient], { [`GET ${BASE}`]: { sources: [{ id: 'x' }] } });
    await expect(fetchKnowledgeSources(1)).rejects.toThrow();
  });

  it('POST maps the form to snake_case and adds community_id', async () => {
    http = stubHttp([apiClient], { [`POST ${BASE}`]: {} });
    await createKnowledgeSource(7, FORM);
    expect(http.seen[0]).toMatchObject({
      method: 'POST',
      url: BASE,
      body: {
        source_name: 'Wiki',
        source_type: 'github_wiki',
        refresh_interval: 'daily',
        source_url: 'https://github.com/o/r',
        branch: 'main',
        docs_path: '/docs',
        encrypted_token: 'ghp_x',
        community_id: 7,
      },
    });
  });

  it('omits blank optionals so server defaults apply', async () => {
    http = stubHttp([apiClient], { [`POST ${BASE}`]: {} });
    await createKnowledgeSource(7, { ...FORM, url: '', branch: '', docsPath: '', token: '' });
    expect(http.seen[0]?.body).toEqual({ source_name: 'Wiki', source_type: 'github_wiki', refresh_interval: 'daily', community_id: 7 });
  });

  it('PUT never sends source_type (immutable server-side)', async () => {
    http = stubHttp([apiClient], { [`PUT ${BASE}/4`]: {} });
    await updateKnowledgeSource(4, FORM);
    expect(http.seen[0]?.method).toBe('PUT');
    expect(http.seen[0]?.url).toBe(`${BASE}/4`);
    expect(http.seen[0]?.body).not.toHaveProperty('source_type');
    expect(http.seen[0]?.body).toMatchObject({ source_name: 'Wiki' });
  });

  it('DELETE and reindex hit the id routes', async () => {
    http = stubHttp([apiClient], { [`DELETE ${BASE}/4`]: {}, [`POST ${BASE}/4/reindex`]: {} });
    await deleteKnowledgeSource(4);
    await reindexKnowledgeSource(4);
    expect(http.seen.map((r) => `${r.method} ${r.url}`)).toEqual([`DELETE ${BASE}/4`, `POST ${BASE}/4/reindex`]);
  });
});
