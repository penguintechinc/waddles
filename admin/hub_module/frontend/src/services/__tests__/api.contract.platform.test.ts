/**
 * Request-contract tests for the public, community, support, platform, super-admin and marketplace helper groups in `services/api.js`.
 */
import { afterAll, describe, it } from 'vitest';

import api, { publicApi, communityApi, supportApi, platformApi, superAdminApi, marketplaceApi, unifiedMarketplaceApi, vendorApi, marketplaceAdminApi } from '../api';
import { assertContractRow, recordRequests, type ContractRow } from '../../test/apiContract';

const rec = recordRequests(api);
afterAll(rec.restore);

const PUBLICAPI_ROWS: readonly ContractRow[] = [
  ['getStats()', 'GET /api/v1/public/stats'],
  ['getCommunities(params)', 'GET /api/v1/public/communities'],
  ['getSpotlightedCommunities()', 'GET /api/v1/public/communities/spotlighted'],
  ['getCommunity(id)', 'GET /api/v1/public/communities/{id}'],
  ['getCommunityProfile(id)', 'GET /api/v1/public/communities/{id}/profile'],
  ['getLiveStreams(params)', 'GET /api/v1/public/live'],
  ['getStreamDetails(entityId)', 'GET /api/v1/public/streams/{entityId}'],
  ['getSignupSettings()', 'GET /api/v1/signup-settings'],
  ['getBanner()', 'GET /api/v1/public/banner'],
  ['getMusicQueue(communityId)', 'GET /api/v1/public/communities/{communityId}/music-station/queue'],
];

const COMMUNITYAPI_ROWS: readonly ContractRow[] = [
  ['getMyCommunities()', 'GET /api/v1/communities/my'],
  ['create(data)', 'POST /api/v1/communities/create', { body: '@data' }],
  ['getDashboard(id)', 'GET /api/v1/communities/{id}/dashboard'],
  ['getLeaderboard(id, params)', 'GET /api/v1/communities/{id}/leaderboard'],
  ['getActivity(id, params)', 'GET /api/v1/communities/{id}/activity'],
  ['getEvents(id, params)', 'GET /api/v1/communities/{id}/events'],
  ['getMemories(id, params)', 'GET /api/v1/communities/{id}/memories'],
  ['getModules(id)', 'GET /api/v1/communities/{id}/modules'],
  ['getMembers(id, params)', 'GET /api/v1/communities/{id}/members'],
  ['updateProfile(id, data)', 'PUT /api/v1/communities/{id}/profile', { body: '@data' }],
  ['leave(id)', 'POST /api/v1/communities/{id}/leave'],
  ['getChatHistory(id, params)', 'GET /api/v1/community/{id}/chat/history'],
  ['getChatChannels(id)', 'GET /api/v1/community/{id}/chat/channels'],
  ['join(id, message)', 'POST /api/v1/communities/{id}/join', { body: { message: '@message' } }],
  ['getMyJoinRequests()', 'GET /api/v1/communities/join-requests'],
  ['cancelJoinRequest(requestId)', 'DELETE /api/v1/communities/join-requests/{requestId}'],
  ['addServer(id, data)', 'POST /api/v1/communities/{id}/servers', { body: '@data' }],
  ['getServers(id)', 'GET /api/v1/communities/{id}/servers'],
  ['getMyServerLinkRequests()', 'GET /api/v1/communities/server-link-requests'],
  ['cancelServerLinkRequest(requestId)', 'DELETE /api/v1/communities/server-link-requests/{requestId}'],
  ['getWatchTimeLeaderboard(id, params)', 'GET /api/v1/communities/{id}/leaderboard/watch-time'],
  ['getMessageLeaderboard(id, params)', 'GET /api/v1/communities/{id}/leaderboard/messages'],
  ['getMyActivityStats(id)', 'GET /api/v1/communities/{id}/activity/my-stats'],
  ['getMyReputation(id)', 'GET /api/v1/community/{id}/reputation/me'],
  ['getReputationLeaderboard(id, params)', 'GET /api/v1/community/{id}/reputation/leaderboard'],
];

