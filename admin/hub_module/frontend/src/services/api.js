import axios from 'axios';

// SECURITY (security.md C4 / OWASP A07): the session JWT lives ONLY in the
// HttpOnly `wb_session` cookie hub-api sets on login/OAuth-exchange/refresh
// (hub_api/services/session_cookie.py) — never in localStorage, never
// readable via `document.cookie`. A single XSS payload anywhere in this SPA
// used to be able to exfiltrate the whole session by reading localStorage;
// there is now nothing here for it to read. `withCredentials: true` makes
// axios send that cookie automatically; the browser attaches it to every
// same-origin request regardless, but this keeps the client explicit and
// correct if VITE_API_URL ever points elsewhere. hub-api's own CSRF
// mitigation is the cookie's `SameSite=Lax` attribute (no double-submit
// token needed — see services/session_cookie.py's docstring), so no
// X-XSRF-TOKEN handling belongs here either.
const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '',
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Response interceptor for error handling
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;

    // Handle 401 errors (session expired) — hub-api rotates the session
    // cookie on a successful /refresh; the browser stores the new cookie
    // automatically from the Set-Cookie response header, so the retried
    // request needs nothing attached by hand.
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      try {
        const response = await api.post('/api/v1/auth/refresh');
        if (response.data.success) {
          return api(originalRequest);
        }
      } catch (refreshError) {
        window.location.href = '/login';
        return Promise.reject(refreshError);
      }
    }

    return Promise.reject(error);
  }
);

export default api;

// API helper functions
export const publicApi = {
  getStats: () => api.get('/api/v1/public/stats'),
  getCommunities: (params) => api.get('/api/v1/public/communities', { params }),
  getSpotlightedCommunities: () => api.get('/api/v1/public/communities/spotlighted'),
  getCommunity: (id) => api.get(`/api/v1/public/communities/${id}`),
  getCommunityProfile: (id) => api.get(`/api/v1/public/communities/${id}/profile`),
  getLiveStreams: (params) => api.get('/api/v1/public/live', { params }),
  getStreamDetails: (entityId) => api.get(`/api/v1/public/streams/${entityId}`),
  getSignupSettings: () => api.get('/api/v1/signup-settings'),
  getBanner: () => api.get('/api/v1/public/banner'),
  // Public song-queue page (no auth required) -- chat users land here via !sq
  getMusicQueue: (communityId) => api.get(`/api/v1/public/communities/${communityId}/music-station/queue`),
};

export const communityApi = {
  getMyCommunities: () => api.get('/api/v1/communities/my'),
  create: (data) => api.post('/api/v1/communities/create', data),
  getDashboard: (id) => api.get(`/api/v1/communities/${id}/dashboard`),
  getLeaderboard: (id, params) => api.get(`/api/v1/communities/${id}/leaderboard`, { params }),
  getActivity: (id, params) => api.get(`/api/v1/communities/${id}/activity`, { params }),
  getEvents: (id, params) => api.get(`/api/v1/communities/${id}/events`, { params }),
  getMemories: (id, params) => api.get(`/api/v1/communities/${id}/memories`, { params }),
  getModules: (id) => api.get(`/api/v1/communities/${id}/modules`),
  getMembers: (id, params) => api.get(`/api/v1/communities/${id}/members`, { params }),
  updateProfile: (id, data) => api.put(`/api/v1/communities/${id}/profile`, data),
  leave: (id) => api.post(`/api/v1/communities/${id}/leave`),
  getChatHistory: (id, params) => api.get(`/api/v1/community/${id}/chat/history`, { params }),
  getChatChannels: (id) => api.get(`/api/v1/community/${id}/chat/channels`),
  // Join functionality
  join: (id, message) => api.post(`/api/v1/communities/${id}/join`, { message }),
  getMyJoinRequests: () => api.get('/api/v1/communities/join-requests'),
  cancelJoinRequest: (requestId) => api.delete(`/api/v1/communities/join-requests/${requestId}`),
  // Server linking (user adding their server)
  addServer: (id, data) => api.post(`/api/v1/communities/${id}/servers`, data),
  getServers: (id) => api.get(`/api/v1/communities/${id}/servers`),
  getMyServerLinkRequests: () => api.get('/api/v1/communities/server-link-requests'),
  cancelServerLinkRequest: (requestId) => api.delete(`/api/v1/communities/server-link-requests/${requestId}`),
  // Activity leaderboards
  getWatchTimeLeaderboard: (id, params) =>
    api.get(`/api/v1/communities/${id}/leaderboard/watch-time`, { params }),
  getMessageLeaderboard: (id, params) =>
    api.get(`/api/v1/communities/${id}/leaderboard/messages`, { params }),
  getMyActivityStats: (id) => api.get(`/api/v1/communities/${id}/activity/my-stats`),
  // Reputation visibility (gh-310) -- community + global score/tier for the
  // caller, and a top-scorers leaderboard (display names only, no ids).
  getMyReputation: (id) => api.get(`/api/v1/community/${id}/reputation/me`),
  getReputationLeaderboard: (id, params) =>
    api.get(`/api/v1/community/${id}/reputation/leaderboard`, { params }),
};

