/**
 * Request-contract tests for the user, analytics, stream, workflow, calendar, inventory, token, auth, interaction, RCON, tenant, role and bundle helper groups in `services/api.js`.
 */
import { afterAll, describe, it } from 'vitest';

import api, { userApi, analyticsApi, streamApi, workflowApi, calendarApi, inventoryApi, tokenApi, joinRequestApi, userOAuthApi, passkeyApi, interactionApi, rconApi, tenantApi, rolesApi, channelPermissionsApi, superadminTenantApi, bundleApi, bundleAdminApi } from '../api';
import { assertContractRow, recordRequests, type ContractRow } from '../../test/apiContract';

const rec = recordRequests(api);
afterAll(rec.restore);

const USERAPI_ROWS: readonly ContractRow[] = [
  ['getIdentities()', 'GET /api/v1/user/identities'],
  ['linkIdentity(platform)', 'POST /api/v1/user/identities/link/{platform}'],
  ['unlinkIdentity(platform)', 'DELETE /api/v1/user/identities/{platform}'],
  ['getPrimaryIdentity()', 'GET /api/v1/user/identities/primary'],
  ['setPrimaryIdentity(platform)', 'PUT /api/v1/user/identities/primary', { body: { platform: '@platform' } }],
  ['getMyProfile()', 'GET /api/v1/user/profile'],
  ['updateProfile(data)', 'PUT /api/v1/user/profile', { body: '@data' }],
  ['deleteAvatar()', 'DELETE /api/v1/user/profile/avatar'],
  ['getLinkedPlatforms()', 'GET /api/v1/user/linked-platforms'],
  ['getPublicProfile(userId)', 'GET /api/v1/public/users/{userId}/profile'],
  ['getMemberProfile(communityId, userId)', 'GET /api/v1/communities/{communityId}/members/{userId}/profile'],
];

const ANALYTICSAPI_ROWS: readonly ContractRow[] = [
  ['getMyStats()', 'GET /api/v1/analytics/me/stats'],
  ['getMyReputation()', 'GET /api/v1/analytics/me/reputation'],
  ['getMemberStats(communityId, userId)', 'GET /api/v1/analytics/community/{communityId}/members/{userId}/stats'],
  ['getMemberReputation(communityId, userId)', 'GET /api/v1/analytics/community/{communityId}/members/{userId}/reputation'],
  ['getPlatformOverview()', 'GET /api/v1/analytics/platform/overview'],
  ['getPlatformReputation()', 'GET /api/v1/analytics/platform/reputation'],
  ['getPlatformGrowth()', 'GET /api/v1/analytics/platform/growth?period=30d'],
  ['getPlatformActivity()', 'GET /api/v1/analytics/platform/activity'],
  ['getCommunityHealth()', 'GET /api/v1/analytics/platform/community-health?limit=50'],
  ['getAdminUserStats(userId)', 'GET /api/v1/analytics/admin/users/{userId}/stats'],
];

const STREAMAPI_ROWS: readonly ContractRow[] = [
  ['getLiveStreams(communityId)', 'GET /api/v1/communities/{communityId}/streams'],
  ['getFeaturedStreams(communityId)', 'GET /api/v1/communities/{communityId}/streams/featured'],
  ['getStreamDetails(communityId, entityId)', 'GET /api/v1/communities/{communityId}/streams/{entityId}'],
];

