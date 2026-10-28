import { lazy } from 'react';
import { Routes, Route, Navigate, useParams } from 'react-router-dom';
import { useAuth } from './contexts/AuthContext';
import { LoadingState, ServedFeature } from './components/ui';

// Every page is route-level code-split: the shell (layouts) ships in the main
// bundle, each page loads on demand behind the layout's <RouteOutlet />.
const TenantBundleCatalog = lazy(() => import('./pages/tenant/TenantBundleCatalog'));
const AdminCommunityBundles = lazy(() => import('./pages/admin/AdminCommunityBundles'));
const SuperAdminSuperCommunities = lazy(() => import('./pages/superadmin/SuperAdminSuperCommunities'));
const SecretPullPage = lazy(() => import('./pages/secret/SecretPullPage'));
const AdminRoleSyncMapping = lazy(() => import('./pages/admin/AdminRoleSyncMapping'));

// Layouts
import PublicLayout from './layouts/PublicLayout';
import DashboardLayout from './layouts/DashboardLayout';
import AdminLayout from './layouts/AdminLayout';

// Public pages
const HomePage = lazy(() => import('./pages/public/HomePage'));
const CommunitiesPage = lazy(() => import('./pages/public/CommunitiesPage'));
const CommunityPublicPage = lazy(() => import('./pages/public/CommunityPublicPage'));
const LiveStreamsPage = lazy(() => import('./pages/public/LiveStreamsPage'));
const UserPublicProfile = lazy(() => import('./pages/public/UserPublicProfile'));
const MusicQueuePage = lazy(() => import('./pages/public/MusicQueuePage'));

// Auth pages
const LoginPage = lazy(() => import('./pages/auth/LoginPage'));
const RegisterPage = lazy(() => import('./pages/auth/RegisterPage'));
const PasswordRecoveryUnavailablePage = lazy(() => import('./pages/auth/PasswordRecoveryUnavailablePage'));
const OAuthCallback = lazy(() => import('./pages/auth/OAuthCallback'));

// Cookie Policy page
const CookiePolicy = lazy(() => import('./pages/CookiePolicy'));
const PrivacyRights = lazy(() => import('./pages/PrivacyRights'));

// Calendar pages
const CalendarSettings = lazy(() => import('./pages/calendar/CalendarSettings'));
const BookingPages = lazy(() => import('./pages/calendar/BookingPages'));
const MyBookings = lazy(() => import('./pages/calendar/MyBookings'));
const BookingPagePublic = lazy(() => import('./pages/calendar/BookingPagePublic'));

// Dashboard pages
const DashboardHome = lazy(() => import('./pages/dashboard/DashboardHome'));
const CommunityDashboard = lazy(() => import('./pages/dashboard/CommunityDashboard'));
const CommunitySettings = lazy(() => import('./pages/dashboard/CommunitySettings'));
const CommunityChat = lazy(() => import('./pages/dashboard/CommunityChat'));
const CommunityLeaderboard = lazy(() => import('./pages/dashboard/CommunityLeaderboard'));
const CommunityMembers = lazy(() => import('./pages/dashboard/CommunityMembers'));
const AccountSettings = lazy(() => import('./pages/dashboard/AccountSettings'));
const UserProfileEdit = lazy(() => import('./pages/dashboard/UserProfileEdit'));
const CreateCommunity = lazy(() => import('./pages/dashboard/CreateCommunity'));
const MyAnalytics = lazy(() => import('./pages/dashboard/MyAnalytics'));