export const adminApi = {
  // Community settings
  getSettings: (communityId) => api.get(`/api/v1/admin/${communityId}/settings`),
  updateSettings: (communityId, data) => api.put(`/api/v1/admin/${communityId}/settings`, data),
  // Join requests
  getJoinRequests: (communityId, params) => api.get(`/api/v1/admin/${communityId}/join-requests`, { params }),
  approveJoinRequest: (communityId, requestId, note) =>
    api.post(`/api/v1/admin/${communityId}/join-requests/${requestId}/approve`, { note }),
  rejectJoinRequest: (communityId, requestId, note) =>
    api.post(`/api/v1/admin/${communityId}/join-requests/${requestId}/reject`, { note }),
  // Member management
  getMembers: (communityId, params) => api.get(`/api/v1/admin/${communityId}/members`, { params }),
  updateMemberRole: (communityId, userId, role) =>
    api.put(`/api/v1/admin/${communityId}/members/${userId}/role`, { role }),
  adjustReputation: (communityId, userId, amount, reason) =>
    api.put(`/api/v1/admin/${communityId}/members/${userId}/reputation`, { amount, reason }),
  removeMember: (communityId, userId, reason) =>
    api.delete(`/api/v1/admin/${communityId}/members/${userId}`, { data: { reason } }),
  getModules: (communityId) => api.get(`/api/v1/admin/${communityId}/modules`),
  updateModuleConfig: (communityId, moduleId, data) =>
    api.put(`/api/v1/admin/${communityId}/modules/${moduleId}/config`, data),
  getConnectedPlatforms: (communityId) => api.get(`/api/v1/admin/${communityId}/connected-platforms`),
  getCommunityOAuthCredentials: (communityId) => api.get(`/api/v1/admin/${communityId}/oauth/credentials`),
  createCommunityOAuthCredential: (communityId, data) => api.post(`/api/v1/admin/${communityId}/oauth/credentials`, data),
  updateCommunityOAuthCredential: (communityId, id, data) => api.put(`/api/v1/admin/${communityId}/oauth/credentials/${id}`, data),
  deleteCommunityOAuthCredential: (communityId, id) => api.delete(`/api/v1/admin/${communityId}/oauth/credentials/${id}`),
  testCommunityOAuthCredential: (communityId, id) => api.post(`/api/v1/admin/${communityId}/oauth/credentials/${id}/test`),
  getBrowserSources: (communityId) => api.get(`/api/v1/admin/${communityId}/browser-sources`),
  regenerateBrowserSources: (communityId, sourceType) =>
    api.post(`/api/v1/admin/${communityId}/browser-sources/regenerate`, { sourceType }),
  getDomains: (communityId) => api.get(`/api/v1/admin/${communityId}/domains`),
  addDomain: (communityId, domain) => api.post(`/api/v1/admin/${communityId}/domains`, { domain }),
  verifyDomain: (communityId, domainId) =>
    api.post(`/api/v1/admin/${communityId}/domains/${domainId}/verify`),
  removeDomain: (communityId, domainId) =>
    api.delete(`/api/v1/admin/${communityId}/domains/${domainId}`),
  generateTempPassword: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/temp-password`, data),
  // Server linking management
  getServers: (communityId, params) => api.get(`/api/v1/admin/${communityId}/servers`, { params }),
  updateServer: (communityId, serverId, data) =>
    api.put(`/api/v1/admin/${communityId}/servers/${serverId}`, data),
  removeServer: (communityId, serverId) =>
    api.delete(`/api/v1/admin/${communityId}/servers/${serverId}`),
  getServerLinkRequests: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/server-link-requests`, { params }),
  approveServerLinkRequest: (communityId, requestId, note) =>
    api.post(`/api/v1/admin/${communityId}/server-link-requests/${requestId}/approve`, { note }),
  rejectServerLinkRequest: (communityId, requestId, note) =>
    api.post(`/api/v1/admin/${communityId}/server-link-requests/${requestId}/reject`, { note }),
  // Mirror groups
  getMirrorGroups: (communityId) => api.get(`/api/v1/admin/${communityId}/mirror-groups`),
  getMirrorGroup: (communityId, groupId) =>
    api.get(`/api/v1/admin/${communityId}/mirror-groups/${groupId}`),
  createMirrorGroup: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/mirror-groups`, data),
  updateMirrorGroup: (communityId, groupId, data) =>
    api.put(`/api/v1/admin/${communityId}/mirror-groups/${groupId}`, data),
  deleteMirrorGroup: (communityId, groupId) =>
    api.delete(`/api/v1/admin/${communityId}/mirror-groups/${groupId}`),
  addMirrorGroupMember: (communityId, groupId, data) =>
    api.post(`/api/v1/admin/${communityId}/mirror-groups/${groupId}/members`, data),
  updateMirrorGroupMember: (communityId, groupId, memberId, data) =>
    api.put(`/api/v1/admin/${communityId}/mirror-groups/${groupId}/members/${memberId}`, data),
  removeMirrorGroupMember: (communityId, groupId, memberId) =>
    api.delete(`/api/v1/admin/${communityId}/mirror-groups/${groupId}/members/${memberId}`),
  // Leaderboard configuration
  getLeaderboardConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/leaderboard-config`),
  updateLeaderboardConfig: (communityId, data) =>
    api.put(`/api/v1/admin/${communityId}/leaderboard-config`, data),
  // Community profile management
  updateCommunityProfile: (communityId, data) =>
    api.put(`/api/v1/admin/${communityId}/profile`, data),
  uploadCommunityLogo: (communityId, file) => {
    const formData = new FormData();
    formData.append('logo', file);
    return api.post(`/api/v1/admin/${communityId}/logo`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  deleteCommunityLogo: (communityId) => api.delete(`/api/v1/admin/${communityId}/logo`),
  uploadCommunityBanner: (communityId, file) => {
    const formData = new FormData();
    formData.append('banner', file);
    return api.post(`/api/v1/admin/${communityId}/banner`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  deleteCommunityBanner: (communityId) => api.delete(`/api/v1/admin/${communityId}/banner`),
  // Reputation configuration (FICO-style 300-850 scoring)
  getReputationConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/reputation/config`),
  updateReputationConfig: (communityId, data) =>
    api.put(`/api/v1/admin/${communityId}/reputation/config`, data),
  getAtRiskUsers: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/reputation/at-risk`, { params }),
  getReputationLeaderboard: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/reputation/leaderboard`, { params }),
  // AI Insights
  getAIInsights: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/ai-insights`, { params }),
  getAIInsight: (communityId, insightId) =>
    api.get(`/api/v1/admin/${communityId}/ai-insights/${insightId}`),
  // AI Researcher Config
  getAIResearcherConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/ai-researcher/config`),
  updateAIResearcherConfig: (communityId, data) =>
    api.put(`/api/v1/admin/${communityId}/ai-researcher/config`, data),
  getAvailableAIModels: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/ai-researcher/available-models`),
  // AI Chatter Config
  getAIChatterConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/ai-chatter/config`),
  updateAIChatterConfig: (communityId, config) =>
    api.put(`/api/v1/admin/${communityId}/ai-chatter/config`, config),
  // Bot Detection
  getBotDetections: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/bot-detection`, { params }),
  getBotDetectionResults: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/bot-detection`, { params }),
  getBotScore: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/bot-score`),
  getSuspectedBots: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/suspected-bots`, { params }),
  reviewSuspectedBot: (communityId, botId, data) =>
    api.put(`/api/v1/admin/${communityId}/suspected-bots/${botId}/review`, data),
  reviewBotDetection: (communityId, resultId, data) =>
    api.post(`/api/v1/admin/${communityId}/bot-detection/${resultId}/review`, data),
  markBotDetectionReviewed: (communityId, resultId) =>
    api.post(`/api/v1/admin/${communityId}/bot-detection/${resultId}/mark-reviewed`),
  // Context Visualization
  getAIContext: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/ai-context`),
  // Overlay management
  getOverlay: (communityId) => api.get(`/api/v1/admin/${communityId}/overlay`),
  updateOverlay: (communityId, data) => api.put(`/api/v1/admin/${communityId}/overlay`, data),
  rotateOverlayKey: (communityId) => api.post(`/api/v1/admin/${communityId}/overlay/rotate`),
  getOverlayStats: (communityId) => api.get(`/api/v1/admin/${communityId}/overlay/stats`),
  // Loyalty configuration
  getLoyaltyConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/loyalty/config`),
  updateLoyaltyConfig: (communityId, data) =>
    api.put(`/api/v1/admin/${communityId}/loyalty/config`, data),
  // Loyalty leaderboard
  getLoyaltyLeaderboard: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/loyalty/leaderboard`, { params }),
  adjustLoyaltyBalance: (communityId, userId, data) =>
    api.put(`/api/v1/admin/${communityId}/loyalty/user/${userId}/balance`, data),
  wipeLoyaltyCurrency: (communityId) =>
    api.post(`/api/v1/admin/${communityId}/loyalty/wipe`),
  getLoyaltyStats: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/loyalty/stats`),
  // NOTE (gh-317): giveaways/games/gear-shop admin routes were removed —
  // hub-api's `services.community_loyalty` (MVP core-currency schema) has
  // no tables for them; see `hub_api/blueprints/v1/community_loyalty.py`'s
  // module docstring. Shop items (`loyalty_shop_items`) have a service-layer
  // `upsert_item()` but no admin blueprint route yet, so no client function
  // is added for them either.
  // Announcements
  getAnnouncements: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/announcements`, { params }),
  getAnnouncement: (communityId, announcementId) =>
    api.get(`/api/v1/admin/${communityId}/announcements/${announcementId}`),
  createAnnouncement: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/announcements`, data),
  updateAnnouncement: (communityId, announcementId, data) =>
    api.put(`/api/v1/admin/${communityId}/announcements/${announcementId}`, data),
  deleteAnnouncement: (communityId, announcementId) =>
    api.delete(`/api/v1/admin/${communityId}/announcements/${announcementId}`),
  publishAnnouncement: (communityId, announcementId) =>
    api.post(`/api/v1/admin/${communityId}/announcements/${announcementId}/publish`),
  pinAnnouncement: (communityId, announcementId) =>
    api.put(`/api/v1/admin/${communityId}/announcements/${announcementId}/pin`),
  unpinAnnouncement: (communityId, announcementId) =>
    api.put(`/api/v1/admin/${communityId}/announcements/${announcementId}/unpin`),
  archiveAnnouncement: (communityId, announcementId) =>
    api.post(`/api/v1/admin/${communityId}/announcements/${announcementId}/archive`),
  broadcastAnnouncement: (communityId, announcementId, platforms) =>
    api.post(`/api/v1/admin/${communityId}/announcements/${announcementId}/broadcast`, { platforms }),
  getBroadcastStatus: (communityId, announcementId) =>
    api.get(`/api/v1/admin/${communityId}/announcements/${announcementId}/broadcast-status`),
  // Analytics
  getAnalyticsBasic: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/analytics/basic`),
  getAnalyticsPoll: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/analytics/poll`),
  getAnalyticsHealthScore: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/analytics/health-score`),
  getAnalyticsBadActors: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/analytics/bad-actors`, { params }),
  getAnalyticsRetention: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/analytics/retention`),
  // Security
  getSecurityConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/security/config`),
  updateSecurityConfig: (communityId, data) =>
    api.put(`/api/v1/admin/${communityId}/security/config`, data),
  getSecurityBlockedWords: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/security/blocked-words`, { params }),
  addSecurityBlockedWord: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/security/blocked-words`, data),
  updateSecurityBlockedWord: (communityId, wordId, data) =>
    api.put(`/api/v1/admin/${communityId}/security/blocked-words/${wordId}`, data),
  deleteSecurityBlockedWord: (communityId, wordId) =>
    api.delete(`/api/v1/admin/${communityId}/security/blocked-words/${wordId}`),
  getSecurityWarnings: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/security/warnings`, { params }),
  getSecurityModerationLog: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/security/moderation-log`, { params }),
  // Shoutout configuration
  getShoutoutConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/shoutout/config`),
  updateShoutoutConfig: (communityId, data) =>
    api.put(`/api/v1/admin/${communityId}/shoutout/config`, data),
  getShoutoutCreators: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/shoutout/creators`),
  addShoutoutCreator: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/shoutout/creators`, data),
  removeShoutoutCreator: (communityId, creatorId) =>
    api.delete(`/api/v1/admin/${communityId}/shoutout/creators/${creatorId}`),
  getShoutoutHistory: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/shoutout/history`, { params }),
  // Translation configuration
  getTranslationConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/translation/config`),
  updateTranslationConfig: (communityId, config) =>
    api.put(`/api/v1/admin/${communityId}/translation/config`, config),
  // Music module
  getMusicSettings: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/music/settings`),
  updateMusicSettings: (communityId, settings) =>
    api.put(`/api/v1/admin/${communityId}/music/settings`, settings),
  getMusicProviders: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/music/providers`),
  initiateMusicProviderOAuth: (communityId, provider) =>
    api.post(`/api/v1/admin/${communityId}/music/providers/${provider}/oauth`),
  disconnectMusicProvider: (communityId, provider) =>
    api.delete(`/api/v1/admin/${communityId}/music/providers/${provider}`),
  updateMusicProviderConfig: (communityId, provider, config) =>
    api.put(`/api/v1/admin/${communityId}/music/providers/${provider}/config`, config),
  getRadioStations: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/music/radio-stations`),
  addRadioStation: (communityId, station) =>
    api.post(`/api/v1/admin/${communityId}/music/radio-stations`, station),
  deleteRadioStation: (communityId, stationId) =>
    api.delete(`/api/v1/admin/${communityId}/music/radio-stations/${stationId}`),
  testRadioStreamUrl: (communityId, stationId) =>
    api.post(`/api/v1/admin/${communityId}/music/radio-stations/${stationId}/test`),
  setDefaultRadioStation: (communityId, stationId) =>
    api.put(`/api/v1/admin/${communityId}/music/radio-stations/${stationId}/default`),
  getMusicDashboard: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/music/dashboard`),
  controlPlayback: (communityId, action) =>
    api.post(`/api/v1/admin/${communityId}/music/playback/control`, { action }),
  // Moderation from the public queue page (admin/moderator only, server-enforced)
  removeMusicQueueItem: (communityId, itemId) =>
    api.delete(`/api/v1/admin/${communityId}/music-station/queue/${itemId}`),
  // Music Station policy (YouTube allowed labels, etc.)
  getMusicStationPolicy: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/music-station/policy`),
  updateMusicStationPolicy: (communityId, policy) =>
    api.put(`/api/v1/admin/${communityId}/music-station/policy`, policy),

  // Calendar Events
  getCalendarEvents: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events`, { params }),
  createCalendarEvent: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events`, data),
  getCalendarEvent: (communityId, eventId) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}`),
  updateCalendarEvent: (communityId, eventId, data) =>
    api.put(`/api/v1/admin/${communityId}/calendar/events/${eventId}`, data),
  deleteCalendarEvent: (communityId, eventId) =>
    api.delete(`/api/v1/admin/${communityId}/calendar/events/${eventId}`),
  approveCalendarEvent: (communityId, eventId) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/approve`),
  rejectCalendarEvent: (communityId, eventId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/reject`, data),
  getEventRsvpCounts: (communityId, eventId) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/rsvp-counts`),
  getEventAttendees: (communityId, eventId) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/attendees`),

  // ===== Calendar Ticketing =====
  // Ticket types
  getTicketTypes: (communityId, eventId) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/ticket-types`),
  createTicketType: (communityId, eventId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/ticket-types`, data),
  updateTicketType: (communityId, eventId, typeId, data) =>
    api.put(`/api/v1/admin/${communityId}/calendar/events/${eventId}/ticket-types/${typeId}`, data),
  deleteTicketType: (communityId, eventId, typeId) =>
    api.delete(`/api/v1/admin/${communityId}/calendar/events/${eventId}/ticket-types/${typeId}`),

  // Tickets
  getTickets: (communityId, eventId, params) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/tickets`, { params }),
  createTicket: (communityId, eventId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/tickets`, data),
  getTicket: (communityId, eventId, ticketId) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/tickets/${ticketId}`),
  cancelTicket: (communityId, eventId, ticketId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/tickets/${ticketId}/cancel`, data),
  transferTicket: (communityId, eventId, ticketId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/tickets/${ticketId}/transfer`, data),

  // Check-in
  verifyTicket: (ticketCode, performCheckin = true) =>
    api.post('/api/v1/admin/calendar/verify-ticket', { ticket_code: ticketCode, perform_checkin: performCheckin }),
  checkIn: (communityId, eventId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/check-in`, data),
  undoCheckIn: (communityId, eventId, ticketId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/tickets/${ticketId}/undo-check-in`, data),

  // Attendance & Reporting
  getAttendanceStats: (communityId, eventId) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/attendance`),
  getCheckInLog: (communityId, eventId, params) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/check-in-log`, { params }),
  exportAttendance: (communityId, eventId, format = 'json') =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/attendance/export`, { params: { format } }),

  // Event admins
  getEventAdmins: (communityId, eventId) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/admins`),
  assignEventAdmin: (communityId, eventId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/admins`, data),
  updateEventAdmin: (communityId, eventId, adminId, data) =>
    api.put(`/api/v1/admin/${communityId}/calendar/events/${eventId}/admins/${adminId}`, data),
  revokeEventAdmin: (communityId, eventId, adminId, data) =>
    api.delete(`/api/v1/admin/${communityId}/calendar/events/${eventId}/admins/${adminId}`, { data }),
  getMyEventPermissions: (communityId, eventId) =>
    api.get(`/api/v1/admin/${communityId}/calendar/events/${eventId}/my-permissions`),

  // Ticketing configuration
  enableTicketing: (communityId, eventId, data) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/ticketing/enable`, data),
  disableTicketing: (communityId, eventId) =>
    api.post(`/api/v1/admin/${communityId}/calendar/events/${eventId}/ticketing/disable`),

  // ===== Live Streaming =====
  getStreamConfig: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/streams`),
  createStreamConfig: (communityId) =>
    api.post(`/api/v1/admin/${communityId}/streams`),
  regenerateStreamKey: (communityId) =>
    api.post(`/api/v1/admin/${communityId}/streams/key/regenerate`),
  getStreamDestinations: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/streams/destinations`),
  addStreamDestination: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/streams/destinations`, data),
  removeStreamDestination: (communityId, destId) =>
    api.delete(`/api/v1/admin/${communityId}/streams/destinations/${destId}`),
  toggleStreamForceCut: (communityId, destId) =>
    api.put(`/api/v1/admin/${communityId}/streams/destinations/${destId}/force-cut`),
  getStreamStatus: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/streams/status`),

  // ===== Community Calls (WebRTC) =====
  getCallRooms: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/calls/rooms`),
  getCallRoom: (communityId, roomName) =>
    api.get(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}`),
  createCallRoom: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/calls/rooms`, data),
  deleteCallRoom: (communityId, roomName) =>
    api.delete(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}`),
  lockCallRoom: (communityId, roomName) =>
    api.post(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}/lock`),
  unlockCallRoom: (communityId, roomName) =>
    api.post(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}/unlock`),
  getCallParticipants: (communityId, roomName) =>
    api.get(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}/participants`),
  kickCallParticipant: (communityId, roomName, identity) =>
    api.post(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}/kick`, { identity }),
  muteAllCallParticipants: (communityId, roomName) =>
    api.post(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}/mute-all`),
  getRaisedHands: (communityId, roomName) =>
    api.get(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}/raised-hands`),
  acknowledgeHand: (communityId, roomName, userId) =>
    api.post(`/api/v1/admin/${communityId}/calls/rooms/${encodeURIComponent(roomName)}/acknowledge-hand`, { user_id: userId }),

  // ===== Polls =====
  getPolls: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/polls`),
  getPoll: (communityId, pollId) =>
    api.get(`/api/v1/admin/${communityId}/polls/${pollId}`),
  createPoll: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/polls`, data),
  deletePoll: (communityId, pollId) =>
    api.delete(`/api/v1/admin/${communityId}/polls/${pollId}`),

  // ===== Forms =====
  getForms: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/forms`),
  getForm: (communityId, formId) =>
    api.get(`/api/v1/admin/${communityId}/forms/${formId}`),
  createForm: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/forms`, data),
  deleteForm: (communityId, formId) =>
    api.delete(`/api/v1/admin/${communityId}/forms/${formId}`),
  getFormSubmissions: (communityId, formId) =>
    api.get(`/api/v1/admin/${communityId}/forms/${formId}/submissions`),

  // ===== Support Tickets =====
  getSupportCategories: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/support/categories`),
  createSupportCategory: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/support/categories`, data),
  updateSupportCategory: (communityId, categoryId, data) =>
    api.put(`/api/v1/admin/${communityId}/support/categories/${categoryId}`, data),
  deleteSupportCategory: (communityId, categoryId) =>
    api.delete(`/api/v1/admin/${communityId}/support/categories/${categoryId}`),
  getSupportTickets: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/support/tickets`, { params }),
  getSupportTicket: (communityId, ticketId) =>
    api.get(`/api/v1/admin/${communityId}/support/tickets/${ticketId}`),
  updateTicketStatus: (communityId, ticketId, status) =>
    api.put(`/api/v1/admin/${communityId}/support/tickets/${ticketId}/status`, { status }),
  assignSupportTicket: (communityId, ticketId, assigneeUserId) =>
    api.put(`/api/v1/admin/${communityId}/support/tickets/${ticketId}/assign`, { assignee_user_id: assigneeUserId }),
  updateSupportTicketPriority: (communityId, ticketId, priority) =>
    api.put(`/api/v1/admin/${communityId}/support/tickets/${ticketId}/priority`, { priority }),
  addSupportTicketComment: (communityId, ticketId, content, isInternal) =>
    api.post(`/api/v1/admin/${communityId}/support/tickets/${ticketId}/comments`, { content, is_internal: isInternal }),
  getSupportStats: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/support/stats`),
  // Commands (read-only)
  getCommands: (communityId) => api.get(`/api/v1/admin/${communityId}/commands`),
  // Server link requests (community-initiated)
  createServerLinkRequest: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/server-link-requests`, data),
  // Community OAuth connections (gh-320)
  listCommunityConnections: (communityId) =>
    api.get(`/api/v1/communities/${communityId}/connections`),
  authorizeCommunityConnection: (communityId, provider) =>
    api.post(`/api/v1/communities/${communityId}/connections/${provider}/authorize`),
  disconnectCommunityConnection: (communityId, provider) =>
    api.delete(`/api/v1/communities/${communityId}/connections/${provider}`),
};