const WORKFLOWAPI_ROWS: readonly ContractRow[] = [
  ['listWorkflows(communityId, params)', 'GET /api/v1/admin/{communityId}/workflows'],
  ['getWorkflow(communityId, workflowId)', 'GET /api/v1/admin/{communityId}/workflows/{workflowId}'],
  ['createWorkflow(communityId, data)', 'POST /api/v1/admin/{communityId}/workflows', { body: '@data' }],
  ['updateWorkflow(communityId, workflowId, data)', 'PUT /api/v1/admin/{communityId}/workflows/{workflowId}', { body: '@data' }],
  ['deleteWorkflow(communityId, workflowId)', 'DELETE /api/v1/admin/{communityId}/workflows/{workflowId}'],
  ['publishWorkflow(communityId, workflowId)', 'POST /api/v1/admin/{communityId}/workflows/{workflowId}/publish'],
  ['unpublishWorkflow(communityId, workflowId)', 'POST /api/v1/admin/{communityId}/workflows/{workflowId}/unpublish'],
  ['validateWorkflow(communityId, workflowId)', 'POST /api/v1/admin/{communityId}/workflows/{workflowId}/validate'],
  ['executeWorkflow(communityId, workflowId, data)', 'POST /api/v1/admin/{communityId}/workflows/{workflowId}/execute', { body: '@data' }],
  ['testWorkflow(communityId, workflowId, data)', 'POST /api/v1/admin/{communityId}/workflows/{workflowId}/test', { body: '@data' }],
  ['getExecutions(communityId, workflowId, params)', 'GET /api/v1/admin/{communityId}/workflows/{workflowId}/executions'],
  ['getExecution(communityId, workflowId, executionId)', 'GET /api/v1/admin/{communityId}/workflows/{workflowId}/executions/{executionId}'],
  ['cancelExecution(communityId, workflowId, executionId)', 'POST /api/v1/admin/{communityId}/workflows/{workflowId}/executions/{executionId}/cancel'],
  ['listWebhooks(communityId, workflowId)', 'GET /api/v1/admin/{communityId}/workflows/{workflowId}/webhooks'],
  ['createWebhook(communityId, workflowId, data)', 'POST /api/v1/admin/{communityId}/workflows/{workflowId}/webhooks', { body: '@data' }],
  ['deleteWebhook(communityId, workflowId, webhookId)', 'DELETE /api/v1/admin/{communityId}/workflows/{workflowId}/webhooks/{webhookId}'],
  ['regenerateWebhookSecret(communityId, workflowId, webhookId)', 'POST /api/v1/admin/{communityId}/workflows/{workflowId}/webhooks/{webhookId}/regenerate'],
];

const CALENDARAPI_ROWS: readonly ContractRow[] = [
  ['getGoogleAuthUrl()', 'GET /api/v1/calendar/oauth/google/auth-url'],
  ['getMicrosoftAuthUrl()', 'GET /api/v1/calendar/oauth/microsoft/auth-url'],
  ['getConnectedCalendars()', 'GET /api/v1/calendar/oauth/calendars'],
  ['syncCalendar(id)', 'POST /api/v1/calendar/oauth/calendars/{id}/sync'],
  ['disconnectCalendar(id)', 'DELETE /api/v1/calendar/oauth/calendars/{id}'],
  ['getAvailabilitySettings()', 'GET /api/v1/calendar/availability/settings'],
  ['updateAvailabilitySettings(data)', 'PUT /api/v1/calendar/availability/settings', { body: '@data' }],
  ['getWeeklyAvailability()', 'GET /api/v1/calendar/availability/weekly'],
  ['updateWeeklyAvailability(data)', 'PUT /api/v1/calendar/availability/weekly', { body: '@data' }],
  ['getAvailableSlots(userId, date, duration)', 'GET /api/v1/calendar/availability/{userId}/slots', { params: { date: '@date', duration: '@duration' } }],
  ['createBookingPage(data)', 'POST /api/v1/calendar/booking-pages', { body: '@data' }],
  ['getBookingPages()', 'GET /api/v1/calendar/booking-pages'],
  ['getBookingPage(idOrSlug)', 'GET /api/v1/calendar/booking-pages/{idOrSlug}'],
  ['updateBookingPage(id, data)', 'PUT /api/v1/calendar/booking-pages/{id}', { body: '@data' }],
  ['deleteBookingPage(id)', 'DELETE /api/v1/calendar/booking-pages/{id}'],
  ['getBookingSlots(slug, date)', 'GET /api/v1/calendar/book/{slug}/slots', { params: { date: '@date' } }],
  ['createBooking(slug, data)', 'POST /api/v1/calendar/book/{slug}', { body: '@data' }],
  ['getBooking(uuid)', 'GET /api/v1/calendar/bookings/{uuid}'],
  ['cancelBooking(uuid)', 'DELETE /api/v1/calendar/bookings/{uuid}'],
  ['getMyBookings(params)', 'GET /api/v1/calendar/my-bookings'],
  ['addGroupMember(pageId, data)', 'POST /api/v1/calendar/booking-pages/{pageId}/members', { body: '@data' }],
  ['removeGroupMember(pageId, userId)', 'DELETE /api/v1/calendar/booking-pages/{pageId}/members/{userId}'],
  ['getGroupMembers(pageId)', 'GET /api/v1/calendar/booking-pages/{pageId}/members'],
  ['getGroupAvailability(pageId, date)', 'GET /api/v1/calendar/booking-pages/{pageId}/group-availability', { params: { date: '@date' } }],
  ['getBestSlots(pageId, start, end, limit)', 'GET /api/v1/calendar/booking-pages/{pageId}/best-slots', { params: { start: '@start', end: '@end', limit: '@limit' } }],
];

