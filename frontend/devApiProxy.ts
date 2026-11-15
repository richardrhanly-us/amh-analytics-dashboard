// The development server's /api proxy for the admin app (vite.admin.config.ts). Development only: `vite build` never
// reads the `server` block, and neither variable is VITE_-prefixed, so nothing here can reach a built bundle.
//
// DEV_API_PROXY_TARGET        where /api is forwarded. Empty or unset: no proxy at all, and no fallback host.
// DEV_API_PROXY_CHANGE_ORIGIN `true` (exactly, after trimming) sends the TARGET's host name as the Host header, which a
//                             hosted backend (a DigitalOcean app) requires. Anything else keeps the default: the
//                             browser's own Host, as for a local backend. The browser's Origin header is forwarded
//                             unchanged either way, so the backend's origin checks are unaffected.

export type DevApiProxy = Record<'/api', { target: string; changeOrigin: boolean }>

export function devApiProxy(env: Record<string, string | undefined>): DevApiProxy | undefined {
  const target = (env.DEV_API_PROXY_TARGET ?? '').trim()
  if (target === '') {
    return undefined
  }
  return { '/api': { target, changeOrigin: (env.DEV_API_PROXY_CHANGE_ORIGIN ?? '').trim() === 'true' } }
}
