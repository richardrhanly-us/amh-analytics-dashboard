import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'

// The separate SortView administration app (A2 item 1). Its own root, entry and output, so nothing of it is ever in
// the customer build -- which is vite.config.ts, unchanged. It shares this package's dependencies, its .env files
// (envDir) and the same development-only /api proxy.
const frontendDir = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, frontendDir, '')
  const proxyTarget = (env.DEV_API_PROXY_TARGET ?? '').trim()

  return {
    root: fileURLToPath(new URL('./admin', import.meta.url)),
    envDir: frontendDir,
    plugins: [react()],
    build: {
      outDir: fileURLToPath(new URL('./dist-admin', import.meta.url)),
      emptyOutDir: true,
    },
    // Dev server only, as in vite.config.ts. A different port, so both apps can run side by side.
    server: {
      port: 5174,
      ...(proxyTarget === '' ? {} : { proxy: { '/api': { target: proxyTarget, changeOrigin: false } } }),
    },
  }
})