const INVENTORYAPI_ROWS: readonly ContractRow[] = [
  ['listItems(communityId)', 'GET /api/v1/admin/{communityId}/inventory/items'],
  ['createItem(communityId, data)', 'POST /api/v1/admin/{communityId}/inventory/items', { body: '@data' }],
  ['updateItem(communityId, itemId, data)', 'PUT /api/v1/admin/{communityId}/inventory/items/{itemId}', { body: '@data' }],
  ['deleteItem(communityId, itemId)', 'DELETE /api/v1/admin/{communityId}/inventory/items/{itemId}'],
  ['addStock(communityId, itemId, data)', 'POST /api/v1/admin/{communityId}/inventory/items/{itemId}/stock/add', { body: '@data' }],
  ['removeStock(communityId, itemId, data)', 'POST /api/v1/admin/{communityId}/inventory/items/{itemId}/stock/remove', { body: '@data' }],
  ['listAllCheckouts(communityId, params)', 'GET /api/v1/admin/{communityId}/inventory/checkouts'],
  ['getSummary(communityId)', 'GET /api/v1/admin/{communityId}/inventory/summary'],
  ['getAuditLog(communityId, params)', 'GET /api/v1/admin/{communityId}/inventory/log'],
  ['listAvailable(communityId, params)', 'GET /api/v1/admin/{communityId}/inventory/available'],
  ['checkoutItem(communityId, data)', 'POST /api/v1/admin/{communityId}/inventory/checkout', { body: '@data' }],
  ['checkinItem(communityId, data)', 'POST /api/v1/admin/{communityId}/inventory/checkin', { body: '@data' }],
  ['getMyCheckouts(communityId)', 'GET /api/v1/admin/{communityId}/inventory/my-items'],
];

const TOKENAPI_ROWS: readonly ContractRow[] = [
  ['getPATScopes()', 'GET /api/v1/user/tokens/scopes'],
  ['getPAT()', 'GET /api/v1/user/tokens/pat'],
  ['createPAT(data)', 'POST /api/v1/user/tokens/pat', { body: '@data' }],
  ['revokePAT()', 'DELETE /api/v1/user/tokens/pat'],
  ['getCATScopes(communityId)', 'GET /api/v1/admin/{communityId}/tokens/scopes'],
  ['listCATs(communityId)', 'GET /api/v1/admin/{communityId}/tokens/cats'],
  ['createCAT(communityId, data)', 'POST /api/v1/admin/{communityId}/tokens/cats', { body: '@data' }],
  ['revokeCAT(communityId, tokenId)', 'DELETE /api/v1/admin/{communityId}/tokens/cats/{tokenId}'],
];

const JOINREQUESTAPI_ROWS: readonly ContractRow[] = [
  ['submit(communityId, data)', 'POST /community/{communityId}/join-requests', { body: '@data' }],
  ['getMine(communityId)', 'GET /community/{communityId}/join-requests/mine'],
  ['list(communityId)', 'GET /admin/{communityId}/join-requests'],
  ['approve(communityId, requestId)', 'PUT /admin/{communityId}/join-requests/{requestId}/approve'],
  ['reject(communityId, requestId)', 'PUT /admin/{communityId}/join-requests/{requestId}/reject'],
];

const USEROAUTHAPI_ROWS: readonly ContractRow[] = [
  ['getCredentials()', 'GET /api/v1/user/oauth/credentials'],
  ['createCredential(data)', 'POST /api/v1/user/oauth/credentials', { body: '@data' }],
  ['updateCredential(id, data)', 'PUT /api/v1/user/oauth/credentials/{id}', { body: '@data' }],
  ['deleteCredential(id)', 'DELETE /api/v1/user/oauth/credentials/{id}'],
  ['testCredential(id)', 'POST /api/v1/user/oauth/credentials/{id}/test'],
];

