import { Outlet, Link, useLocation, useParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import VendorRequestFooter from '../components/VendorRequestFooter';
import {
  HomeIcon,
  UserGroupIcon,
  ChatBubbleLeftRightIcon,
  Cog6ToothIcon,
  UserCircleIcon,
  UserIcon,
  ChartBarIcon,
  ChartBarSquareIcon,
  BuildingStorefrontIcon,
  ShieldCheckIcon,
  ShoppingCartIcon,
  LinkIcon,
  TrophyIcon,
  InboxStackIcon,
  ServerStackIcon,
  TicketIcon,
  CubeIcon,
  CodeBracketIcon,
} from '@heroicons/react/24/outline';
import { useMemo, useState } from 'react';
import GlobalBanner from '../components/GlobalBanner';
import { SidebarMenu, SidebarMenuTrigger } from '@penguintechinc/react-libs';
import UserDropdown from '../components/UserDropdown';

function DashboardLayout() {
  const { user, isSuperAdmin, isVendor, isAnalyticsConsumer, isCommunityAdmin } = useAuth();
  const location = useLocation();
  const { id: communityId } = useParams();
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);

  const activeGroupKey = useMemo(() => {
    if (location.pathname.startsWith('/superadmin') || location.pathname.startsWith('/admin/platform')) {
      return 'super-admin';
    }
    if (!communityId) return 'main';
    if (location.pathname.startsWith('/admin')) return 'community-admin';
    return 'community';
  }, [communityId, location.pathname]);

  const categories = useMemo(() => {
    const cats = [];

    if (!communityId) {
      const mainItems = [
        { name: 'My Communities', href: '/dashboard', icon: HomeIcon },
        { name: 'My Profile', href: '/dashboard/profile', icon: UserIcon },
        { name: 'Account Settings', href: '/dashboard/settings', icon: UserCircleIcon },
        { name: 'My Channels', href: '/dashboard/my-channels', icon: LinkIcon },
        { name: 'My Analytics', href: '/dashboard/my-analytics', icon: ChartBarIcon },
      ];

      // Platform Analytics — visible to analytics consumers and super admins
      if (isAnalyticsConsumer || isSuperAdmin) {
        mainItems.push({ name: 'Platform Analytics', href: '/platform/analytics', icon: ChartBarIcon });
      }

      cats.push({
        key: 'main',
        header: 'Navigation',
        collapsible: false,
        defaultOpen: true,
        items: mainItems,
      });
    } else {
      // Community nav — Settings moved to community-admin group
      cats.push({
        key: 'community',
        header: 'Community',
        collapsible: true,
        defaultOpen: true,
        items: [
          { name: 'Overview', href: `/dashboard/community/${communityId}`, icon: HomeIcon },
          { name: 'Members', href: `/dashboard/community/${communityId}/members`, icon: UserGroupIcon },
          { name: 'Chat & Forums', href: `/community/${communityId}/interact`, icon: ChatBubbleLeftRightIcon },
          { name: 'Leaderboard', href: `/dashboard/community/${communityId}/leaderboard`, icon: TrophyIcon },
          { name: 'Inventory', href: `/community/${communityId}/inventory`, icon: InboxStackIcon },
          { name: 'Game Servers', href: `/community/${communityId}/game-servers`, icon: ServerStackIcon },
          { name: 'Support', href: `/community/${communityId}/support/submit`, icon: TicketIcon },
        ],
      });

      if (isCommunityAdmin(communityId)) {
        cats.push({
          key: 'community-admin',
          header: 'Community Admin',
          collapsible: true,
          defaultOpen: activeGroupKey === 'community-admin',
          items: [
            { name: 'Community Settings', href: `/dashboard/community/${communityId}/settings`, icon: Cog6ToothIcon },
            { name: 'Moderation', href: `/dashboard/community/${communityId}/moderation`, icon: ShieldCheckIcon },
            { name: 'Admin Panel', href: `/admin/${communityId}`, icon: BuildingStorefrontIcon },
          ],
        });
      }
    }

    if (isVendor) {
      cats.push({
        key: 'vendor',
        header: 'Vendor',
        collapsible: true,
        defaultOpen: activeGroupKey === 'vendor',
        items: [
          { name: 'Dashboard', href: '/vendor/dashboard', icon: ChartBarIcon },
          { name: 'My Submissions', href: '/vendor/submissions', icon: BuildingStorefrontIcon },
          { name: 'Submit New Module', href: '/vendor/submit', icon: ShoppingCartIcon },
          { name: 'My Modules', href: '/vendor/modules', icon: CubeIcon },
          { name: 'Analytics', href: '/vendor/analytics', icon: ChartBarSquareIcon },
          { name: 'Discount Codes', href: '/vendor/discount-codes', icon: TicketIcon },
          { name: 'GitHub Sync', href: '/vendor/github-sync', icon: CodeBracketIcon },
          { name: 'Settings', href: '/vendor/settings', icon: Cog6ToothIcon },
        ],
      });
    }

    if (isSuperAdmin) {
      cats.push({
        key: 'super-admin',
        header: 'Super Admin',
        collapsible: true,
        defaultOpen: activeGroupKey === 'super-admin',
        items: [
          { name: 'Dashboard', href: '/superadmin', icon: ChartBarIcon },
          { name: 'Communities', href: '/superadmin/communities', icon: HomeIcon },
          { name: 'Module Registry', href: '/superadmin/modules', icon: BuildingStorefrontIcon },
          { name: 'Bundle Approvals', href: '/superadmin/bundle-approvals', icon: InboxStackIcon },
          { name: 'User Management', href: '/superadmin/users', icon: UserIcon },
          { name: 'Vendor Requests', href: '/superadmin/vendor-requests', icon: ShoppingCartIcon },
          { name: 'Analytics', href: '/superadmin/analytics', icon: ChartBarIcon },
          { name: 'Platform Config', href: '/superadmin/platform-config', icon: Cog6ToothIcon },
        ],
      });
    }

    return cats;
  }, [communityId, activeGroupKey, isSuperAdmin, isVendor, isAnalyticsConsumer, isCommunityAdmin]);

  return (
    <div className="min-h-screen bg-navy-950">
      <GlobalBanner />
      {/* Top bar */}
      <header className="bg-navy-900 border-b border-navy-700 sticky top-0 z-50">
        <div className="flex justify-between items-center h-16 px-4 sm:px-6 lg:px-8">
          <div className="flex items-center space-x-3">
            <SidebarMenuTrigger
              onClick={() => setMobileOpen((o) => !o)}
              isOpen={mobileOpen}
              themeMode="dark"
            />
            <Link to="/" className="flex items-center space-x-2">
              <img src="/waddlebot-logo.png" alt="Waddles" className="w-8 h-8" />
              <span className="text-xl font-bold text-gold-400">Waddles</span>
            </Link>
          </div>

          <UserDropdown />
        </div>
      </header>

      <div className="flex">
        {/* Sidebar */}
        <SidebarMenu
          categories={categories}
          currentPath={location.pathname}
          onNavigate={(href) => navigate(href)}
          autoCollapse={true}
          activeGroupKey={activeGroupKey}
          width="w-64"
          themeMode="dark"
          mobileOpen={mobileOpen}
          onMobileClose={() => setMobileOpen(false)}
          footerItems={[
            { name: user?.displayName || user?.username || 'Account', href: '/dashboard/profile', icon: UserCircleIcon },
          ]}
        />

        {/* Main content — offset by sidebar width */}
        <main className="flex-1 p-6 lg:ml-64">
          <Outlet />
        </main>
      </div>
      <VendorRequestFooter />
    </div>
  );
}

export default DashboardLayout;
