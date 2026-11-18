# Tenant ingest sources (hub-webui)

Tenant admins manage a community's inbound event sources (Twitch/Discord/Kick/YouTube/Slack) in the webui.

- Page: `src/pages/tenant/TenantIngestSources.jsx`; client: `src/services/tenantIngestSourcesApi.ts`.
- Backend (`hub_api/blueprints/v1/ingest_sources.py`), all under `/api/v1/communities/{communityId}/ingest-sources`
  (`{communityId}` is the community's `public_id` UUID):
  `GET` (scope `tenant:read`, cursor-paginated), `POST` (`tenant:admin`, body `platform`, `sourceId`, `label`, 201),
  `PATCH /{sourceId}` (`label` and/or `enabled`), `DELETE /{sourceId}` (204, disables the source's workstream).
- Ids on the wire are `public_id` UUIDs only (migration `0122_ingest_sources_public_id`): the DTO's `id` is the
  source's UUID, `communityId` the community's UUID, `meta.nextCursor` the last row's UUID, and both path ids are UUIDs.
  The BIGINT keys never cross the boundary. The server resolves each UUID to its internal key behind the tenant fence
  (tenant from the validated JWT only); a malformed, numeric, unknown or other-tenant id is a uniform 404 (an
  unknown `cursor` is 422 `invalid_cursor`).
- Client validation mirrors the server: platform in `discord|twitch|kick|youtube|slack`; `sourceId` matches `^[A-Za-z0-9._:-]{1,255}$`; `label` 1-255 chars.
- The registry stores no credentials; the server rejects unknown body fields. The UI therefore has no secret inputs and renders only DTO fields. Credentials (e.g. Twitch OAuth) go through their own write-only flows.
- Removal always asks for confirmation. 403 is shown as a permission message.
- Open item: the community picker still reads `tenantApi.getCommunities` (`GET /api/v1/tenant/{slug}/communities`), whose
  `CommunityDTO` (`hub_api/blueprints/v1/tenant.py`) exposes only the numeric `id` -- no `publicId`. Until it does, the
  page cannot supply the community UUID the ingest-sources routes now require.
