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
      ],
      exclude: ['src/components/ui/index.ts', 'src/components/ui/**/__tests__/**'],
      thresholds: {
        statements: 90,
        branches: 90,
        functions: 90,
        lines: 90,
      },
    },
  },
});
