import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate, useParams } from 'react-router-dom';
import { useAuth } from './contexts/AuthContext';

// Wave-1 route stubs (S0 foundation) -- lazy so later slices only need to
// fill in the component file; the import line and <Route> below are
// pre-registered to avoid App.jsx merge conflicts between parallel slices.
const TenantBundleCatalog = lazy(() => import('./pages/tenant/TenantBundleCatalog'));
const AdminCommunityBundles = lazy(() => import('./pages/admin/AdminCommunityBundles'));
const SuperAdminSuperCommunities = lazy(() => import('./pages/superadmin/SuperAdminSuperCommunities'));
const AdminRoleSyncMapping = lazy(() => import('./pages/admin/AdminRoleSyncMapping'));

// Layouts
import PublicLayout from './layouts/PublicLayout';
import DashboardLayout from './layouts/DashboardLayout';
import AdminLayout from './layouts/AdminLayout';

// Public pages
import HomePage from './pages/public/HomePage';
import CommunitiesPage from './pages/public/CommunitiesPage';
import CommunityPublicPage from './pages/public/CommunityPublicPage';
import LiveStreamsPage from './pages/public/LiveStreamsPage';
import UserPublicProfile from './pages/public/UserPublicProfile';
import MusicQueuePage from './pages/public/MusicQueuePage';

// Auth pages
import LoginPage from './pages/auth/LoginPage';
import RegisterPage from './pages/auth/RegisterPage';
import PasswordRecoveryUnavailablePage from './pages/auth/PasswordRecoveryUnavailablePage';
import OAuthCallback from './pages/auth/OAuthCallback';

// Cookie Policy page
import CookiePolicy from './pages/CookiePolicy';
import PrivacyRights from './pages/PrivacyRights';

// Calendar pages
import CalendarSettings from './pages/calendar/CalendarSettings';
import BookingPages from './pages/calendar/BookingPages';
import MyBookings from './pages/calendar/MyBookings';
import BookingPagePublic from './pages/calendar/BookingPagePublic';

// Dashboard pages
import DashboardHome from './pages/dashboard/DashboardHome';
import CommunityDashboard from './pages/dashboard/CommunityDashboard';
import CommunitySettings from './pages/dashboard/CommunitySettings';
import CommunityChat from './pages/dashboard/CommunityChat';
import CommunityLeaderboard from './pages/dashboard/CommunityLeaderboard';
import CommunityMembers from './pages/dashboard/CommunityMembers';
import AccountSettings from './pages/dashboard/AccountSettings';
import UserProfileEdit from './pages/dashboard/UserProfileEdit';
import CreateCommunity from './pages/dashboard/CreateCommunity';
import MyAnalytics from './pages/dashboard/MyAnalytics';

