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
    // react-libs ships extensionless directory imports Node ESM rejects; let vite resolve them.
    server: { deps: { inline: ['@penguintechinc/react-libs'] } },
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
        // RTC calls: typed hub-api client, query hooks, moderator/member views.
        'src/services/callsApi.ts',
        'src/hooks/useCalls.ts',
        'src/components/calls/**/*.{ts,tsx}',
        'src/components/interaction/VoiceView.tsx',
        'src/pages/admin/AdminCommunityCalls.tsx',
        // Town Hall: browser WHIP/WHEP media client, call session state machine, call view and page.
        'src/services/media/**/*.ts',
        'src/hooks/{useCallSession,useCallPresence}.ts',
        'src/pages/community/TownHall.tsx',
        // Contract re-points: typed AI-knowledge client, the gap-check route matcher, gated/re-pointed pages.
        'scripts/lib/backend-routes.mjs',
        'src/pages/admin/AdminHome.jsx',
      ],
      exclude: ['src/components/ui/index.ts', 'src/components/ui/**/__tests__/**', 'src/components/**/__tests__/**', 'src/services/media/__tests__/**'],
      thresholds: {
        statements: 90,
        branches: 90,
        functions: 90,
        lines: 90,
      },
    },
  },
});
