import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // '' as the prefix reads every variable, including the deliberately un-prefixed DEV_API_PROXY_TARGET. It is
  // used here, in Node, and is never exposed to browser code (only VITE_* variables are).
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = (env.DEV_API_PROXY_TARGET ?? '').trim()

  return {
    plugins: [react()],
    // Dev server only: `vite build` ignores `server`, so there is no production proxy. With no target set there
    // is no proxy at all -- it never falls back to any host.
    server: proxyTarget === '' ? {} : { proxy: { '/api': { target: proxyTarget, changeOrigin: false } } },
  }
})