// Admin pages
import AdminHome from './pages/admin/AdminHome';
import AdminMembers from './pages/admin/AdminMembers';
import AdminModules from './pages/admin/AdminModules';
import AdminMarketplace from './pages/admin/AdminMarketplace';
import AdminMarketplaceModuleDetail from './pages/admin/AdminMarketplaceModuleDetail.jsx';
import AdminCommunityPremium from './pages/admin/AdminCommunityPremium.jsx';
import SuperAdminMarketplaceSettings from './pages/superadmin/SuperAdminMarketplaceSettings.jsx';
import AdminStreamOverlays from './pages/admin/AdminStreamOverlays';
import AdminDomains from './pages/admin/AdminDomains';
import AdminServers from './pages/admin/AdminServers';
import AdminConnectedPlatforms from './pages/admin/AdminConnectedPlatforms';
import LiveActivity from './pages/community/LiveActivity';
import AdminMirrorGroups from './pages/admin/AdminMirrorGroups';
import AdminLeaderboardConfig from './pages/admin/AdminLeaderboardConfig';
import AdminCommunityProfile from './pages/admin/AdminCommunityProfile';
import ReputationSettings from './pages/admin/ReputationSettings';
import AdminAIInsights from './pages/admin/AdminAIInsights';
import AdminAIResearcherConfig from './pages/admin/AdminAIResearcherConfig';
import AdminAIChatterConfig from './pages/admin/AdminAIChatterConfig';
import AdminBotDetection from './pages/admin/AdminBotDetection';
import AdminAnnouncements from './pages/admin/AdminAnnouncements';
import AdminAnalytics from './pages/admin/AdminAnalytics';
import AdminMemberAnalytics from './pages/admin/AdminMemberAnalytics';
import AdminSecurity from './pages/admin/AdminSecurity';
import LoyaltySettings from './pages/admin/LoyaltySettings';
import LoyaltyLeaderboard from './pages/admin/LoyaltyLeaderboard';
import LoyaltyGiveaways from './pages/admin/LoyaltyGiveaways';
import LoyaltyGames from './pages/admin/LoyaltyGames';
import LoyaltyGear from './pages/admin/LoyaltyGear';
import CommunityConnections from './pages/admin/CommunityConnections';
import AdminWorkflows from './pages/admin/AdminWorkflows';
import AdminShoutouts from './pages/admin/AdminShoutouts';
import AdminTranslation from './pages/admin/AdminTranslation';
import AdminMusicDashboard from './pages/admin/AdminMusicDashboard';
import AdminMusicSettings from './pages/admin/AdminMusicSettings';
import AdminMusicProviders from './pages/admin/AdminMusicProviders';
import AdminRadioStations from './pages/admin/AdminRadioStations';
import AdminVendorReview from './pages/admin/AdminVendorReview';
import AdminCalendarEvents from './pages/admin/AdminCalendarEvents';
import AdminCalendarTicketing from './pages/admin/AdminCalendarTicketing';
import AdminCalendarScanner from './pages/admin/AdminCalendarScanner';
import AdminCalendarAttendance from './pages/admin/AdminCalendarAttendance';
import AdminLiveStreams from './pages/admin/AdminLiveStreams';
import AdminCommunityCalls from './pages/admin/AdminCommunityCalls';
import AdminPolls from './pages/admin/AdminPolls';
import AdminForms from './pages/admin/AdminForms';
import AdminSupportDashboard from './pages/admin/AdminSupportDashboard';
import AdminSupportTicketDetail from './pages/admin/AdminSupportTicketDetail';
import AdminJoinRequests from './pages/admin/AdminJoinRequests';
import AdminInventory from './pages/admin/AdminInventory';
import AdminRconServers from './pages/admin/AdminRconServers';
import AdminCommunityTokens from './pages/admin/AdminCommunityTokens';
import AdminCommands from './pages/admin/AdminCommands';
import AdminPlatformSettings from './pages/admin/AdminPlatformSettings';
import AdminLfgConfig from './pages/admin/AdminLfgConfig';
import AdminClipConfig from './pages/admin/AdminClipConfig';
import AdminAliasConfig from './pages/admin/AdminAliasConfig';
import AdminMemoriesConfig from './pages/admin/AdminMemoriesConfig';
import AdminServerStatusConfig from './pages/admin/AdminServerStatusConfig';
import AdminServerManagerConfig from './pages/admin/AdminServerManagerConfig';
import MyChannels from './pages/dashboard/MyChannels';
import SupportSubmitTicket from './pages/community/SupportSubmitTicket';
import SupportMyTickets from './pages/community/SupportMyTickets';
import InventoryBrowse from './pages/community/InventoryBrowse';
import GameServers from './pages/community/GameServers';
import InventoryMyItems from './pages/community/InventoryMyItems';
import PersonalAccessToken from './pages/dashboard/PersonalAccessToken';
import CommunityInteraction from './pages/community/CommunityInteraction';
import AdminInteractionChannels from './pages/admin/AdminInteractionChannels';

// Vendor pages
import VendorSubmissionForm from './pages/vendor/VendorSubmissionForm';
import VendorSubmissionStatus from './pages/vendor/VendorSubmissionStatus';
import VendorDashboard from './pages/vendor/VendorDashboard';
import VendorSubmissions from './pages/vendor/VendorSubmissions';
import VendorRequest from './pages/vendor/VendorRequest';
import VendorModules from './pages/vendor/VendorModules';
import VendorAnalytics from './pages/vendor/VendorAnalytics';
import VendorDiscountCodes from './pages/vendor/VendorDiscountCodes';
import VendorSettings from './pages/vendor/VendorSettings';
import VendorGithubSync from './pages/vendor/VendorGithubSync';