const PASSKEYAPI_ROWS: readonly ContractRow[] = [
  ['startRegistration()', 'POST /api/v1/user/passkey/register/start'],
  ['finishRegistration(data)', 'POST /api/v1/user/passkey/register/finish', { body: '@data' }],
  ['listCredentials()', 'GET /api/v1/user/passkey/credentials'],
  ['removeCredential(id)', 'DELETE /api/v1/user/passkey/credentials/{id}'],
  ['startLogin(data)', 'POST /api/v1/auth/passkey/login/start', { body: '@data' }],
  ['finishLogin(data)', 'POST /api/v1/auth/passkey/login/finish', { body: '@data' }],
];

const INTERACTIONAPI_ROWS: readonly ContractRow[] = [
  ['getChannels(communityId)', 'GET /api/v1/admin/{communityId}/interaction/channels'],
  ['createChannel(communityId, data)', 'POST /api/v1/admin/{communityId}/interaction/channels', { body: '@data' }],
  ['updateChannel(communityId, channelId, data)', 'PUT /api/v1/admin/{communityId}/interaction/channels/{channelId}', { body: '@data' }],
  ['deleteChannel(communityId, channelId)', 'DELETE /api/v1/admin/{communityId}/interaction/channels/{channelId}'],
  ['getMemberChannels(communityId)', 'GET /api/v1/community/{communityId}/interact/channels'],
  ['createMemberChannel(communityId, data)', 'POST /api/v1/community/{communityId}/interact/channels', { body: '@data' }],
  ['getForumPosts(communityId, channelId, params)', 'GET /api/v1/community/{communityId}/interact/forum/{channelId}/posts'],
  ['getForumPost(communityId, channelId, postId)', 'GET /api/v1/community/{communityId}/interact/forum/{channelId}/posts/{postId}'],
  ['createForumPost(communityId, channelId, data)', 'POST /api/v1/community/{communityId}/interact/forum/{channelId}/posts', { body: '@data' }],
  ['createForumReply(communityId, postId, data)', 'POST /api/v1/community/{communityId}/interact/forum/posts/{postId}/replies', { body: '@data' }],
  ['moderatePost(communityId, postId, data)', 'PUT /api/v1/admin/{communityId}/interaction/forum/posts/{postId}', { body: '@data' }],
  ['deleteReply(communityId, replyId)', 'DELETE /api/v1/admin/{communityId}/interaction/forum/replies/{replyId}'],
  ['getVoiceRooms(communityId)', 'GET /api/v1/community/{communityId}/interact/voice/rooms'],
  ['joinVoiceRoom(communityId, roomName)', 'POST /api/v1/community/{communityId}/interact/voice/rooms/{roomName|enc}/join'],
  ['leaveVoiceRoom(communityId, roomName)', 'POST /api/v1/community/{communityId}/interact/voice/rooms/{roomName|enc}/leave'],
  ['createAdHocVoiceRoom(communityId, data)', 'POST /api/v1/community/{communityId}/interact/voice/rooms', { body: '@data' }],
];