export const supportApi = {
  submitTicket: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/support/submit`, data),
  getMyTickets: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/support/my-tickets`),
  getMyTicket: (communityId, ticketId) =>
    api.get(`/api/v1/admin/${communityId}/support/my-tickets/${ticketId}`),
  addComment: (communityId, ticketId, content) =>
    api.post(`/api/v1/admin/${communityId}/support/my-tickets/${ticketId}/comments`, { content }),
  getCategories: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/support/categories`),
};

export const platformApi = {
  getUsers: (params) => api.get('/api/v1/platform/users', { params }),
  getUser: (id) => api.get(`/api/v1/platform/users/${id}`),
  updateUserRole: (id, role) => api.put(`/api/v1/platform/users/${id}/role`, { role }),
  deactivateUser: (id, reason) => api.delete(`/api/v1/platform/users/${id}`, { data: { reason } }),
  getCommunities: (params) => api.get('/api/v1/platform/communities', { params }),
  getCommunity: (id) => api.get(`/api/v1/platform/communities/${id}`),
  updateCommunity: (id, data) => api.put(`/api/v1/platform/communities/${id}`, data),
  deactivateCommunity: (id, reason) =>
    api.delete(`/api/v1/platform/communities/${id}`, { data: { reason } }),
  getHealth: () => api.get('/api/v1/platform/health'),
  getModules: () => api.get('/api/v1/platform/modules'),
  getAuditLog: (params) => api.get('/api/v1/platform/audit-log', { params }),
  getStats: () => api.get('/api/v1/platform/stats'),
};

export const superAdminApi = {
  getDashboard: () => api.get('/api/v1/superadmin/dashboard'),
  // Analytics
  getAnalytics: () => api.get('/api/v1/superadmin/analytics'),
  getReputationDistribution: () => api.get('/api/v1/superadmin/analytics/reputation'),
  getGrowthTrends: (params) => api.get('/api/v1/superadmin/analytics/growth', { params }),
  getActivityBreakdown: () => api.get('/api/v1/superadmin/analytics/activity'),
  getCommunities: (params) => api.get('/api/v1/superadmin/communities', { params }),
  getCommunity: (id) => api.get(`/api/v1/superadmin/communities/${id}`),
  createCommunity: (data) => api.post('/api/v1/superadmin/communities', data),
  updateCommunity: (id, data) => api.put(`/api/v1/superadmin/communities/${id}`, data),
  deleteCommunity: (id) => api.delete(`/api/v1/superadmin/communities/${id}`),
  reassignOwner: (id, data) => api.post(`/api/v1/superadmin/communities/${id}/reassign`, data),
  // Module registry
  getAllModules: (params) => api.get('/api/v1/superadmin/marketplace/modules', { params }),
  createModule: (data) => api.post('/api/v1/superadmin/marketplace/modules', data),
  updateModule: (id, data) => api.put(`/api/v1/superadmin/marketplace/modules/${id}`, data),
  publishModule: (id, isPublished) => api.put(`/api/v1/superadmin/marketplace/modules/${id}/publish`, { isPublished }),
  deleteModule: (id) => api.delete(`/api/v1/superadmin/marketplace/modules/${id}`),
  // Platform configuration
  getPlatformConfigs: () => api.get('/api/v1/superadmin/platform-config'),
  updatePlatformConfig: (platform, data) => api.put(`/api/v1/superadmin/platform-config/${platform}`, data),
  testPlatformConnection: (platform) => api.post(`/api/v1/superadmin/platform-config/${platform}/test`),
  // Hub settings
  getHubSettings: () => api.get('/api/v1/superadmin/settings'),
  updateHubSettings: (data) => api.put('/api/v1/superadmin/settings', data),
  // Storage testing
  testStorageConnection: () => api.post('/api/v1/superadmin/platform-config/storage/test'),
  // Software & Repository Discovery
  getSoftwareRepositories: () => api.get('/api/v1/superadmin/software/repositories'),
  getSoftwareRepository: (id) => api.get(`/api/v1/superadmin/software/repositories/${id}`),
  addSoftwareRepository: (data) => api.post('/api/v1/superadmin/software/repositories', data),
  updateSoftwareRepository: (id, data) => api.put(`/api/v1/superadmin/software/repositories/${id}`, data),
  deleteSoftwareRepository: (id) => api.delete(`/api/v1/superadmin/software/repositories/${id}`),
  scanSoftwareRepository: (id) => api.post(`/api/v1/superadmin/software/repositories/${id}/scan`),
  testRepositoryConnection: (data) => api.post('/api/v1/superadmin/software/repositories/test', data),
  getRepositoryDependencies: (id) => api.get(`/api/v1/superadmin/software/repositories/${id}/dependencies`),
  // Service Discovery
  getServices: () => api.get('/api/v1/superadmin/services'),
  getService: (id) => api.get(`/api/v1/superadmin/services/${id}`),
  addService: (data) => api.post('/api/v1/superadmin/services', data),
  updateService: (id, data) => api.put(`/api/v1/superadmin/services/${id}`, data),
  deleteService: (id) => api.delete(`/api/v1/superadmin/services/${id}`),
  refreshService: (id) => api.post(`/api/v1/superadmin/services/${id}/refresh`),
  refreshAllServices: () => api.post('/api/v1/superadmin/services/refresh-all'),
  // User management
  listUsers: (params) => api.get('/api/v1/superadmin/users', { params }),
  getUser: (userId) => api.get(`/api/v1/superadmin/users/${userId}`),
  createUser: (data) => api.post('/api/v1/superadmin/users', data),
  updateUser: (userId, data) => api.put(`/api/v1/superadmin/users/${userId}`, data),
  deleteUser: (userId) => api.delete(`/api/v1/superadmin/users/${userId}`),
  assignSuperAdminRole: (userId, grant) =>
    api.post(`/api/v1/superadmin/users/${userId}/super-admin-role`, { grant }),
  assignVendorRole: (userId, grant) =>
    api.post(`/api/v1/superadmin/users/${userId}/vendor-role`, { grant }),
  setEmailVerification: (userId, verified) =>
    api.post(`/api/v1/superadmin/users/${userId}/verify-email`, { verified }),
  generatePasswordReset: (userId) =>
    api.post(`/api/v1/superadmin/users/${userId}/password-reset`),
};

// Marketplace API
export const marketplaceApi = {
  browseModules: (communityId, params) => api.get(`/api/v1/admin/${communityId}/marketplace/modules`, { params }),
  getModuleDetails: (communityId, moduleId) => api.get(`/api/v1/admin/${communityId}/marketplace/modules/${moduleId}`),
  installModule: (communityId, moduleId) => api.post(`/api/v1/admin/${communityId}/marketplace/modules/${moduleId}/install`),
  uninstallModule: (communityId, moduleId) => api.delete(`/api/v1/admin/${communityId}/marketplace/modules/${moduleId}`),
  configureModule: (communityId, moduleId, data) => api.put(`/api/v1/admin/${communityId}/marketplace/modules/${moduleId}/config`, data),
  addReview: (communityId, moduleId, data) => api.post(`/api/v1/admin/${communityId}/marketplace/modules/${moduleId}/review`, data),
};

// New unified marketplace API (routes through marketplace module)
export const unifiedMarketplaceApi = {
  // Catalog
  getCatalog: (params) => api.get('/api/v1/marketplace/catalog', { params }),
  getCatalogEntry: (source, id, params) => api.get(`/api/v1/marketplace/catalog/${source}/${id}`, { params }),
  getCategories: () => api.get('/api/v1/marketplace/catalog/categories'),
  getFeatured: (params) => api.get('/api/v1/marketplace/catalog/featured', { params }),

  // Community installations
  getInstalled: (communityId) => api.get(`/api/v1/marketplace/communities/${communityId}/installed`),
  installModule: (communityId, data) => api.post(`/api/v1/marketplace/communities/${communityId}/install`, data),
  uninstallModule: (communityId, moduleId, source) => api.delete(`/api/v1/marketplace/communities/${communityId}/install/${moduleId}`, { params: { source } }),
  toggleModule: (communityId, moduleId, data) => api.put(`/api/v1/marketplace/communities/${communityId}/install/${moduleId}`, data),

  // Premium
  getPricing: (params) => api.get('/api/v1/marketplace/premium/pricing', { params }),
  getPremiumStatus: (communityId) => api.get(`/api/v1/marketplace/premium/status/${communityId}`),
  subscribePremium: (data) => api.post('/api/v1/marketplace/premium/subscribe', data),
  cancelPremium: (data) => api.post('/api/v1/marketplace/premium/cancel', data),
};

export const vendorApi = {
  getProfile: () => api.get('/api/v1/marketplace/vendor/profile'),
  createProfile: (data) => api.post('/api/v1/marketplace/vendor/profile', data),
  getModules: (params) => api.get('/api/v1/marketplace/vendor/modules', { params }),
  createModule: (data) => api.post('/api/v1/marketplace/vendor/modules', data),
  updateModule: (id, data) => api.put(`/api/v1/marketplace/vendor/modules/${id}`, data),
  submitForReview: (id, data) => api.post(`/api/v1/marketplace/vendor/modules/${id}/submit`, data),
  getRequest: () => api.get('/api/v1/marketplace/vendor/request'),
  createRequest: (data) => api.post('/api/v1/marketplace/vendor/request', data),
};

export const marketplaceAdminApi = {
  getVendorRequests: (params) => api.get('/api/v1/marketplace/admin/marketplace/vendor-requests', { params }),
  approveVendorRequest: (id, data) => api.post(`/api/v1/marketplace/admin/marketplace/vendor-requests/${id}/approve`, data),
  rejectVendorRequest: (id, data) => api.post(`/api/v1/marketplace/admin/marketplace/vendor-requests/${id}/reject`, data),
  getSubmissions: (params) => api.get('/api/v1/marketplace/admin/marketplace/submissions', { params }),
  approveSubmission: (id, data) => api.post(`/api/v1/marketplace/admin/marketplace/submissions/${id}/approve`, data),
  rejectSubmission: (id, data) => api.post(`/api/v1/marketplace/admin/marketplace/submissions/${id}/reject`, data),
  getSettings: () => api.get('/api/v1/marketplace/admin/marketplace/settings'),
  updateSettings: (data) => api.put('/api/v1/marketplace/admin/marketplace/settings', data),
};

// User Identity & Profile API
export const userApi = {
  // Identity management
  getIdentities: () => api.get('/api/v1/user/identities'),
  linkIdentity: (platform) => api.post(`/api/v1/user/identities/link/${platform}`),
  unlinkIdentity: (platform) => api.delete(`/api/v1/user/identities/${platform}`),
  getPrimaryIdentity: () => api.get('/api/v1/user/identities/primary'),
  setPrimaryIdentity: (platform) => api.put('/api/v1/user/identities/primary', { platform }),
  // Profile management
  getMyProfile: () => api.get('/api/v1/user/profile'),
  updateProfile: (data) => api.put('/api/v1/user/profile', data),
  uploadAvatar: (file) => {
    const formData = new FormData();
    formData.append('avatar', file);
    return api.post('/api/v1/user/profile/avatar', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  deleteAvatar: () => api.delete('/api/v1/user/profile/avatar'),
  getLinkedPlatforms: () => api.get('/api/v1/user/linked-platforms'),
  // View other profiles
  getPublicProfile: (userId) => api.get(`/api/v1/public/users/${userId}/profile`),
  getMemberProfile: (communityId, userId) =>
    api.get(`/api/v1/communities/${communityId}/members/${userId}/profile`),
};

// Analytics API
export const analyticsApi = {
  getMyStats: () => api.get('/api/v1/analytics/me/stats'),
  getMyReputation: () => api.get('/api/v1/analytics/me/reputation'),
  getMemberStats: (communityId, userId) => api.get(`/api/v1/analytics/community/${communityId}/members/${userId}/stats`),
  getMemberReputation: (communityId, userId) => api.get(`/api/v1/analytics/community/${communityId}/members/${userId}/reputation`),
  getPlatformOverview: () => api.get('/api/v1/analytics/platform/overview'),
  getPlatformReputation: () => api.get('/api/v1/analytics/platform/reputation'),
  getPlatformGrowth: (period = '30d') => api.get(`/api/v1/analytics/platform/growth?period=${period}`),
  getPlatformActivity: () => api.get('/api/v1/analytics/platform/activity'),
  getCommunityHealth: (limit = 50) => api.get(`/api/v1/analytics/platform/community-health?limit=${limit}`),
  getAdminUserStats: (userId) => api.get(`/api/v1/analytics/admin/users/${userId}/stats`),
};

// Stream API
export const streamApi = {
  getLiveStreams: (communityId) => api.get(`/api/v1/communities/${communityId}/streams`),
  getFeaturedStreams: (communityId) => api.get(`/api/v1/communities/${communityId}/streams/featured`),
  getStreamDetails: (communityId, entityId) =>
    api.get(`/api/v1/communities/${communityId}/streams/${entityId}`),
};

// Workflow API
export const workflowApi = {
  // Workflow management
  listWorkflows: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/workflows`, { params }),
  getWorkflow: (communityId, workflowId) =>
    api.get(`/api/v1/admin/${communityId}/workflows/${workflowId}`),
  createWorkflow: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/workflows`, data),
  updateWorkflow: (communityId, workflowId, data) =>
    api.put(`/api/v1/admin/${communityId}/workflows/${workflowId}`, data),
  deleteWorkflow: (communityId, workflowId) =>
    api.delete(`/api/v1/admin/${communityId}/workflows/${workflowId}`),

  // Workflow publishing
  publishWorkflow: (communityId, workflowId) =>
    api.post(`/api/v1/admin/${communityId}/workflows/${workflowId}/publish`),
  unpublishWorkflow: (communityId, workflowId) =>
    api.post(`/api/v1/admin/${communityId}/workflows/${workflowId}/unpublish`),

  // Workflow validation
  validateWorkflow: (communityId, workflowId) =>
    api.post(`/api/v1/admin/${communityId}/workflows/${workflowId}/validate`),

  // Workflow execution
  executeWorkflow: (communityId, workflowId, data) =>
    api.post(`/api/v1/admin/${communityId}/workflows/${workflowId}/execute`, data),
  testWorkflow: (communityId, workflowId, data) =>
    api.post(`/api/v1/admin/${communityId}/workflows/${workflowId}/test`, data),

  // Execution history
  getExecutions: (communityId, workflowId, params) =>
    api.get(`/api/v1/admin/${communityId}/workflows/${workflowId}/executions`, { params }),
  getExecution: (communityId, workflowId, executionId) =>
    api.get(`/api/v1/admin/${communityId}/workflows/${workflowId}/executions/${executionId}`),
  cancelExecution: (communityId, workflowId, executionId) =>
    api.post(`/api/v1/admin/${communityId}/workflows/${workflowId}/executions/${executionId}/cancel`),

  // Webhooks for workflow triggers
  listWebhooks: (communityId, workflowId) =>
    api.get(`/api/v1/admin/${communityId}/workflows/${workflowId}/webhooks`),
  createWebhook: (communityId, workflowId, data) =>
    api.post(`/api/v1/admin/${communityId}/workflows/${workflowId}/webhooks`, data),
  deleteWebhook: (communityId, workflowId, webhookId) =>
    api.delete(`/api/v1/admin/${communityId}/workflows/${workflowId}/webhooks/${webhookId}`),
  regenerateWebhookSecret: (communityId, workflowId, webhookId) =>
    api.post(`/api/v1/admin/${communityId}/workflows/${workflowId}/webhooks/${webhookId}/regenerate`),
};

// Calendar API
export const calendarApi = {
  // OAuth
  getGoogleAuthUrl: () => api.get('/api/v1/calendar/oauth/google/auth-url'),
  getMicrosoftAuthUrl: () => api.get('/api/v1/calendar/oauth/microsoft/auth-url'),
  getConnectedCalendars: () => api.get('/api/v1/calendar/oauth/calendars'),
  syncCalendar: (id) => api.post(`/api/v1/calendar/oauth/calendars/${id}/sync`),
  disconnectCalendar: (id) => api.delete(`/api/v1/calendar/oauth/calendars/${id}`),

  // Availability
  getAvailabilitySettings: () => api.get('/api/v1/calendar/availability/settings'),
  updateAvailabilitySettings: (data) => api.put('/api/v1/calendar/availability/settings', data),
  getWeeklyAvailability: () => api.get('/api/v1/calendar/availability/weekly'),
  updateWeeklyAvailability: (data) => api.put('/api/v1/calendar/availability/weekly', data),
  getAvailableSlots: (userId, date, duration) => api.get(`/api/v1/calendar/availability/${userId}/slots`, { params: { date, duration } }),

  // Booking Pages
  createBookingPage: (data) => api.post('/api/v1/calendar/booking-pages', data),
  getBookingPages: () => api.get('/api/v1/calendar/booking-pages'),
  getBookingPage: (idOrSlug) => api.get(`/api/v1/calendar/booking-pages/${idOrSlug}`),
  updateBookingPage: (id, data) => api.put(`/api/v1/calendar/booking-pages/${id}`, data),
  deleteBookingPage: (id) => api.delete(`/api/v1/calendar/booking-pages/${id}`),

  // Public Booking
  getBookingSlots: (slug, date) => api.get(`/api/v1/calendar/book/${slug}/slots`, { params: { date } }),
  createBooking: (slug, data) => api.post(`/api/v1/calendar/book/${slug}`, data),
  getBooking: (uuid) => api.get(`/api/v1/calendar/bookings/${uuid}`),
  cancelBooking: (uuid) => api.delete(`/api/v1/calendar/bookings/${uuid}`),
  getMyBookings: (params) => api.get('/api/v1/calendar/my-bookings', { params }),

  // Group
  addGroupMember: (pageId, data) => api.post(`/api/v1/calendar/booking-pages/${pageId}/members`, data),
  removeGroupMember: (pageId, userId) => api.delete(`/api/v1/calendar/booking-pages/${pageId}/members/${userId}`),
  getGroupMembers: (pageId) => api.get(`/api/v1/calendar/booking-pages/${pageId}/members`),
  getGroupAvailability: (pageId, date) => api.get(`/api/v1/calendar/booking-pages/${pageId}/group-availability`, { params: { date } }),
  getBestSlots: (pageId, start, end, limit) => api.get(`/api/v1/calendar/booking-pages/${pageId}/best-slots`, { params: { start, end, limit } }),
};

// Inventory (Quartermaster) API
export const inventoryApi = {
  // Admin
  listItems: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/inventory/items`),
  createItem: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/inventory/items`, data),
  updateItem: (communityId, itemId, data) =>
    api.put(`/api/v1/admin/${communityId}/inventory/items/${itemId}`, data),
  deleteItem: (communityId, itemId) =>
    api.delete(`/api/v1/admin/${communityId}/inventory/items/${itemId}`),
  addStock: (communityId, itemId, data) =>
    api.post(`/api/v1/admin/${communityId}/inventory/items/${itemId}/stock/add`, data),
  removeStock: (communityId, itemId, data) =>
    api.post(`/api/v1/admin/${communityId}/inventory/items/${itemId}/stock/remove`, data),
  listAllCheckouts: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/inventory/checkouts`, { params }),
  getSummary: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/inventory/summary`),
  getAuditLog: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/inventory/log`, { params }),

  // Member
  listAvailable: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/inventory/available`, { params }),
  checkoutItem: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/inventory/checkout`, data),
  checkinItem: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/inventory/checkin`, data),
  getMyCheckouts: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/inventory/my-items`),
};