const SUPPORTAPI_ROWS: readonly ContractRow[] = [
  ['submitTicket(communityId, data)', 'POST /api/v1/admin/{communityId}/support/submit', { body: '@data' }],
  ['getMyTickets(communityId)', 'GET /api/v1/admin/{communityId}/support/my-tickets'],
  ['getMyTicket(communityId, ticketId)', 'GET /api/v1/admin/{communityId}/support/my-tickets/{ticketId}'],
  ['addComment(communityId, ticketId, content)', 'POST /api/v1/admin/{communityId}/support/my-tickets/{ticketId}/comments', { body: { content: '@content' } }],
  ['getCategories(communityId)', 'GET /api/v1/admin/{communityId}/support/categories'],
];

const PLATFORMAPI_ROWS: readonly ContractRow[] = [
  ['getUsers(params)', 'GET /api/v1/platform/users'],
  ['getUser(id)', 'GET /api/v1/platform/users/{id}'],
  ['updateUserRole(id, role)', 'PUT /api/v1/platform/users/{id}/role', { body: { role: '@role' } }],
  ['deactivateUser(id, reason)', 'DELETE /api/v1/platform/users/{id}', { body: { reason: '@reason' } }],
  ['getCommunities(params)', 'GET /api/v1/platform/communities'],
  ['getCommunity(id)', 'GET /api/v1/platform/communities/{id}'],
  ['updateCommunity(id, data)', 'PUT /api/v1/platform/communities/{id}', { body: '@data' }],
  ['deactivateCommunity(id, reason)', 'DELETE /api/v1/platform/communities/{id}', { body: { reason: '@reason' } }],
  ['getHealth()', 'GET /api/v1/platform/health'],
  ['getModules()', 'GET /api/v1/platform/modules'],
  ['getAuditLog(params)', 'GET /api/v1/platform/audit-log'],
  ['getStats()', 'GET /api/v1/platform/stats'],
];

const SUPERADMINAPI_ROWS: readonly ContractRow[] = [
  ['getDashboard()', 'GET /api/v1/superadmin/dashboard'],
  ['getAnalytics()', 'GET /api/v1/superadmin/analytics'],
  ['getReputationDistribution()', 'GET /api/v1/superadmin/analytics/reputation'],
  ['getGrowthTrends(params)', 'GET /api/v1/superadmin/analytics/growth'],
  ['getActivityBreakdown()', 'GET /api/v1/superadmin/analytics/activity'],
  ['getCommunities(params)', 'GET /api/v1/superadmin/communities'],
  ['getCommunity(id)', 'GET /api/v1/superadmin/communities/{id}'],
  ['createCommunity(data)', 'POST /api/v1/superadmin/communities', { body: '@data' }],
  ['updateCommunity(id, data)', 'PUT /api/v1/superadmin/communities/{id}', { body: '@data' }],
  ['deleteCommunity(id)', 'DELETE /api/v1/superadmin/communities/{id}'],
  ['reassignOwner(id, data)', 'POST /api/v1/superadmin/communities/{id}/reassign', { body: '@data' }],
  ['getAllModules(params)', 'GET /api/v1/superadmin/marketplace/modules'],
  ['createModule(data)', 'POST /api/v1/superadmin/marketplace/modules', { body: '@data' }],
  ['updateModule(id, data)', 'PUT /api/v1/superadmin/marketplace/modules/{id}', { body: '@data' }],
  ['publishModule(id, isPublished)', 'PUT /api/v1/superadmin/marketplace/modules/{id}/publish', { body: { isPublished: '@isPublished' } }],
  ['deleteModule(id)', 'DELETE /api/v1/superadmin/marketplace/modules/{id}'],
  ['getPlatformConfigs()', 'GET /api/v1/superadmin/platform-config'],
  ['updatePlatformConfig(platform, data)', 'PUT /api/v1/superadmin/platform-config/{platform}', { body: '@data' }],
  ['testPlatformConnection(platform)', 'POST /api/v1/superadmin/platform-config/{platform}/test'],
  ['getHubSettings()', 'GET /api/v1/superadmin/settings'],
  ['updateHubSettings(data)', 'PUT /api/v1/superadmin/settings', { body: '@data' }],
  ['testStorageConnection()', 'POST /api/v1/superadmin/platform-config/storage/test'],
  ['getSoftwareRepositories()', 'GET /api/v1/superadmin/software/repositories'],
  ['getSoftwareRepository(id)', 'GET /api/v1/superadmin/software/repositories/{id}'],
  ['addSoftwareRepository(data)', 'POST /api/v1/superadmin/software/repositories', { body: '@data' }],
  ['updateSoftwareRepository(id, data)', 'PUT /api/v1/superadmin/software/repositories/{id}', { body: '@data' }],
  ['deleteSoftwareRepository(id)', 'DELETE /api/v1/superadmin/software/repositories/{id}'],
  ['scanSoftwareRepository(id)', 'POST /api/v1/superadmin/software/repositories/{id}/scan'],
  ['testRepositoryConnection(data)', 'POST /api/v1/superadmin/software/repositories/test', { body: '@data' }],
  ['getRepositoryDependencies(id)', 'GET /api/v1/superadmin/software/repositories/{id}/dependencies'],
  ['getServices()', 'GET /api/v1/superadmin/services'],
  ['getService(id)', 'GET /api/v1/superadmin/services/{id}'],
  ['addService(data)', 'POST /api/v1/superadmin/services', { body: '@data' }],
  ['updateService(id, data)', 'PUT /api/v1/superadmin/services/{id}', { body: '@data' }],
  ['deleteService(id)', 'DELETE /api/v1/superadmin/services/{id}'],
  ['refreshService(id)', 'POST /api/v1/superadmin/services/{id}/refresh'],
  ['refreshAllServices()', 'POST /api/v1/superadmin/services/refresh-all'],
  ['listUsers(params)', 'GET /api/v1/superadmin/users'],
  ['getUser(userId)', 'GET /api/v1/superadmin/users/{userId}'],
  ['createUser(data)', 'POST /api/v1/superadmin/users', { body: '@data' }],
  ['updateUser(userId, data)', 'PUT /api/v1/superadmin/users/{userId}', { body: '@data' }],
  ['deleteUser(userId)', 'DELETE /api/v1/superadmin/users/{userId}'],
  ['assignSuperAdminRole(userId, grant)', 'POST /api/v1/superadmin/users/{userId}/super-admin-role', { body: { grant: '@grant' } }],
  ['assignVendorRole(userId, grant)', 'POST /api/v1/superadmin/users/{userId}/vendor-role', { body: { grant: '@grant' } }],
  ['setEmailVerification(userId, verified)', 'POST /api/v1/superadmin/users/{userId}/verify-email', { body: { verified: '@verified' } }],
  ['generatePasswordReset(userId)', 'POST /api/v1/superadmin/users/{userId}/password-reset'],
];