const RCONAPI_ROWS: readonly ContractRow[] = [
  ['listServers(communityId)', 'GET /api/v1/admin/{communityId}/rcon/servers'],
  ['createServer(communityId, data)', 'POST /api/v1/admin/{communityId}/rcon/servers', { body: '@data' }],
  ['updateServer(communityId, serverId, data)', 'PUT /api/v1/admin/{communityId}/rcon/servers/{serverId}', { body: '@data' }],
  ['deleteServer(communityId, serverId)', 'DELETE /api/v1/admin/{communityId}/rcon/servers/{serverId}'],
  ['testConnection(communityId, serverId, data)', 'POST /api/v1/admin/{communityId}/rcon/servers/{serverId}/test', { body: '@data' }],
  ['executeCommand(communityId, serverId, data)', 'POST /api/v1/admin/{communityId}/rcon/servers/{serverId}/command', { body: '@data' }],
  ['kickPlayer(communityId, serverId, data)', 'POST /api/v1/admin/{communityId}/rcon/servers/{serverId}/kick', { body: '@data' }],
  ['banPlayer(communityId, serverId, data)', 'POST /api/v1/admin/{communityId}/rcon/servers/{serverId}/ban', { body: '@data' }],
  ['getChannels(communityId, serverId)', 'GET /api/v1/admin/{communityId}/rcon/servers/{serverId}/channels'],
  ['moveUser(communityId, serverId, data)', 'POST /api/v1/admin/{communityId}/rcon/servers/{serverId}/move', { body: '@data' }],
  ['sendMessage(communityId, serverId, data)', 'POST /api/v1/admin/{communityId}/rcon/servers/{serverId}/message', { body: '@data' }],
  ['getCommandLog(communityId, params)', 'GET /api/v1/admin/{communityId}/rcon/log'],
  ['getAccessPolicy(communityId, serverId)', 'GET /api/v1/admin/{communityId}/rcon/servers/{serverId}/policy'],
  ['updateAccessPolicy(communityId, serverId, data)', 'PUT /api/v1/admin/{communityId}/rcon/servers/{serverId}/policy', { body: '@data' }],
  ['triggerEnforcement(communityId, serverId)', 'POST /api/v1/admin/{communityId}/rcon/servers/{serverId}/enforce'],
  ['getAccessLog(communityId, serverId, params)', 'GET /api/v1/admin/{communityId}/rcon/servers/{serverId}/access-log'],
  ['listInfo(communityId)', 'GET /api/v1/admin/{communityId}/rcon/info'],
  ['getServerStatus(communityId, serverId)', 'GET /api/v1/admin/{communityId}/rcon/info/{serverId}/status'],
  ['getPlayerList(communityId, serverId)', 'GET /api/v1/admin/{communityId}/rcon/info/{serverId}/players'],
];

const TENANTAPI_ROWS: readonly ContractRow[] = [
  ['getLoginInfo(slug)', 'GET /api/v1/auth/tenant/{slug}'],
  ['getTenant(slug)', 'GET /api/v1/tenant/{slug}'],
  ['updateTenant(slug, data)', 'PUT /api/v1/tenant/{slug}', { body: '@data' }],
  ['getSettings(slug)', 'GET /api/v1/tenant/{slug}/settings'],
  ['updateSettings(slug, settings)', 'PUT /api/v1/tenant/{slug}/settings', { body: { settings: '@settings' } }],
  ['getCommunities(slug, params)', 'GET /api/v1/tenant/{slug}/communities'],
  ['getModules(slug)', 'GET /api/v1/tenant/{slug}/modules'],
  ['updateModules(slug, allowedModuleIds)', 'PUT /api/v1/tenant/{slug}/modules', { body: { allowedModuleIds: '@allowedModuleIds' } }],
  ['getAdmins(slug)', 'GET /api/v1/tenant/{slug}/admins'],
  ['addAdmin(slug, userId, role)', 'POST /api/v1/tenant/{slug}/admins', { body: { userId: '@userId', role: '@role' } }],
  ['removeAdmin(slug, userId)', 'DELETE /api/v1/tenant/{slug}/admins/{userId}'],
];

const ROLESAPI_ROWS: readonly ContractRow[] = [
  ['list(communityId)', 'GET /api/v1/admin/{communityId}/interaction/roles'],
  ['create(communityId, data)', 'POST /api/v1/admin/{communityId}/interaction/roles', { body: '@data' }],
  ['update(communityId, roleId, data)', 'PUT /api/v1/admin/{communityId}/interaction/roles/{roleId}', { body: '@data' }],
  ['delete(communityId, roleId)', 'DELETE /api/v1/admin/{communityId}/interaction/roles/{roleId}'],
];

const CHANNELPERMISSIONSAPI_ROWS: readonly ContractRow[] = [
  ['getOverrides(communityId, channelId)', 'GET /api/v1/admin/{communityId}/interaction/channels/{channelId}/permissions'],
  ['updateOverrides(communityId, channelId, overrides)', 'PUT /api/v1/admin/{communityId}/interaction/channels/{channelId}/permissions', { body: { overrides: '@overrides' } }],
];

const SUPERADMINTENANTAPI_ROWS: readonly ContractRow[] = [
  ['list(params)', 'GET /api/v1/superadmin/tenants'],
  ['create(data)', 'POST /api/v1/superadmin/tenants', { body: '@data' }],
  ['update(id, data)', 'PUT /api/v1/superadmin/tenants/{id}', { body: '@data' }],
  ['delete(id)', 'DELETE /api/v1/superadmin/tenants/{id}'],
];