// Personal Access Token (PAT) + Community Access Token (CAT) API
export const tokenApi = {
  // User PAT
  getPATScopes: () =>
    api.get('/api/v1/user/tokens/scopes'),
  getPAT: () =>
    api.get('/api/v1/user/tokens/pat'),
  createPAT: (data) =>
    api.post('/api/v1/user/tokens/pat', data),
  revokePAT: () =>
    api.delete('/api/v1/user/tokens/pat'),

  // Community CAT (admin)
  getCATScopes: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/tokens/scopes`),
  listCATs: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/tokens/cats`),
  createCAT: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/tokens/cats`, data),
  revokeCAT: (communityId, tokenId) =>
    api.delete(`/api/v1/admin/${communityId}/tokens/cats/${tokenId}`),
};

// ─── Join Request API ────────────────────────────────────────────────────────
export const joinRequestApi = {
  submit: (communityId, data) => api.post(`/community/${communityId}/join-requests`, data),
  getMine: (communityId) => api.get(`/community/${communityId}/join-requests/mine`),
  list: (communityId) => api.get(`/admin/${communityId}/join-requests`),
  approve: (communityId, requestId) => api.put(`/admin/${communityId}/join-requests/${requestId}/approve`),
  reject: (communityId, requestId) => api.put(`/admin/${communityId}/join-requests/${requestId}/reject`),
};

// ─── Passkey API ─────────────────────────────────────────────────────────────
export const userOAuthApi = {
  getCredentials: () => api.get('/api/v1/user/oauth/credentials'),
  createCredential: (data) => api.post('/api/v1/user/oauth/credentials', data),
  updateCredential: (id, data) => api.put(`/api/v1/user/oauth/credentials/${id}`, data),
  deleteCredential: (id) => api.delete(`/api/v1/user/oauth/credentials/${id}`),
  testCredential: (id) => api.post(`/api/v1/user/oauth/credentials/${id}/test`),
};

export const passkeyApi = {
  startRegistration: () => api.post('/api/v1/user/passkey/register/start'),
  finishRegistration: (data) => api.post('/api/v1/user/passkey/register/finish', data),
  listCredentials: () => api.get('/api/v1/user/passkey/credentials'),
  removeCredential: (id) => api.delete(`/api/v1/user/passkey/credentials/${id}`),
  startLogin: (data) => api.post('/api/v1/auth/passkey/login/start', data),
  finishLogin: (data) => api.post('/api/v1/auth/passkey/login/finish', data),
};

// ─── Interaction (Hub Channels, Forums, Voice) API ──────────────────────────
export const interactionApi = {
  // Admin — channel CRUD
  getChannels: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/interaction/channels`),
  createChannel: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/interaction/channels`, data),
  updateChannel: (communityId, channelId, data) =>
    api.put(`/api/v1/admin/${communityId}/interaction/channels/${channelId}`, data),
  deleteChannel: (communityId, channelId) =>
    api.delete(`/api/v1/admin/${communityId}/interaction/channels/${channelId}`),

  // Member — channels list (community route)
  getMemberChannels: (communityId) =>
    api.get(`/api/v1/community/${communityId}/interact/channels`),
  createMemberChannel: (communityId, data) =>
    api.post(`/api/v1/community/${communityId}/interact/channels`, data),

  // Forum
  getForumPosts: (communityId, channelId, params) =>
    api.get(`/api/v1/community/${communityId}/interact/forum/${channelId}/posts`, { params }),
  getForumPost: (communityId, channelId, postId) =>
    api.get(`/api/v1/community/${communityId}/interact/forum/${channelId}/posts/${postId}`),
  createForumPost: (communityId, channelId, data) =>
    api.post(`/api/v1/community/${communityId}/interact/forum/${channelId}/posts`, data),
  createForumReply: (communityId, postId, data) =>
    api.post(`/api/v1/community/${communityId}/interact/forum/posts/${postId}/replies`, data),

  // Admin — forum moderation
  moderatePost: (communityId, postId, data) =>
    api.put(`/api/v1/admin/${communityId}/interaction/forum/posts/${postId}`, data),
  deleteReply: (communityId, replyId) =>
    api.delete(`/api/v1/admin/${communityId}/interaction/forum/replies/${replyId}`),

  // Voice rooms (proxied to module_rtc via calls controller)
  getVoiceRooms: (communityId) =>
    api.get(`/api/v1/community/${communityId}/interact/voice/rooms`),
  joinVoiceRoom: (communityId, roomName) =>
    api.post(`/api/v1/community/${communityId}/interact/voice/rooms/${encodeURIComponent(roomName)}/join`),
  leaveVoiceRoom: (communityId, roomName) =>
    api.post(`/api/v1/community/${communityId}/interact/voice/rooms/${encodeURIComponent(roomName)}/leave`),
  createAdHocVoiceRoom: (communityId, data) =>
    api.post(`/api/v1/community/${communityId}/interact/voice/rooms`, data),
};

// ─── Server Manager (RCON / Voice) API ──────────────────────────────────────
export const rconApi = {
  // Admin
  listServers: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/rcon/servers`),
  createServer: (communityId, data) =>
    api.post(`/api/v1/admin/${communityId}/rcon/servers`, data),
  updateServer: (communityId, serverId, data) =>
    api.put(`/api/v1/admin/${communityId}/rcon/servers/${serverId}`, data),
  deleteServer: (communityId, serverId) =>
    api.delete(`/api/v1/admin/${communityId}/rcon/servers/${serverId}`),
  testConnection: (communityId, serverId, data) =>
    api.post(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/test`, data),
  executeCommand: (communityId, serverId, data) =>
    api.post(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/command`, data),
  kickPlayer: (communityId, serverId, data) =>
    api.post(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/kick`, data),
  banPlayer: (communityId, serverId, data) =>
    api.post(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/ban`, data),
  getChannels: (communityId, serverId) =>
    api.get(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/channels`),
  moveUser: (communityId, serverId, data) =>
    api.post(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/move`, data),
  sendMessage: (communityId, serverId, data) =>
    api.post(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/message`, data),
  getCommandLog: (communityId, params) =>
    api.get(`/api/v1/admin/${communityId}/rcon/log`, { params }),
  getAccessPolicy: (communityId, serverId) =>
    api.get(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/policy`),
  updateAccessPolicy: (communityId, serverId, data) =>
    api.put(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/policy`, data),
  triggerEnforcement: (communityId, serverId) =>
    api.post(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/enforce`),
  getAccessLog: (communityId, serverId, params) =>
    api.get(`/api/v1/admin/${communityId}/rcon/servers/${serverId}/access-log`, { params }),

  // Member
  listInfo: (communityId) =>
    api.get(`/api/v1/admin/${communityId}/rcon/info`),
  getServerStatus: (communityId, serverId) =>
    api.get(`/api/v1/admin/${communityId}/rcon/info/${serverId}/status`),
  getPlayerList: (communityId, serverId) =>
    api.get(`/api/v1/admin/${communityId}/rcon/info/${serverId}/players`),
};