// Platform admin pages
import PlatformDashboard from './pages/platform/PlatformDashboard';
import PlatformUsers from './pages/platform/PlatformUsers';
import PlatformCommunities from './pages/platform/PlatformCommunities';
import PlatformAnalytics from './pages/platform/PlatformAnalytics';

// Super admin pages
import SuperAdminDashboard from './pages/superadmin/SuperAdminDashboard';
import SuperAdminCommunities from './pages/superadmin/SuperAdminCommunities';
import SuperAdminModuleRegistry from './pages/superadmin/SuperAdminModuleRegistry';
import SuperAdminBundleApprovals from './pages/superadmin/SuperAdminBundleApprovals';
import SuperAdminPlatformConfig from './pages/superadmin/SuperAdminPlatformConfig';
import SuperAdminSoftwareDiscovery from './pages/superadmin/SuperAdminSoftwareDiscovery';
import SuperAdminServiceDiscovery from './pages/superadmin/SuperAdminServiceDiscovery';
import SuperAdminVendorRequests from './pages/superadmin/SuperAdminVendorRequests';
import SuperAdminUsers from './pages/superadmin/SuperAdminUsers';
import SuperAdminAnalytics from './pages/superadmin/SuperAdminAnalytics';
import SuperAdminTenants from './pages/superadmin/SuperAdminTenants';

// Tenant admin pages
import TenantDashboard from './pages/tenant/TenantDashboard';
import TenantModules from './pages/tenant/TenantModules';
import TenantAdmins from './pages/tenant/TenantAdmins';
import TenantCommunities from './pages/tenant/TenantCommunities';

// Admin pages (new)
import AdminCommunityRoles from './pages/admin/AdminCommunityRoles';
import AdminRaffleCustomization from './pages/admin/AdminRaffleCustomization';
import AdminAIKnowledge from './pages/admin/AdminAIKnowledge';

// Marketplace redirect helper
function MarketplaceRedirect() {
  const { communityId } = useParams();
  return <Navigate to={`/admin/${communityId}/modules`} replace />;
}

// Loading spinner
function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center min-h-screen">
      <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-600"></div>
    </div>
  );
}