const BUNDLEAPI_ROWS: readonly ContractRow[] = [
  ['listVersions(appId)', 'GET /api/v1/apps/{appId|enc}/versions'],
  ['getVersion(appId, version)', 'GET /api/v1/apps/{appId|enc}/versions/{version|enc}'],
  ['getPermissions(appId, version)', 'GET /api/v1/apps/{appId|enc}/versions/{version|enc}/permissions'],
  ['approveVersion(appId, version, data)', 'POST /api/v1/apps/{appId|enc}/versions/{version|enc}/approve', { body: '@data' }],
  ['denyVersion(appId, version, data)', 'POST /api/v1/apps/{appId|enc}/versions/{version|enc}/deny', { body: '@data' }],
];

const BUNDLEADMINAPI_ROWS: readonly ContractRow[] = [
  ['listPendingVersions(params)', 'GET /api/v1/admin/bundle-versions'],
];

describe('userApi request contract', () => {
  for (const row of USERAPI_ROWS) {
    it(row[0], () => assertContractRow(userApi, row, rec.calls));
  }
});

describe('analyticsApi request contract', () => {
  for (const row of ANALYTICSAPI_ROWS) {
    it(row[0], () => assertContractRow(analyticsApi, row, rec.calls));
  }
});

describe('streamApi request contract', () => {
  for (const row of STREAMAPI_ROWS) {
    it(row[0], () => assertContractRow(streamApi, row, rec.calls));
  }
});

describe('workflowApi request contract', () => {
  for (const row of WORKFLOWAPI_ROWS) {
    it(row[0], () => assertContractRow(workflowApi, row, rec.calls));
  }
});

describe('calendarApi request contract', () => {
  for (const row of CALENDARAPI_ROWS) {
    it(row[0], () => assertContractRow(calendarApi, row, rec.calls));
  }
});

describe('inventoryApi request contract', () => {
  for (const row of INVENTORYAPI_ROWS) {
    it(row[0], () => assertContractRow(inventoryApi, row, rec.calls));
  }
});

describe('tokenApi request contract', () => {
  for (const row of TOKENAPI_ROWS) {
    it(row[0], () => assertContractRow(tokenApi, row, rec.calls));
  }
});

describe('joinRequestApi request contract', () => {
  for (const row of JOINREQUESTAPI_ROWS) {
    it(row[0], () => assertContractRow(joinRequestApi, row, rec.calls));
  }
});

describe('userOAuthApi request contract', () => {
  for (const row of USEROAUTHAPI_ROWS) {
    it(row[0], () => assertContractRow(userOAuthApi, row, rec.calls));
  }
});

describe('passkeyApi request contract', () => {
  for (const row of PASSKEYAPI_ROWS) {
    it(row[0], () => assertContractRow(passkeyApi, row, rec.calls));
  }
});

describe('interactionApi request contract', () => {
  for (const row of INTERACTIONAPI_ROWS) {
    it(row[0], () => assertContractRow(interactionApi, row, rec.calls));
  }
});

describe('rconApi request contract', () => {
  for (const row of RCONAPI_ROWS) {
    it(row[0], () => assertContractRow(rconApi, row, rec.calls));
  }
});

describe('tenantApi request contract', () => {
  for (const row of TENANTAPI_ROWS) {
    it(row[0], () => assertContractRow(tenantApi, row, rec.calls));
  }
});

describe('rolesApi request contract', () => {
  for (const row of ROLESAPI_ROWS) {
    it(row[0], () => assertContractRow(rolesApi, row, rec.calls));
  }
});

describe('channelPermissionsApi request contract', () => {
  for (const row of CHANNELPERMISSIONSAPI_ROWS) {
    it(row[0], () => assertContractRow(channelPermissionsApi, row, rec.calls));
  }
});

describe('superadminTenantApi request contract', () => {
  for (const row of SUPERADMINTENANTAPI_ROWS) {
    it(row[0], () => assertContractRow(superadminTenantApi, row, rec.calls));
  }
});

describe('bundleApi request contract', () => {
  for (const row of BUNDLEAPI_ROWS) {
    it(row[0], () => assertContractRow(bundleApi, row, rec.calls));
  }
});

describe('bundleAdminApi request contract', () => {
  for (const row of BUNDLEADMINAPI_ROWS) {
    it(row[0], () => assertContractRow(bundleAdminApi, row, rec.calls));
  }
});