// ── Tenant API ─────────────────────────────────────────────────────
export const tenantApi = {
  getLoginInfo: (slug) => api.get(`/api/v1/auth/tenant/${slug}`),
  getTenant: (slug) => api.get(`/api/v1/tenant/${slug}`),
  updateTenant: (slug, data) => api.put(`/api/v1/tenant/${slug}`, data),
  getSettings: (slug) => api.get(`/api/v1/tenant/${slug}/settings`),
  updateSettings: (slug, settings) => api.put(`/api/v1/tenant/${slug}/settings`, { settings }),
  getCommunities: (slug, params) => api.get(`/api/v1/tenant/${slug}/communities`, { params }),
  getModules: (slug) => api.get(`/api/v1/tenant/${slug}/modules`),
  updateModules: (slug, allowedModuleIds) => api.put(`/api/v1/tenant/${slug}/modules`, { allowedModuleIds }),
  getAdmins: (slug) => api.get(`/api/v1/tenant/${slug}/admins`),
  addAdmin: (slug, userId, role) => api.post(`/api/v1/tenant/${slug}/admins`, { userId, role }),
  removeAdmin: (slug, userId) => api.delete(`/api/v1/tenant/${slug}/admins/${userId}`),
};

// ── Community Roles API ────────────────────────────────────────────
export const rolesApi = {
  list: (communityId) => api.get(`/api/v1/admin/${communityId}/interaction/roles`),
  create: (communityId, data) => api.post(`/api/v1/admin/${communityId}/interaction/roles`, data),
  update: (communityId, roleId, data) => api.put(`/api/v1/admin/${communityId}/interaction/roles/${roleId}`, data),
  delete: (communityId, roleId) => api.delete(`/api/v1/admin/${communityId}/interaction/roles/${roleId}`),
};

