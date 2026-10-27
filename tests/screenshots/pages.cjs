'use strict';
/**
 * Page inventory for the marketing screenshot run. `publicPages` are captured
 * in a logged-out browser context; `authPages` after login. Each `name` becomes
 * docs/screenshots/<name>.png.
 */

/**
 * Build the page lists for a given seeded community and tenant.
 * @param {{communityId: string|number, tenantSlug: string}} opts
 * @returns {{publicPages: object[], authPages: object[]}}
 */
function buildPages({ communityId, tenantSlug }) {
  const publicPages = [
  { name: 'home', path: '/' },
  { name: 'login', path: '/login' },
  { name: 'communities', path: '/communities' },
  { name: 'live-streams', path: '/live' },
  { name: 'cookie-policy', path: '/cookie-policy' },
  ];
  const authPages = [
  // ── Dashboard ──────────────────────────────────────────────────────
  { name: 'dashboard', path: '/dashboard' },
  { name: 'dashboard-settings', path: '/dashboard/settings' },
  { name: 'dashboard-profile', path: '/dashboard/profile' },
  { name: 'dashboard-my-channels', path: '/dashboard/my-channels' },
  { name: 'dashboard-personal-access-token', path: '/account/tokens' },
  { name: 'communities-create', path: '/communities/create' },
  // ── Calendar / Booking ─────────────────────────────────────────────
  { name: 'calendar-settings', path: '/calendar/settings' },
  { name: 'calendar-booking-pages', path: '/calendar/booking-pages' },
  { name: 'calendar-my-bookings', path: '/calendar/my-bookings' },
  // ── Vendor ─────────────────────────────────────────────────────────
  { name: 'vendor-submit', path: '/vendor/submit' },
  { name: 'vendor-submission-status', path: '/vendor/submission-status' },
  { name: 'vendor-dashboard', path: '/vendor/dashboard' },
  { name: 'vendor-request', path: '/vendor/request' },
  // ── Community Member ───────────────────────────────────────────────
  { name: 'community-dashboard', path: `/dashboard/community/${communityId}` },
  { name: 'community-settings', path: `/dashboard/community/${communityId}/settings` },
  { name: 'community-chat', path: `/dashboard/community/${communityId}/chat` },
  { name: 'community-leaderboard', path: `/dashboard/community/${communityId}/leaderboard` },
  { name: 'community-members', path: `/dashboard/community/${communityId}/members` },
  { name: 'community-support-submit', path: `/community/${communityId}/support/submit` },
  { name: 'community-support-my-tickets', path: `/community/${communityId}/support/my-tickets` },
  { name: 'community-game-servers', path: `/community/${communityId}/game-servers` },
  { name: 'community-interaction', path: `/community/${communityId}/interact` },
  { name: 'community-inventory-browse', path: `/community/${communityId}/inventory` },
  { name: 'community-inventory-my-items', path: `/community/${communityId}/inventory/my-items` },
  // ── Admin Core ─────────────────────────────────────────────────────
  { name: 'admin-overview', path: `/admin/${communityId}` },
  { name: 'admin-members', path: `/admin/${communityId}/members` },
  { name: 'admin-modules', path: `/admin/${communityId}/modules` },
  { name: 'admin-stream-overlays', path: `/admin/${communityId}/stream-overlays` },
  { name: 'admin-domains', path: `/admin/${communityId}/domains` },
  { name: 'admin-servers', path: `/admin/${communityId}/servers` },
  { name: 'admin-connected-platforms', path: `/admin/${communityId}/connected-platforms` },
  { name: 'admin-mirror-groups', path: `/admin/${communityId}/mirror-groups` },
  { name: 'admin-leaderboard-config', path: `/admin/${communityId}/leaderboard` },
  { name: 'admin-community-profile', path: `/admin/${communityId}/profile` },
  { name: 'admin-reputation', path: `/admin/${communityId}/reputation` },
  { name: 'admin-announcements', path: `/admin/${communityId}/announcements` },
  { name: 'admin-analytics', path: `/admin/${communityId}/analytics` },
  { name: 'admin-security', path: `/admin/${communityId}/security` },
  { name: 'admin-roles', path: `/admin/${communityId}/roles` },
  { name: 'admin-platform-settings', path: `/admin/${communityId}/platform-settings` },
  // ── Admin Content & Engagement ─────────────────────────────────────
  { name: 'admin-shoutouts', path: `/admin/${communityId}/shoutouts` },
  { name: 'admin-translation', path: `/admin/${communityId}/translation` },
  { name: 'admin-live-streaming', path: `/admin/${communityId}/live-streaming` },
  { name: 'admin-calls', path: `/admin/${communityId}/calls` },
  { name: 'admin-polls', path: `/admin/${communityId}/polls` },
  { name: 'admin-forms', path: `/admin/${communityId}/forms` },
  { name: 'admin-commands', path: `/admin/${communityId}/commands` },
  // ── Admin AI ───────────────────────────────────────────────────────
  { name: 'admin-ai-insights', path: `/admin/${communityId}/ai-insights` },
  { name: 'admin-ai-config', path: `/admin/${communityId}/ai-config` },
  // ── Admin Module Configs ───────────────────────────────────────────
  { name: 'admin-module-lfg-config', path: `/admin/${communityId}/modules/lfg/config` },
  { name: 'admin-module-clip-config', path: `/admin/${communityId}/modules/clip/config` },
  { name: 'admin-module-alias-config', path: `/admin/${communityId}/modules/alias/config` },
  { name: 'admin-module-memories-config', path: `/admin/${communityId}/modules/memories/config` },
  { name: 'admin-module-server-status-config', path: `/admin/${communityId}/modules/server-status/config` },
  { name: 'admin-module-server-manager-config', path: `/admin/${communityId}/modules/server-manager/config` },
  // ── Admin Loyalty ──────────────────────────────────────────────────
  { name: 'admin-loyalty', path: `/admin/${communityId}/loyalty` },
  { name: 'admin-loyalty-leaderboard', path: `/admin/${communityId}/loyalty/leaderboard` },
  { name: 'admin-loyalty-giveaways', path: `/admin/${communityId}/loyalty/giveaways` },
  { name: 'admin-loyalty-games', path: `/admin/${communityId}/loyalty/games` },
  { name: 'admin-loyalty-gear', path: `/admin/${communityId}/loyalty/gear` },
  // ── Admin Music ────────────────────────────────────────────────────
  { name: 'admin-music', path: `/admin/${communityId}/music` },
  { name: 'admin-music-settings', path: `/admin/${communityId}/music/settings` },
  { name: 'admin-music-providers', path: `/admin/${communityId}/music/providers` },
  { name: 'admin-music-radio', path: `/admin/${communityId}/music/radio` },
  // ── Admin Calendar ─────────────────────────────────────────────────
  { name: 'admin-calendar-events', path: `/admin/${communityId}/calendar/events` },
  // ── Admin Support & Operations ─────────────────────────────────────
  { name: 'admin-support', path: `/admin/${communityId}/support` },
  { name: 'admin-join-requests', path: `/admin/${communityId}/join-requests` },
  { name: 'admin-inventory', path: `/admin/${communityId}/inventory` },
  { name: 'admin-tokens', path: `/admin/${communityId}/tokens` },
  { name: 'admin-interaction-channels', path: `/admin/${communityId}/interaction-channels` },
  { name: 'admin-rcon', path: `/admin/${communityId}/rcon` },
  // ── Admin Premium-Only ─────────────────────────────────────────────
  { name: 'admin-bot-detection', path: `/admin/${communityId}/bot-detection` },
  { name: 'admin-workflows', path: `/admin/${communityId}/workflows` },
  // ── Platform Admin ─────────────────────────────────────────────────
  { name: 'platform-dashboard', path: '/platform' },
  { name: 'platform-users', path: '/platform/users' },
  { name: 'platform-communities', path: '/platform/communities' },
  // ── Super Admin ────────────────────────────────────────────────────
  { name: 'superadmin-dashboard', path: '/superadmin' },
  { name: 'superadmin-communities', path: '/superadmin/communities' },
  { name: 'superadmin-create-community', path: '/superadmin/communities/new' },
  { name: 'superadmin-modules', path: '/superadmin/modules' },
  { name: 'superadmin-vendor-submissions', path: '/superadmin/vendor-submissions' },
  { name: 'superadmin-vendor-requests', path: '/superadmin/vendor-requests' },
  { name: 'superadmin-users', path: '/superadmin/users' },
  { name: 'superadmin-platform-config', path: '/superadmin/platform-config' },
  { name: 'superadmin-kong', path: '/superadmin/kong' },
  { name: 'superadmin-software-discovery', path: '/superadmin/software-discovery' },
  { name: 'superadmin-services', path: '/superadmin/services' },
  { name: 'superadmin-analytics', path: '/superadmin/analytics' },
  { name: 'superadmin-tenants', path: '/superadmin/tenants' },
  // ── Tenant Admin ───────────────────────────────────────────────────
  { name: 'tenant-dashboard', path: `/tenant/${tenantSlug}` },
  { name: 'tenant-modules', path: `/tenant/${tenantSlug}/modules` },
  { name: 'tenant-admins', path: `/tenant/${tenantSlug}/admins` },
  { name: 'tenant-communities', path: `/tenant/${tenantSlug}/communities` },
  ];
  return { publicPages, authPages };
}

module.exports = { buildPages };