// Protected route wrapper
function ProtectedRoute({ children, requireCommunityAdmin = false, requirePlatformAdmin = false, requireSuperAdmin = false, requireAnalyticsConsumerOrSuperAdmin = false }) {
  const { user, loading, hasRole, isAnalyticsConsumer } = useAuth();

  if (loading) {
    return <LoadingSpinner />;
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  if (requireCommunityAdmin) {
    // Super admins always have access; community-level admins checked per-community
    const isSuperAdmin = hasRole('admin') || hasRole('super_admin');
    const communityAdminRoles = ['community-owner', 'community-admin', 'moderator'];
    const hasCommunityAdmin = user.communities?.some(c => communityAdminRoles.includes(c.role));
    if (!isSuperAdmin && !hasCommunityAdmin) {
      return <Navigate to="/dashboard" replace />;
    }
  }

  if (requirePlatformAdmin && !hasRole('platform-admin')) {
    return <Navigate to="/dashboard" replace />;
  }

  if (requireSuperAdmin && !hasRole('super_admin')) {
    return <Navigate to="/dashboard" replace />;
  }

  if (requireAnalyticsConsumerOrSuperAdmin && !isAnalyticsConsumer && !hasRole('super_admin')) {
    return <Navigate to="/dashboard" replace />;
  }

  return children;
}

function App() {
  return (
    <Routes>
      {/* Public routes */}
      <Route element={<PublicLayout />}>
        <Route path="/" element={<HomePage />} />
        <Route path="/communities" element={<CommunitiesPage />} />
        <Route path="/communities/:id" element={<CommunityPublicPage />} />
        <Route path="/live" element={<LiveStreamsPage />} />
        <Route path="/users/:userId" element={<UserPublicProfile />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/login/:tenantSlug" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/forgot-password" element={<PasswordRecoveryUnavailablePage />} />
        <Route path="/auth/callback" element={<OAuthCallback />} />
        <Route path="/cookie-policy" element={<CookiePolicy />} />
        <Route path="/privacy-rights" element={<PrivacyRights />} />

        {/* Public booking page (no auth required) */}
        <Route path="/book/:slug" element={<BookingPagePublic />} />

        {/* Public song-queue page (no auth required) -- linked from chat via !sq */}
        <Route path="/c/:communityId/music/queue" element={<MusicQueuePage />} />

        {/* Vendor submission routes (public) */}
        <Route path="/vendor/submit" element={<VendorSubmissionForm />} />
        <Route path="/vendor/submission-status" element={<VendorSubmissionStatus />} />
      </Route>

      {/* Dashboard routes (authenticated) */}
      <Route
        element={
          <ProtectedRoute>
            <DashboardLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/dashboard" element={<DashboardHome />} />
        <Route path="/dashboard/settings" element={<AccountSettings />} />
        <Route path="/dashboard/profile" element={<UserProfileEdit />} />
        <Route path="/communities/create" element={<CreateCommunity />} />
        <Route path="/dashboard/community/:id" element={<CommunityDashboard />} />
        <Route path="/dashboard/community/:id/settings" element={<CommunitySettings />} />
        <Route path="/dashboard/community/:id/chat" element={<CommunityChat />} />
        <Route path="/dashboard/community/:id/leaderboard" element={<CommunityLeaderboard />} />
        <Route path="/dashboard/community/:id/members" element={<CommunityMembers />} />

        {/* Support ticket routes (authenticated) */}
        <Route path="/community/:communityId/support/submit" element={<SupportSubmitTicket />} />
        <Route path="/community/:communityId/support/my-tickets" element={<SupportMyTickets />} />

        {/* Inventory (Quartermaster) routes (authenticated) */}
        <Route path="/community/:communityId/inventory" element={<InventoryBrowse />} />
        <Route path="/community/:communityId/inventory/my-items" element={<InventoryMyItems />} />
        <Route path="/community/:communityId/game-servers" element={<GameServers />} />
        <Route path="/community/:communityId/interact" element={<CommunityInteraction />} />
        <Route path="/community/:communityId/interact/:channelId" element={<CommunityInteraction />} />

        {/* Personal Access Token */}
        <Route path="/account/tokens" element={<PersonalAccessToken />} />
        {/* My Channels */}
        <Route path="/dashboard/my-channels" element={<MyChannels />} />
        {/* My Analytics */}
        <Route path="/dashboard/my-analytics" element={<MyAnalytics />} />

        {/* Calendar routes (authenticated) */}
        <Route path="/calendar/settings" element={<CalendarSettings />} />
        <Route path="/calendar/booking-pages" element={<BookingPages />} />
        <Route path="/calendar/my-bookings" element={<MyBookings />} />

        {/* Vendor dashboard routes (authenticated vendors) */}
        <Route path="/vendor/dashboard" element={<VendorDashboard />} />
        <Route path="/vendor/submissions" element={<VendorSubmissions />} />
        <Route path="/vendor/request" element={<VendorRequest />} />
        <Route path="/vendor/modules" element={<VendorModules />} />
        <Route path="/vendor/analytics" element={<VendorAnalytics />} />
        <Route path="/vendor/discount-codes" element={<VendorDiscountCodes />} />
        <Route path="/vendor/settings" element={<VendorSettings />} />
        <Route path="/vendor/github-sync" element={<VendorGithubSync />} />
      </Route>

      {/* Admin routes (community admin) */}
      <Route
        element={
          <ProtectedRoute requireCommunityAdmin>
            <AdminLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/admin/:communityId" element={<AdminHome />} />
        <Route path="/admin/:communityId/live-activity" element={<LiveActivity />} />
        <Route path="/admin/:communityId/members" element={<AdminMembers />} />
        <Route path="/admin/:communityId/workflows" element={<AdminWorkflows />} />
        <Route path="/admin/:communityId/modules" element={<AdminModules />} />
        <Route path="/admin/:communityId/modules/lfg/config" element={<AdminLfgConfig />} />
        <Route path="/admin/:communityId/modules/clip/config" element={<AdminClipConfig />} />
        <Route path="/admin/:communityId/modules/alias/config" element={<AdminAliasConfig />} />
        <Route path="/admin/:communityId/modules/memories/config" element={<AdminMemoriesConfig />} />
        <Route path="/admin/:communityId/modules/server-status/config" element={<AdminServerStatusConfig />} />
        <Route path="/admin/:communityId/modules/server-manager/config" element={<AdminServerManagerConfig />} />
        <Route path="/admin/:communityId/marketplace" element={<MarketplaceRedirect />} />
        <Route path="/admin/:communityId/marketplace/:source/:id" element={<AdminMarketplaceModuleDetail />} />
        <Route path="/admin/:communityId/premium" element={<AdminCommunityPremium />} />
        <Route path="/admin/:communityId/stream-overlays" element={<AdminStreamOverlays />} />
        <Route path="/admin/:communityId/domains" element={<AdminDomains />} />
        <Route path="/admin/:communityId/servers" element={<AdminServers />} />
        <Route path="/admin/:communityId/connected-platforms" element={<AdminConnectedPlatforms />} />
        <Route path="/admin/:communityId/mirror-groups" element={<AdminMirrorGroups />} />
        <Route path="/admin/:communityId/leaderboard" element={<AdminLeaderboardConfig />} />
        <Route path="/admin/:communityId/profile" element={<AdminCommunityProfile />} />
        <Route path="/admin/:communityId/reputation" element={<ReputationSettings />} />
        <Route path="/admin/:communityId/ai-insights" element={<AdminAIInsights />} />
        <Route path="/admin/:communityId/ai-config" element={<AdminAIResearcherConfig />} />
        <Route path="/admin/:communityId/ai-chatter" element={<AdminAIChatterConfig />} />
        <Route path="/admin/:communityId/bot-detection" element={<AdminBotDetection />} />
        <Route path="/admin/:communityId/announcements" element={<AdminAnnouncements />} />
        <Route path="/admin/:communityId/analytics" element={<AdminAnalytics />} />
        <Route path="/admin/:communityId/security" element={<AdminSecurity />} />
        <Route path="/admin/:communityId/loyalty" element={<LoyaltySettings />} />
        <Route path="/admin/:communityId/loyalty/leaderboard" element={<LoyaltyLeaderboard />} />
        <Route path="/admin/:communityId/loyalty/giveaways" element={<LoyaltyGiveaways />} />
        <Route path="/admin/:communityId/loyalty/games" element={<LoyaltyGames />} />
        <Route path="/admin/:communityId/loyalty/gear" element={<LoyaltyGear />} />
        <Route path="/admin/:communityId/connections" element={<CommunityConnections />} />
        <Route path="/admin/:communityId/raffle-customization" element={<AdminRaffleCustomization />} />
        <Route path="/admin/:communityId/shoutouts" element={<AdminShoutouts />} />
        <Route path="/admin/:communityId/translation" element={<AdminTranslation />} />
        <Route path="/admin/:communityId/music" element={<AdminMusicDashboard />} />
        <Route path="/admin/:communityId/music/settings" element={<AdminMusicSettings />} />
        <Route path="/admin/:communityId/music/providers" element={<AdminMusicProviders />} />
        <Route path="/admin/:communityId/music/radio" element={<AdminRadioStations />} />
        <Route path="/admin/:communityId/calendar/events" element={<AdminCalendarEvents />} />
        <Route path="/admin/:communityId/calendar/events/:eventId/tickets" element={<AdminCalendarTicketing />} />
        <Route path="/admin/:communityId/calendar/events/:eventId/scanner" element={<AdminCalendarScanner />} />
        <Route path="/admin/:communityId/calendar/events/:eventId/attendance" element={<AdminCalendarAttendance />} />
        <Route path="/admin/:communityId/live-streaming" element={<AdminLiveStreams />} />
        <Route path="/admin/:communityId/calls" element={<AdminCommunityCalls />} />
        <Route path="/admin/:communityId/polls" element={<AdminPolls />} />
        <Route path="/admin/:communityId/forms" element={<AdminForms />} />
        <Route path="/admin/:communityId/support" element={<AdminSupportDashboard />} />
        <Route path="/admin/:communityId/support/tickets/:ticketId" element={<AdminSupportTicketDetail />} />
        <Route path="/admin/:communityId/join-requests" element={<AdminJoinRequests />} />
        <Route path="/admin/:communityId/inventory" element={<AdminInventory />} />
        <Route path="/admin/:communityId/rcon" element={<AdminRconServers />} />
        <Route path="/admin/:communityId/tokens" element={<AdminCommunityTokens />} />
        <Route path="/admin/:communityId/commands" element={<AdminCommands />} />
        <Route path="/admin/:communityId/platform-settings" element={<AdminPlatformSettings />} />
        <Route path="/admin/:communityId/interaction-channels" element={<AdminInteractionChannels />} />
        <Route path="/admin/:communityId/roles" element={<AdminCommunityRoles />} />
        <Route path="/admin/:communityId/members/:userId/analytics" element={<AdminMemberAnalytics />} />
        <Route path="/admin/:communityId/ai-knowledge" element={<AdminAIKnowledge />} />
        {/* Wave-1 stub: community bundles slice */}
        <Route
          path="/admin/:communityId/bundles"
          element={
            <Suspense fallback={<LoadingSpinner />}>
              <AdminCommunityBundles />
            </Suspense>
          }
        />
        <Route
          path="/admin/:communityId/role-sync"
          element={
            <Suspense fallback={<LoadingSpinner />}>
              <AdminRoleSyncMapping />
            </Suspense>
          }
        />
      </Route>

      {/* Platform admin routes */}
      <Route
        element={
          <ProtectedRoute requirePlatformAdmin>
            <AdminLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/platform" element={<PlatformDashboard />} />
        <Route path="/platform/users" element={<PlatformUsers />} />
        <Route path="/platform/communities" element={<PlatformCommunities />} />
      </Route>

      {/* Platform Analytics — analyticsConsumer or superAdmin */}
      <Route
        element={
          <ProtectedRoute requireAnalyticsConsumerOrSuperAdmin>
            <DashboardLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/platform/analytics" element={<PlatformAnalytics />} />
      </Route>

      {/* Super admin routes - uses DashboardLayout with sidebar admin section */}
      <Route
        element={
          <ProtectedRoute requireSuperAdmin>
            <DashboardLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/superadmin" element={<SuperAdminDashboard />} />
        <Route path="/superadmin/communities" element={<SuperAdminCommunities />} />
        <Route path="/superadmin/modules" element={<SuperAdminModuleRegistry />} />
        <Route path="/superadmin/bundle-approvals" element={<SuperAdminBundleApprovals />} />
        <Route path="/superadmin/vendor-submissions" element={<AdminVendorReview />} />
        <Route path="/superadmin/vendor-submissions/:submissionId" element={<AdminVendorReview />} />
        <Route path="/superadmin/vendor-requests" element={<SuperAdminVendorRequests />} />
        <Route path="/superadmin/users" element={<SuperAdminUsers />} />
        <Route path="/superadmin/platform-config" element={<SuperAdminPlatformConfig />} />
        <Route path="/superadmin/software-discovery" element={<SuperAdminSoftwareDiscovery />} />
        <Route path="/superadmin/services" element={<SuperAdminServiceDiscovery />} />
        <Route path="/superadmin/analytics" element={<SuperAdminAnalytics />} />
        <Route path="/superadmin/tenants" element={<SuperAdminTenants />} />
        <Route path="/superadmin/marketplace-settings" element={<SuperAdminMarketplaceSettings />} />
        {/* Wave-1 stub: super communities (1:N model) slice */}
        <Route
          path="/superadmin/super-communities"
          element={
            <Suspense fallback={<LoadingSpinner />}>
              <SuperAdminSuperCommunities />
            </Suspense>
          }
        />
      </Route>

      {/* Tenant admin routes */}
      <Route
        element={
          <ProtectedRoute>
            <DashboardLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/tenant/:tenantSlug" element={<TenantDashboard />} />
        <Route path="/tenant/:tenantSlug/modules" element={<TenantModules />} />
        <Route path="/tenant/:tenantSlug/admins" element={<TenantAdmins />} />
        <Route path="/tenant/:tenantSlug/communities" element={<TenantCommunities />} />
        {/* Wave-1 stub: tenant bundle catalog slice */}
        <Route
          path="/tenant/:tenantSlug/bundles"
          element={
            <Suspense fallback={<LoadingSpinner />}>
              <TenantBundleCatalog />
            </Suspense>
          }
        />
      </Route>

      {/* Catch all - redirect to home */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default App;
