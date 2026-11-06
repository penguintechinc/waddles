import { apiClient } from '../lib/apiClient';
import { expectRecord, readArray, readBool, readNumber, readOptNumber, readOptString, readString } from '../lib/wire';

// Typed client for AI-knowledge sources, mirroring hub_api/blueprints/v1/bot.py
// (bot_bp, url_prefix /api/v1/admin). Served routes only:
//   GET|POST /api/v1/admin/ai-knowledge/sources        (list takes ?communityId=)
//   PUT|DELETE /api/v1/admin/ai-knowledge/sources/:id
//   POST /api/v1/admin/ai-knowledge/sources/:id/reindex
// hub-api has NO per-community settings/health routes; those UI parts are gated by the
// `ai-knowledge-settings` / `ai-knowledge-health` entries in unservedFeatures.json.

const BASE = '/api/v1/admin/ai-knowledge/sources';

export type KnowledgeSourceStatus = 'indexed' | 'error' | 'pending';

export interface KnowledgeSource {
  id: number;
  name: string;
  sourceType: string;
  url: string | null;
  branch: string;
  docsPath: string;
  refreshInterval: string;
  isActive: boolean;
  status: KnowledgeSourceStatus;
  pageCount: number;
  lastIndexed: string | null;
}

export interface KnowledgeSourceForm {
  name: string;
  sourceType: string;
  url: string;
  branch: string;
  docsPath: string;
  refreshInterval: string;
  token: string;
}

/** Index state is derived: the wire row carries `index_errors` / `last_indexed_at`, not a status. */
function parseSource(raw: unknown): KnowledgeSource {
  const row = expectRecord(raw, 'knowledge source');
  const errors = readOptString(row, 'index_errors');
  const lastIndexed = readOptString(row, 'last_indexed_at');
  return {
    id: readNumber(row, 'id', 'knowledge source'),
    name: readString(row, 'source_name', 'knowledge source'),
    sourceType: readString(row, 'source_type', 'knowledge source'),
    url: readOptString(row, 'source_url'),
    branch: readOptString(row, 'branch') ?? '',
    docsPath: readOptString(row, 'docs_path') ?? '',
    refreshInterval: readOptString(row, 'refresh_interval') ?? 'weekly',
    isActive: readBool(row, 'is_active', true),
    status: errors ? 'error' : lastIndexed ? 'indexed' : 'pending',
    pageCount: readOptNumber(row, 'indexed_page_count') ?? 0,
    lastIndexed,
  };
}

/** Maps the form to the snake_case wire body; blank optionals are omitted so server defaults apply. */
function toWire(form: KnowledgeSourceForm): Record<string, unknown> {
  const body: Record<string, unknown> = {
    source_name: form.name,
    source_type: form.sourceType,
    refresh_interval: form.refreshInterval,
  };
  if (form.url) body['source_url'] = form.url;
  if (form.branch) body['branch'] = form.branch;
  if (form.docsPath) body['docs_path'] = form.docsPath;
  if (form.token) body['encrypted_token'] = form.token;
  return body;
}

/** `GET /api/v1/admin/ai-knowledge/sources?communityId=`. */
export async function fetchKnowledgeSources(communityId: number): Promise<KnowledgeSource[]> {
  const { data } = await apiClient.get<unknown>(BASE, { params: { communityId } });
  return readArray(expectRecord(data, 'knowledge sources'), 'sources', 'knowledge sources').map(parseSource);
}

/** `POST /api/v1/admin/ai-knowledge/sources`. */
export async function createKnowledgeSource(communityId: number, form: KnowledgeSourceForm): Promise<void> {
  await apiClient.post(BASE, { ...toWire(form), community_id: communityId });
}

/** `PUT /api/v1/admin/ai-knowledge/sources/:id`. */
export async function updateKnowledgeSource(id: number, form: KnowledgeSourceForm): Promise<void> {
  const { source_type: _type, ...body } = toWire(form);
  await apiClient.put(`${BASE}/${id}`, body);
}

/** `DELETE /api/v1/admin/ai-knowledge/sources/:id`. */
export async function deleteKnowledgeSource(id: number): Promise<void> {
  await apiClient.delete(`${BASE}/${id}`);
}

/** `POST /api/v1/admin/ai-knowledge/sources/:id/reindex`. */
export async function reindexKnowledgeSource(id: number): Promise<void> {
  await apiClient.post(`${BASE}/${id}/reindex`);
}