// ── Channel Permissions API ────────────────────────────────────────
export const channelPermissionsApi = {
  getOverrides: (communityId, channelId) => api.get(`/api/v1/admin/${communityId}/interaction/channels/${channelId}/permissions`),
  updateOverrides: (communityId, channelId, overrides) => api.put(`/api/v1/admin/${communityId}/interaction/channels/${channelId}/permissions`, { overrides }),
};

// ── Superadmin Tenant API ──────────────────────────────────────────
export const superadminTenantApi = {
  list: (params) => api.get('/api/v1/superadmin/tenants', { params }),
  create: (data) => api.post('/api/v1/superadmin/tenants', data),
  update: (id, data) => api.put(`/api/v1/superadmin/tenants/${id}`, data),
  delete: (id) => api.delete(`/api/v1/superadmin/tenants/${id}`),
};

// ── Bundle Onboarding API (hub_api/blueprints/v1/bundle_versions.py +
// bundle_approvals.py) ─────────────────────────────────────────────
// `app_id` is a URL path segment, not a query value -- encoded so a
// namespace like `waddles.integrations.vendor-42.mybundle` round-trips
// safely even though it already only contains URL-safe characters.
export const bundleApi = {
  // multipart: `manifest` (YAML, required) + `component` (prebuilt WASM,
  // vendors) OR `source` (tarball, platform:admin only). `source` is
  // deliberately never sent by vendor-facing UI -- vendor uploads are
  // 400-refused server-side (services/vendor_bundle_authz.py).
  createVersion: (appId, formData) =>
    api.post(`/api/v1/apps/${encodeURIComponent(appId)}/versions`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),
  listVersions: (appId) => api.get(`/api/v1/apps/${encodeURIComponent(appId)}/versions`),
  getVersion: (appId, version) =>
    api.get(`/api/v1/apps/${encodeURIComponent(appId)}/versions/${encodeURIComponent(version)}`),
  getPermissions: (appId, version) =>
    api.get(
      `/api/v1/apps/${encodeURIComponent(appId)}/versions/${encodeURIComponent(version)}/permissions`,
    ),
  approveVersion: (appId, version, data) =>
    api.post(
      `/api/v1/apps/${encodeURIComponent(appId)}/versions/${encodeURIComponent(version)}/approve`,
      data,
    ),
  denyVersion: (appId, version, data) =>
    api.post(
      `/api/v1/apps/${encodeURIComponent(appId)}/versions/${encodeURIComponent(version)}/deny`,
      data,
    ),
};

// ── Global-Admin Bundle Approval Queue (hub_api/blueprints/v1/bundle_admin.py) ──
export const bundleAdminApi = {
  listPendingVersions: (params) => api.get('/api/v1/admin/bundle-versions', { params }),
};