// Admin pages
const AdminHome = lazy(() => import('./pages/admin/AdminHome'));
const AdminMembers = lazy(() => import('./pages/admin/AdminMembers'));
const AdminModules = lazy(() => import('./pages/admin/AdminModules'));
const AdminMarketplace = lazy(() => import('./pages/admin/AdminMarketplace'));
const AdminMarketplaceModuleDetail = lazy(() => import('./pages/admin/AdminMarketplaceModuleDetail.jsx'));
const AdminCommunityPremium = lazy(() => import('./pages/admin/AdminCommunityPremium.jsx'));
const SuperAdminMarketplaceSettings = lazy(() => import('./pages/superadmin/SuperAdminMarketplaceSettings.jsx'));
const AdminStreamOverlays = lazy(() => import('./pages/admin/AdminStreamOverlays'));
const AdminDomains = lazy(() => import('./pages/admin/AdminDomains'));
const AdminServers = lazy(() => import('./pages/admin/AdminServers'));
const AdminConnectedPlatforms = lazy(() => import('./pages/admin/AdminConnectedPlatforms'));
const LiveActivity = lazy(() => import('./pages/community/LiveActivity'));
const AdminMirrorGroups = lazy(() => import('./pages/admin/AdminMirrorGroups'));
const AdminLeaderboardConfig = lazy(() => import('./pages/admin/AdminLeaderboardConfig'));
const AdminCommunityProfile = lazy(() => import('./pages/admin/AdminCommunityProfile'));
const ReputationSettings = lazy(() => import('./pages/admin/ReputationSettings'));
const AdminAIInsights = lazy(() => import('./pages/admin/AdminAIInsights'));
const AdminAIResearcherConfig = lazy(() => import('./pages/admin/AdminAIResearcherConfig'));
const AdminAIChatterConfig = lazy(() => import('./pages/admin/AdminAIChatterConfig'));
const AdminBotDetection = lazy(() => import('./pages/admin/AdminBotDetection'));
const AdminAnnouncements = lazy(() => import('./pages/admin/AdminAnnouncements'));
const AdminAnalytics = lazy(() => import('./pages/admin/AdminAnalytics'));
const AdminMemberAnalytics = lazy(() => import('./pages/admin/AdminMemberAnalytics'));
const AdminSecurity = lazy(() => import('./pages/admin/AdminSecurity'));
const LoyaltySettings = lazy(() => import('./pages/admin/LoyaltySettings'));
const LoyaltyLeaderboard = lazy(() => import('./pages/admin/LoyaltyLeaderboard'));
const LoyaltyGiveaways = lazy(() => import('./pages/admin/LoyaltyGiveaways'));
const LoyaltyGames = lazy(() => import('./pages/admin/LoyaltyGames'));
const LoyaltyGear = lazy(() => import('./pages/admin/LoyaltyGear'));
const CommunityConnections = lazy(() => import('./pages/admin/CommunityConnections'));
const AdminWorkflows = lazy(() => import('./pages/admin/AdminWorkflows'));
const AdminShoutouts = lazy(() => import('./pages/admin/AdminShoutouts'));
const AdminTranslation = lazy(() => import('./pages/admin/AdminTranslation'));
const AdminMusicDashboard = lazy(() => import('./pages/admin/AdminMusicDashboard'));
const AdminMusicSettings = lazy(() => import('./pages/admin/AdminMusicSettings'));
const AdminMusicProviders = lazy(() => import('./pages/admin/AdminMusicProviders'));
const AdminRadioStations = lazy(() => import('./pages/admin/AdminRadioStations'));
const AdminVendorReview = lazy(() => import('./pages/admin/AdminVendorReview'));
const AdminCalendarEvents = lazy(() => import('./pages/admin/AdminCalendarEvents'));
const AdminCalendarTicketing = lazy(() => import('./pages/admin/AdminCalendarTicketing'));
const AdminCalendarScanner = lazy(() => import('./pages/admin/AdminCalendarScanner'));
const AdminCalendarAttendance = lazy(() => import('./pages/admin/AdminCalendarAttendance'));
const AdminLiveStreams = lazy(() => import('./pages/admin/AdminLiveStreams'));
const AdminCommunityCalls = lazy(() => import('./pages/admin/AdminCommunityCalls'));
const AdminPolls = lazy(() => import('./pages/admin/AdminPolls'));
const AdminForms = lazy(() => import('./pages/admin/AdminForms'));
const AdminSupportDashboard = lazy(() => import('./pages/admin/AdminSupportDashboard'));
const AdminSupportTicketDetail = lazy(() => import('./pages/admin/AdminSupportTicketDetail'));
const AdminJoinRequests = lazy(() => import('./pages/admin/AdminJoinRequests'));
const AdminInventory = lazy(() => import('./pages/admin/AdminInventory'));
const AdminRconServers = lazy(() => import('./pages/admin/AdminRconServers'));
const AdminCommunityTokens = lazy(() => import('./pages/admin/AdminCommunityTokens'));
const AdminCommands = lazy(() => import('./pages/admin/AdminCommands'));
const AdminPlatformSettings = lazy(() => import('./pages/admin/AdminPlatformSettings'));
const AdminLfgConfig = lazy(() => import('./pages/admin/AdminLfgConfig'));
const AdminClipConfig = lazy(() => import('./pages/admin/AdminClipConfig'));
const AdminAliasConfig = lazy(() => import('./pages/admin/AdminAliasConfig'));
const AdminMemoriesConfig = lazy(() => import('./pages/admin/AdminMemoriesConfig'));
const AdminServerStatusConfig = lazy(() => import('./pages/admin/AdminServerStatusConfig'));
const AdminServerManagerConfig = lazy(() => import('./pages/admin/AdminServerManagerConfig'));
const MyChannels = lazy(() => import('./pages/dashboard/MyChannels'));
const SupportSubmitTicket = lazy(() => import('./pages/community/SupportSubmitTicket'));
const SupportMyTickets = lazy(() => import('./pages/community/SupportMyTickets'));
const InventoryBrowse = lazy(() => import('./pages/community/InventoryBrowse'));
const GameServers = lazy(() => import('./pages/community/GameServers'));
const InventoryMyItems = lazy(() => import('./pages/community/InventoryMyItems'));
const PersonalAccessToken = lazy(() => import('./pages/dashboard/PersonalAccessToken'));
const CommunityInteraction = lazy(() => import('./pages/community/CommunityInteraction'));
const AdminInteractionChannels = lazy(() => import('./pages/admin/AdminInteractionChannels'));

