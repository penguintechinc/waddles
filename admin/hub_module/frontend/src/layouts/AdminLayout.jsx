import { useMemo, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { SidebarMenu, SidebarMenuTrigger } from '@penguintechinc/react-libs';
import { useAuth } from '../contexts/AuthContext';
import { useFeatureFlag } from '../lib/useFeatureFlag';
import GlobalBanner from '../components/GlobalBanner';
import UserDropdown from '../components/UserDropdown';
import { MAIN_CONTENT_ID, RouteAnnouncer, RouteOutlet, SkipLink } from '../components/ui';
import { WADDLES_SIDEBAR_COLORS } from '../theme/sidebarTheme';
import { useSidebarNav } from './useSidebarNav';
import {
  HomeIcon,
  UserGroupIcon,
  PuzzlePieceIcon,
  GlobeAltIcon,
  ChartBarIcon,
  DocumentTextIcon,
  ShieldCheckIcon,
  Cog6ToothIcon,
  BuildingStorefrontIcon,
  ServerStackIcon,
  RectangleGroupIcon,
  ArrowsRightLeftIcon,
  TrophyIcon,
  ScaleIcon,
  SparklesIcon,
  WindowIcon,
  CurrencyDollarIcon,
  MegaphoneIcon,
  FireIcon,
  CommandLineIcon,
  SpeakerWaveIcon,
  LanguageIcon,
  CalendarDaysIcon,
  TicketIcon,
  HashtagIcon,
  AdjustmentsHorizontalIcon,
  SignalIcon,
  MusicalNoteIcon,
  VideoCameraIcon,
  HeartIcon,
  CubeIcon,
  InboxStackIcon,
  ClipboardDocumentListIcon,
  UserPlusIcon,
  AcademicCapIcon,
  LinkIcon,
  ArrowPathRoundedSquareIcon,
} from '@heroicons/react/24/outline';

/**
 * Admin shell for community admin, platform admin and super admin routes.
 * Uses the shared SidebarMenu (responsive drawer on mobile) with tokenised
 * colors; `useSidebarNav` makes exactly one item active per route.
 */
function AdminLayout() {
  const { isPlatformAdmin, isSuperAdmin } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const { communityId } = useParams();
  const [mobileOpen, setMobileOpen] = useState(false);
  // Bundle management is feature-flagged (defaults OFF); only advertise it when the page will render.
  const bundlesEnabled = useFeatureFlag('waddles.webui.community_bundles');

  // Section definitions for community admin
  const communitySections = useMemo(() => (communityId
    ? [
        {
          key: 'community',
          label: 'Community',
          icon: HomeIcon,
          items: [
            { to: `/admin/${communityId}`, icon: HomeIcon, label: 'Overview', exact: true },
            { to: `/admin/${communityId}/live-activity`, icon: SignalIcon, label: 'Live Activity' },
            { to: `/admin/${communityId}/profile`, icon: UserGroupIcon, label: 'Profile' },
            { to: `/admin/${communityId}/members`, icon: UserGroupIcon, label: 'Members' },
            { to: `/admin/${communityId}/join-requests`, icon: UserPlusIcon, label: 'Join Requests' },
            { to: `/admin/${communityId}/announcements`, icon: MegaphoneIcon, label: 'Announcements' },
            { to: `/admin/${communityId}/interaction-channels`, icon: HashtagIcon, label: 'Channels' },
            { to: `/admin/${communityId}/roles`, icon: ShieldCheckIcon, label: 'Roles' },
          ],
        },
        {
          key: 'engagement',
          label: 'Engagement',
          icon: HeartIcon,
          items: [
            { to: `/admin/${communityId}/polls`, icon: ClipboardDocumentListIcon, label: 'Polls' },
            { to: `/admin/${communityId}/forms`, icon: DocumentTextIcon, label: 'Forms' },
            { to: `/admin/${communityId}/shoutouts`, icon: SpeakerWaveIcon, label: 'Shoutouts' },
            { to: `/admin/${communityId}/leaderboard`, icon: TrophyIcon, label: 'Leaderboards' },
            { to: `/admin/${communityId}/calendar/events`, icon: CalendarDaysIcon, label: 'Calendar Events' },
          ],
        },
        {
          key: 'media',
          label: 'Media',
          icon: VideoCameraIcon,
          items: [
            { to: `/admin/${communityId}/music`, icon: MusicalNoteIcon, label: 'Music' },
            { to: `/admin/${communityId}/live-streaming`, icon: VideoCameraIcon, label: 'Live Streaming' },
            { to: `/admin/${communityId}/stream-overlays`, icon: WindowIcon, label: 'Stream Overlays' },
            { to: `/admin/${communityId}/calls`, icon: SpeakerWaveIcon, label: 'Community Calls' },
          ],
        },
        {
          key: 'modules',
          label: 'Modules',
          icon: PuzzlePieceIcon,
          items: [
            { to: `/admin/${communityId}/modules`, icon: PuzzlePieceIcon, label: 'Modules & Marketplace' },
          ],
        },
        {
          key: 'loyalty',
          label: 'Loyalty',
          icon: CurrencyDollarIcon,
          items: [
            { to: `/admin/${communityId}/loyalty`, icon: CurrencyDollarIcon, label: 'Settings' },
            { to: `/admin/${communityId}/loyalty/leaderboard`, icon: TrophyIcon, label: 'Leaderboard' },
            { to: `/admin/${communityId}/loyalty/giveaways`, icon: HeartIcon, label: 'Giveaways' },
            { to: `/admin/${communityId}/loyalty/games`, icon: CubeIcon, label: 'Games' },
            { to: `/admin/${communityId}/loyalty/gear`, icon: InboxStackIcon, label: 'Gear' },
            { to: `/admin/${communityId}/raffle-customization`, icon: SpeakerWaveIcon, label: 'Raffle Sounds', premium: true },
          ],
        },
        {
          key: 'connections',
          label: 'Connections',
          icon: LinkIcon,
          items: [
            { to: `/admin/${communityId}/connections`, icon: LinkIcon, label: 'Connections' },
            {
              to: `/admin/${communityId}/role-sync`,
              icon: ArrowPathRoundedSquareIcon,
              label: 'Role Sync',
            },
          ],
        },
        {
          key: 'moderation',
          label: 'Moderation',
          icon: ShieldCheckIcon,
          items: [
            { to: `/admin/${communityId}/security`, icon: FireIcon, label: 'Security' },
            { to: `/admin/${communityId}/bot-detection`, icon: ShieldCheckIcon, label: 'Bot Detection' },
            { to: `/admin/${communityId}/reputation`, icon: ScaleIcon, label: 'Reputation' },
            { to: `/admin/${communityId}/commands`, icon: HashtagIcon, label: 'Commands' },
          ],
        },
        {
          key: 'ai',
          label: 'AI',
          icon: SparklesIcon,
          items: [
            { to: `/admin/${communityId}/ai-insights`, icon: SparklesIcon, label: 'AI Insights' },
            { to: `/admin/${communityId}/ai-config`, icon: Cog6ToothIcon, label: 'AI Config' },
            { to: `/admin/${communityId}/ai-knowledge`, icon: AcademicCapIcon, label: 'AI Knowledge', premium: true },
          ],
        },
        {
          key: 'platform',
          label: 'Platform',
          icon: RectangleGroupIcon,
          items: [
            { to: `/admin/${communityId}/connected-platforms`, icon: RectangleGroupIcon, label: 'Connected Platforms' },
            { to: `/admin/${communityId}/servers`, icon: ServerStackIcon, label: 'Linked Servers' },
            { to: `/admin/${communityId}/rcon`, icon: SignalIcon, label: 'Game Servers', premium: true },
            { to: `/admin/${communityId}/mirror-groups`, icon: ArrowsRightLeftIcon, label: 'Chat Mirroring' },
            { to: `/admin/${communityId}/platform-settings`, icon: AdjustmentsHorizontalIcon, label: 'Platform Settings' },
          ],
        },
        {
          key: 'settings',
          label: 'Settings',
          icon: Cog6ToothIcon,
          items: [
            { to: `/admin/${communityId}/analytics`, icon: ChartBarIcon, label: 'Analytics' },
            { to: `/admin/${communityId}/workflows`, icon: CommandLineIcon, label: 'Workflows', premium: true },
            ...(bundlesEnabled
              ? [{ to: `/admin/${communityId}/bundles`, icon: PuzzlePieceIcon, label: 'Bundle Apps' }]
              : []),
            { to: `/admin/${communityId}/domains`, icon: GlobeAltIcon, label: 'Custom Domains' },
            { to: `/admin/${communityId}/translation`, icon: LanguageIcon, label: 'Translation' },
            { to: `/admin/${communityId}/support`, icon: TicketIcon, label: 'Support Tickets' },
            { to: `/admin/${communityId}/tokens`, icon: Cog6ToothIcon, label: 'Tokens' },
            { to: `/admin/${communityId}/inventory`, icon: InboxStackIcon, label: 'Inventory' },
          ],
        },
      ]
    : []), [communityId, bundlesEnabled]);

  // Platform admin nav
  const platformAdminNav = useMemo(() => [
    { to: '/platform', icon: ChartBarIcon, label: 'Dashboard', exact: true },
    { to: '/platform/users', icon: UserGroupIcon, label: 'Users' },
    { to: '/platform/communities', icon: HomeIcon, label: 'Communities' },
  ], []);

  // Super admin nav
  const superAdminNav = useMemo(() => [
    { to: '/superadmin', icon: ChartBarIcon, label: 'Dashboard', exact: true },
    { to: '/superadmin/communities', icon: HomeIcon, label: 'Communities' },
    { to: '/superadmin/modules', icon: BuildingStorefrontIcon, label: 'Module Registry' },
    { to: '/superadmin/analytics', icon: ChartBarIcon, label: 'Analytics' },
    { to: '/superadmin/platform-config', icon: Cog6ToothIcon, label: 'Platform Config' },
    { to: '/superadmin/tenants', icon: ServerStackIcon, label: 'Tenants' },
  ], []);

  const isSuperAdminPath = location.pathname.startsWith('/superadmin');
  const title = communityId ? 'Community Admin' : isSuperAdminPath ? 'Super Admin' : 'Platform Admin';

  const groups = useMemo(() => {
    if (communityId) {
      return communitySections.map((section) => ({
        key: section.key,
        header: section.label,
        collapsible: true,
        items: section.items.map((item) => ({
          name: item.premium ? `${item.label} (Pro)` : item.label,
          href: item.to,
          icon: item.icon,
          exact: item.exact,
        })),
      }));
    }
    const flat = isSuperAdminPath ? superAdminNav : platformAdminNav;
    return [
      {
        key: 'main',
        header: title,
        collapsible: false,
        items: flat.map((item) => ({ name: item.label, href: item.to, icon: item.icon, exact: item.exact })),
      },
    ];
  }, [communityId, communitySections, isSuperAdminPath, platformAdminNav, superAdminNav, title]);

  const footer = useMemo(() => {
    const items = [];
    if (communityId) items.push({ name: 'View Community', href: `/dashboard/community/${communityId}`, icon: DocumentTextIcon });
    if (isPlatformAdmin && communityId) items.push({ name: 'Platform Admin', href: '/platform', icon: ShieldCheckIcon });
    if (isSuperAdmin && !isSuperAdminPath) items.push({ name: 'Super Admin', href: '/superadmin', icon: ShieldCheckIcon });
    items.push({ name: 'Back to Dashboard', href: '/dashboard', icon: HomeIcon });
    return items;
  }, [communityId, isPlatformAdmin, isSuperAdmin, isSuperAdminPath]);

  // Open the group that owns the current route (SidebarMenu autoCollapse).
  const activeGroupKey = useMemo(() => {
    if (!communityId) return 'main';
    const owner = communitySections.find((section) =>
      section.items.some((item) =>
        item.exact ? location.pathname === item.to : location.pathname.startsWith(item.to),
      ),
    );
    return owner?.key ?? 'community';
  }, [communityId, communitySections, location.pathname]);

  const nav = useSidebarNav(groups, footer, location.pathname, navigate);

  const brand = (
    <Link to="/" className="flex items-center gap-2 rounded-control">
      <img src="/waddlebot-logo.png" alt="" className="h-8 w-8" />
      <span className="text-xl font-bold text-fg-accent">Waddles</span>
    </Link>
  );

  return (
    <div className="min-h-screen bg-app">
      <SkipLink />
      <RouteAnnouncer />
      <SidebarMenu
        logo={brand}
        categories={nav.categories}
        currentPath={nav.currentPath}
        onNavigate={nav.onNavigate}
        footerItems={nav.footerItems}
        autoCollapse
        activeGroupKey={activeGroupKey}
        width="w-64"
        themeMode="dark"
        colors={WADDLES_SIDEBAR_COLORS}
        mobileOpen={mobileOpen}
        onMobileClose={() => setMobileOpen(false)}
      />

      <div className="flex min-h-screen flex-col lg:pl-64">
        <GlobalBanner />
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-line bg-surface/95 px-4 backdrop-blur sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <SidebarMenuTrigger
              onClick={() => setMobileOpen((open) => !open)}
              isOpen={mobileOpen}
              themeMode="dark"
              colors={WADDLES_SIDEBAR_COLORS}
            />
            <div className="lg:hidden">{brand}</div>
            <div className="hidden items-center gap-2 text-fg-accent sm:flex">
              <ShieldCheckIcon aria-hidden="true" className="h-5 w-5" />
              <span className="text-sm font-semibold">{title}</span>
            </div>
          </div>
          <UserDropdown />
        </header>

        <main id={MAIN_CONTENT_ID} tabIndex={-1} className="flex-1 px-4 py-8 focus:outline-none sm:px-6 lg:px-8">
          <div className="mx-auto w-full max-w-7xl">
            <RouteOutlet />
          </div>
        </main>
      </div>
    </div>
  );
}

export default AdminLayout;
