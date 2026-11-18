import { apiClient } from '../lib/apiClient';
import { expectRecord, readArray, readBool, readOptString, readString } from '../lib/wire';

/**
 * Typed client for the community-scoped ingest-source registry
 * (`hub_api/blueprints/v1/ingest_sources.py`). The registry carries NO credential of any
 * kind (the server rejects unknown body fields), so nothing here sends or reads a secret.
 * Ids are `public_id` UUIDs only: a source's `id` and its `communityId` are UUID strings and the
 * source id is the path segment for PATCH/DELETE (the list cursor is a source UUID too). The
 * numeric surrogate keys never cross the API; hub-api resolves them server-side behind the
 * tenant fence and answers any unknown/foreign/numeric id with 404.
 */

export const INGEST_PLATFORMS = ['discord', 'twitch', 'kick', 'youtube', 'slack'] as const;
export type IngestPlatform = (typeof INGEST_PLATFORMS)[number];

export const SOURCE_ID_MAX_LEN = 255;
export const LABEL_MAX_LEN = 255;
/** Mirrors the server's `_SOURCE_ID_RE`. */
export const SOURCE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,255}$/;

export interface IngestSource {
  /** The source's `public_id` UUID. */
  id: string;
  platform: string;
  sourceId: string;
  label: string;
  enabled: boolean;
  /** The owning community's `public_id` UUID. */
  communityId: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface IngestSourcePage {
  sources: IngestSource[];
  nextCursor: string | null;
}

export interface CreateIngestSourceInput {
  platform: string;
  sourceId: string;
  label: string;
}

export interface UpdateIngestSourceInput {
  label?: string;
  enabled?: boolean;
}

export interface IngestSourceFormErrors {
  platform?: string;
  sourceId?: string;
  label?: string;
}

/** Client-side mirror of the server DTO constraints; returns `{}` when valid. */
export function validateIngestSource(input: CreateIngestSourceInput): IngestSourceFormErrors {
  const errors: IngestSourceFormErrors = {};
  if (!(INGEST_PLATFORMS as readonly string[]).includes(input.platform)) {
    errors.platform = `Platform must be one of: ${INGEST_PLATFORMS.join(', ')}`;
  }
  if (!SOURCE_ID_PATTERN.test(input.sourceId.trim())) {
    errors.sourceId = `Source ID must be 1-${SOURCE_ID_MAX_LEN} characters: letters, digits, . _ : -`;
  }
  const label = input.label.trim();
  if (label === '' || label.length > LABEL_MAX_LEN) {
    errors.label = `Label must be 1-${LABEL_MAX_LEN} characters`;
  }
  return errors;
}

function parseSource(raw: unknown): IngestSource {
  const r = expectRecord(raw, 'ingest source');
  return {
    id: readString(r, 'id', 'ingest source'),
    platform: readString(r, 'platform', 'ingest source'),
    sourceId: readString(r, 'sourceId', 'ingest source'),
    label: readString(r, 'label', 'ingest source'),
    enabled: readBool(r, 'enabled', false),
    communityId: readOptString(r, 'communityId'),
    createdAt: readOptString(r, 'createdAt') ?? '',
    updatedAt: readOptString(r, 'updatedAt') ?? '',
  };
}

const base = (communityId: string) => `/api/v1/communities/${encodeURIComponent(String(communityId))}/ingest-sources`;

export const tenantIngestSourcesApi = {
  /** `GET /api/v1/communities/{communityId}/ingest-sources` (scope tenant:read), cursor-paginated. */
  list: async (communityId: string, params: { cursor?: string; limit?: number } = {}): Promise<IngestSourcePage> => {
    const { data } = await apiClient.get<unknown>(base(communityId), { params });
    const body = expectRecord(data, 'ingest source list');
    const meta = expectRecord(body['meta'] ?? {}, 'ingest source list meta');
    return {
      sources: readArray(body, 'ingestSources', 'ingest source list').map(parseSource),
      nextCursor: readOptString(meta, 'nextCursor'),
    };
  },

  /** `POST /api/v1/communities/{communityId}/ingest-sources` (scope tenant:admin) -> 201. */
  create: async (communityId: string, input: CreateIngestSourceInput): Promise<IngestSource> => {
    const { data } = await apiClient.post<unknown>(base(communityId), {
      platform: input.platform,
      sourceId: input.sourceId.trim(),
      label: input.label.trim(),
    });
    return parseSource(expectRecord(data, 'create ingest source')['ingestSource']);
  },

  /** `PATCH /api/v1/communities/{communityId}/ingest-sources/{sourceUuid}` -- only `label` and `enabled`. */
  update: async (communityId: string, ingestSourceId: string, input: UpdateIngestSourceInput): Promise<IngestSource> => {
    const { data } = await apiClient.patch<unknown>(`${base(communityId)}/${encodeURIComponent(ingestSourceId)}`, input);
    return parseSource(expectRecord(data, 'update ingest source')['ingestSource']);
  },

  /** `DELETE /api/v1/communities/{communityId}/ingest-sources/{sourceUuid}` -> 204. */
  remove: async (communityId: string, ingestSourceId: string): Promise<void> => {
    await apiClient.delete(`${base(communityId)}/${encodeURIComponent(ingestSourceId)}`);
  },
};