const MARKETPLACEAPI_ROWS: readonly ContractRow[] = [
  ['browseModules(communityId, params)', 'GET /api/v1/admin/{communityId}/marketplace/modules'],
  ['getModuleDetails(communityId, moduleId)', 'GET /api/v1/admin/{communityId}/marketplace/modules/{moduleId}'],
  ['installModule(communityId, moduleId)', 'POST /api/v1/admin/{communityId}/marketplace/modules/{moduleId}/install'],
  ['uninstallModule(communityId, moduleId)', 'DELETE /api/v1/admin/{communityId}/marketplace/modules/{moduleId}'],
  ['configureModule(communityId, moduleId, data)', 'PUT /api/v1/admin/{communityId}/marketplace/modules/{moduleId}/config', { body: '@data' }],
  ['addReview(communityId, moduleId, data)', 'POST /api/v1/admin/{communityId}/marketplace/modules/{moduleId}/review', { body: '@data' }],
];

const UNIFIEDMARKETPLACEAPI_ROWS: readonly ContractRow[] = [
  ['getCatalog(params)', 'GET /api/v1/marketplace/catalog'],
  ['getCatalogEntry(source, id, params)', 'GET /api/v1/marketplace/catalog/{source}/{id}'],
  ['getCategories()', 'GET /api/v1/marketplace/catalog/categories'],
  ['getFeatured(params)', 'GET /api/v1/marketplace/catalog/featured'],
  ['getInstalled(communityId)', 'GET /api/v1/marketplace/communities/{communityId}/installed'],
  ['installModule(communityId, data)', 'POST /api/v1/marketplace/communities/{communityId}/install', { body: '@data' }],
  ['uninstallModule(communityId, moduleId, source)', 'DELETE /api/v1/marketplace/communities/{communityId}/install/{moduleId}', { params: { source: '@source' } }],
  ['toggleModule(communityId, moduleId, data)', 'PUT /api/v1/marketplace/communities/{communityId}/install/{moduleId}', { body: '@data' }],
  ['getPricing(params)', 'GET /api/v1/marketplace/premium/pricing'],
  ['getPremiumStatus(communityId)', 'GET /api/v1/marketplace/premium/status/{communityId}'],
  ['subscribePremium(data)', 'POST /api/v1/marketplace/premium/subscribe', { body: '@data' }],
  ['cancelPremium(data)', 'POST /api/v1/marketplace/premium/cancel', { body: '@data' }],
];

