import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8060',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    coverage: {
      provider: 'v8',
      include: [
        'src/components/dashboard/CommunityStatsWidget.jsx',
        'src/components/dashboard/QuickActionsWidget.jsx',
        'src/layouts/DashboardLayout.jsx',
        'src/layouts/AdminLayout.jsx',
        'src/layouts/useSidebarNav.ts',
        'src/components/ui/**/*.{ts,tsx}',
        // Contract-gap fix: typed served-route services, their query hooks and the pages built on them.
        'src/lib/wire.ts',
        'src/lib/unservedFeatures.ts',
        'src/contexts/authView.ts',
        'src/services/{myCommunities,communityActivity,communityMembers,communityOverview,communityAdmin,joinRequests,reputation}Api.ts',
        'src/hooks/{useMyCommunities,useCommunityActivity,useCommunityPanels,useJoinRequests}.ts',
        'src/components/leaderboard/**/*.{ts,tsx}',
        'src/components/community/**/*.{ts,tsx}',
        'src/components/dashboard/{MyCommunityCard,MemberRow,CommunityHeader,AnnouncementsPanel}.tsx',
        'src/components/dashboard/{communityRoles,memberFormat}.ts',
        'src/pages/dashboard/{DashboardHome,CommunityDashboard,CommunityLeaderboard,CommunityMembers,CreateCommunity}.tsx',
        'src/pages/community/LiveActivity.tsx',
        'src/pages/admin/AdminJoinRequests.tsx',
      ],
      exclude: ['src/components/ui/index.ts', 'src/components/ui/**/__tests__/**', 'src/components/**/__tests__/**'],
      thresholds: {
        statements: 90,
        branches: 90,
        functions: 90,
        lines: 90,
      },
    },
  },
});
