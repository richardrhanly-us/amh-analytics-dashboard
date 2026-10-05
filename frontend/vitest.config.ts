import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Kept apart from vite.config.ts so the test run never reads DEV_API_PROXY_TARGET or starts a proxy.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // Every test starts from clean mocks, environment variables and globals.
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
  },
})