const VENDORAPI_ROWS: readonly ContractRow[] = [
  ['getProfile()', 'GET /api/v1/marketplace/vendor/profile'],
  ['createProfile(data)', 'POST /api/v1/marketplace/vendor/profile', { body: '@data' }],
  ['getModules(params)', 'GET /api/v1/marketplace/vendor/modules'],
  ['createModule(data)', 'POST /api/v1/marketplace/vendor/modules', { body: '@data' }],
  ['updateModule(id, data)', 'PUT /api/v1/marketplace/vendor/modules/{id}', { body: '@data' }],
  ['submitForReview(id, data)', 'POST /api/v1/marketplace/vendor/modules/{id}/submit', { body: '@data' }],
  ['getRequest()', 'GET /api/v1/marketplace/vendor/request'],
  ['createRequest(data)', 'POST /api/v1/marketplace/vendor/request', { body: '@data' }],
];

const MARKETPLACEADMINAPI_ROWS: readonly ContractRow[] = [
  ['getVendorRequests(params)', 'GET /api/v1/marketplace/admin/marketplace/vendor-requests'],
  ['approveVendorRequest(id, data)', 'POST /api/v1/marketplace/admin/marketplace/vendor-requests/{id}/approve', { body: '@data' }],
  ['rejectVendorRequest(id, data)', 'POST /api/v1/marketplace/admin/marketplace/vendor-requests/{id}/reject', { body: '@data' }],
  ['getSubmissions(params)', 'GET /api/v1/marketplace/admin/marketplace/submissions'],
  ['approveSubmission(id, data)', 'POST /api/v1/marketplace/admin/marketplace/submissions/{id}/approve', { body: '@data' }],
  ['rejectSubmission(id, data)', 'POST /api/v1/marketplace/admin/marketplace/submissions/{id}/reject', { body: '@data' }],
  ['getSettings()', 'GET /api/v1/marketplace/admin/marketplace/settings'],
  ['updateSettings(data)', 'PUT /api/v1/marketplace/admin/marketplace/settings', { body: '@data' }],
];

describe('publicApi request contract', () => {
  for (const row of PUBLICAPI_ROWS) {
    it(row[0], () => assertContractRow(publicApi, row, rec.calls));
  }
});

describe('communityApi request contract', () => {
  for (const row of COMMUNITYAPI_ROWS) {
    it(row[0], () => assertContractRow(communityApi, row, rec.calls));
  }
});

describe('supportApi request contract', () => {
  for (const row of SUPPORTAPI_ROWS) {
    it(row[0], () => assertContractRow(supportApi, row, rec.calls));
  }
});

describe('platformApi request contract', () => {
  for (const row of PLATFORMAPI_ROWS) {
    it(row[0], () => assertContractRow(platformApi, row, rec.calls));
  }
});

describe('superAdminApi request contract', () => {
  for (const row of SUPERADMINAPI_ROWS) {
    it(row[0], () => assertContractRow(superAdminApi, row, rec.calls));
  }
});

describe('marketplaceApi request contract', () => {
  for (const row of MARKETPLACEAPI_ROWS) {
    it(row[0], () => assertContractRow(marketplaceApi, row, rec.calls));
  }
});

describe('unifiedMarketplaceApi request contract', () => {
  for (const row of UNIFIEDMARKETPLACEAPI_ROWS) {
    it(row[0], () => assertContractRow(unifiedMarketplaceApi, row, rec.calls));
  }
});

describe('vendorApi request contract', () => {
  for (const row of VENDORAPI_ROWS) {
    it(row[0], () => assertContractRow(vendorApi, row, rec.calls));
  }
});

describe('marketplaceAdminApi request contract', () => {
  for (const row of MARKETPLACEADMINAPI_ROWS) {
    it(row[0], () => assertContractRow(marketplaceAdminApi, row, rec.calls));
  }
});