// Vendor pages
const VendorSubmissionForm = lazy(() => import('./pages/vendor/VendorSubmissionForm'));
const VendorSubmissionStatus = lazy(() => import('./pages/vendor/VendorSubmissionStatus'));
const VendorDashboard = lazy(() => import('./pages/vendor/VendorDashboard'));
const VendorSubmissions = lazy(() => import('./pages/vendor/VendorSubmissions'));
const VendorRequest = lazy(() => import('./pages/vendor/VendorRequest'));
const VendorModules = lazy(() => import('./pages/vendor/VendorModules'));
const VendorAnalytics = lazy(() => import('./pages/vendor/VendorAnalytics'));
const VendorDiscountCodes = lazy(() => import('./pages/vendor/VendorDiscountCodes'));
const VendorSettings = lazy(() => import('./pages/vendor/VendorSettings'));
const VendorGithubSync = lazy(() => import('./pages/vendor/VendorGithubSync'));

// Platform admin pages
const PlatformDashboard = lazy(() => import('./pages/platform/PlatformDashboard'));
const PlatformUsers = lazy(() => import('./pages/platform/PlatformUsers'));
const PlatformCommunities = lazy(() => import('./pages/platform/PlatformCommunities'));
const PlatformAnalytics = lazy(() => import('./pages/platform/PlatformAnalytics'));

// Super admin pages
const SuperAdminDashboard = lazy(() => import('./pages/superadmin/SuperAdminDashboard'));
const SuperAdminCommunities = lazy(() => import('./pages/superadmin/SuperAdminCommunities'));
const SuperAdminModuleRegistry = lazy(() => import('./pages/superadmin/SuperAdminModuleRegistry'));
const SuperAdminBundleApprovals = lazy(() => import('./pages/superadmin/SuperAdminBundleApprovals'));
const SuperAdminPlatformConfig = lazy(() => import('./pages/superadmin/SuperAdminPlatformConfig'));
const SuperAdminSoftwareDiscovery = lazy(() => import('./pages/superadmin/SuperAdminSoftwareDiscovery'));
const SuperAdminServiceDiscovery = lazy(() => import('./pages/superadmin/SuperAdminServiceDiscovery'));
const SuperAdminVendorRequests = lazy(() => import('./pages/superadmin/SuperAdminVendorRequests'));
const SuperAdminUsers = lazy(() => import('./pages/superadmin/SuperAdminUsers'));
const SuperAdminAnalytics = lazy(() => import('./pages/superadmin/SuperAdminAnalytics'));
const SuperAdminTenants = lazy(() => import('./pages/superadmin/SuperAdminTenants'));

// Tenant admin pages
const TenantDashboard = lazy(() => import('./pages/tenant/TenantDashboard'));
const TenantModules = lazy(() => import('./pages/tenant/TenantModules'));
const TenantAdmins = lazy(() => import('./pages/tenant/TenantAdmins'));
const TenantCommunities = lazy(() => import('./pages/tenant/TenantCommunities'));

// Admin pages (new)
const AdminCommunityRoles = lazy(() => import('./pages/admin/AdminCommunityRoles'));
const AdminRaffleCustomization = lazy(() => import('./pages/admin/AdminRaffleCustomization'));
const AdminAIKnowledge = lazy(() => import('./pages/admin/AdminAIKnowledge'));

// Living style guide: development builds only (tree-shaken from production).
const DesignSystem = import.meta.env.DEV ? lazy(() => import('./pages/dev/DesignSystem')) : null;

// Marketplace redirect helper
function MarketplaceRedirect() {
  const { communityId } = useParams();
  return <Navigate to={`/admin/${communityId}/modules`} replace />;
}

// Protected route wrapper
function ProtectedRoute({ children, requireCommunityAdmin = false, requirePlatformAdmin = false, requireSuperAdmin = false, requireAnalyticsConsumerOrSuperAdmin = false }) {
  const { user, loading, hasRole, isAnalyticsConsumer } = useAuth();

  if (loading) {
    return <LoadingState fullPage label="Checking your session" />;
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
        <Route path="/users/:userId" element={<ServedFeature feature="member-profile"><UserPublicProfile /></ServedFeature>} />
        <Route
          path="/secret"
          element={
            <Suspense fallback={<LoadingSpinner />}>
              <SecretPullPage />
            </Suspense>
          }
        />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/login/:tenantSlug" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/forgot-password" element={<PasswordRecoveryUnavailablePage />} />
        <Route path="/auth/callback" element={<OAuthCallback />} />
        {DesignSystem && <Route path="/design-system" element={<DesignSystem />} />}
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
        <Route path="/admin/:communityId/domains" element={<ServedFeature feature="custom-domains"><AdminDomains /></ServedFeature>} />
        <Route path="/admin/:communityId/servers" element={<ServedFeature feature="linked-servers"><AdminServers /></ServedFeature>} />
        <Route path="/admin/:communityId/connected-platforms" element={<AdminConnectedPlatforms />} />
        <Route path="/admin/:communityId/mirror-groups" element={<ServedFeature feature="mirror-groups"><AdminMirrorGroups /></ServedFeature>} />
        <Route path="/admin/:communityId/leaderboard" element={<AdminLeaderboardConfig />} />
        <Route path="/admin/:communityId/profile" element={<AdminCommunityProfile />} />
        <Route path="/admin/:communityId/reputation" element={<ReputationSettings />} />
        <Route path="/admin/:communityId/ai-insights" element={<ServedFeature feature="ai-insights"><AdminAIInsights /></ServedFeature>} />
        <Route path="/admin/:communityId/ai-config" element={<ServedFeature feature="ai-researcher"><AdminAIResearcherConfig /></ServedFeature>} />
        <Route path="/admin/:communityId/ai-chatter" element={<AdminAIChatterConfig />} />
        <Route path="/admin/:communityId/bot-detection" element={<ServedFeature feature="bot-detection"><AdminBotDetection /></ServedFeature>} />
        <Route path="/admin/:communityId/announcements" element={<AdminAnnouncements />} />
        <Route path="/admin/:communityId/analytics" element={<ServedFeature feature="community-analytics"><AdminAnalytics /></ServedFeature>} />
        <Route path="/admin/:communityId/security" element={<AdminSecurity />} />
        <Route path="/admin/:communityId/loyalty" element={<LoyaltySettings />} />
        <Route path="/admin/:communityId/loyalty/leaderboard" element={<LoyaltyLeaderboard />} />
        <Route path="/admin/:communityId/loyalty/giveaways" element={<LoyaltyGiveaways />} />
        <Route path="/admin/:communityId/loyalty/games" element={<LoyaltyGames />} />
        <Route path="/admin/:communityId/loyalty/gear" element={<LoyaltyGear />} />
        <Route path="/admin/:communityId/connections" element={<CommunityConnections />} />
        <Route path="/admin/:communityId/raffle-customization" element={<AdminRaffleCustomization />} />
        <Route path="/admin/:communityId/shoutouts" element={<AdminShoutouts />} />
        <Route path="/admin/:communityId/translation" element={<ServedFeature feature="translation"><AdminTranslation /></ServedFeature>} />
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
        <Route path="/admin/:communityId/platform-settings" element={<ServedFeature feature="linked-servers"><AdminPlatformSettings /></ServedFeature>} />
        <Route path="/admin/:communityId/interaction-channels" element={<AdminInteractionChannels />} />
        <Route path="/admin/:communityId/roles" element={<AdminCommunityRoles />} />
        <Route path="/admin/:communityId/members/:userId/analytics" element={<AdminMemberAnalytics />} />
        <Route path="/admin/:communityId/ai-knowledge" element={<AdminAIKnowledge />} />
        {/* Wave-1 stub: community bundles slice */}
        <Route path="/admin/:communityId/bundles" element={<AdminCommunityBundles />} />
        <Route path="/admin/:communityId/role-sync" element={<AdminRoleSyncMapping />} />
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
        <Route path="/superadmin/software-discovery" element={<ServedFeature feature="software-discovery"><SuperAdminSoftwareDiscovery /></ServedFeature>} />
        <Route path="/superadmin/services" element={<ServedFeature feature="service-discovery"><SuperAdminServiceDiscovery /></ServedFeature>} />
        <Route path="/superadmin/analytics" element={<SuperAdminAnalytics />} />
        <Route path="/superadmin/tenants" element={<SuperAdminTenants />} />
        <Route path="/superadmin/marketplace-settings" element={<SuperAdminMarketplaceSettings />} />
        {/* Wave-1 stub: super communities (1:N model) slice */}
        <Route path="/superadmin/super-communities" element={<SuperAdminSuperCommunities />} />
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
        <Route path="/tenant/:tenantSlug/bundles" element={<TenantBundleCatalog />} />
      </Route>

      {/* Catch all - redirect to home */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default App;
