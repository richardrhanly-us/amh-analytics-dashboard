/* eslint-disable react-refresh/only-export-components -- a test helper: never hot-reloaded */
import type { QueryClient } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router'

import App from '../App.tsx'
import { AuthProvider } from '../auth/AuthProvider.tsx'
import { createQueryClient } from '../query/queryClient.ts'

/** Shows the router's current address, and stands in for the browser's Back and Forward buttons. */
function Browser() {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <div data-testid="browser">
      <span data-testid="address">{location.pathname}</span>
      <button onClick={() => navigate(-1)}>browser-back</button>
      <button onClick={() => navigate(1)}>browser-forward</button>
    </div>
  )
}

/**
 * The app's own query client with one change. By default a failed read is not retried, so a test sees a
 * failure at once. With `retries`, the app's real retry rule decides, with no wait between attempts.
 * Every render makes its own client, so nothing is shared between tests.
 */
function testQueryClient(retries: boolean) {
  return (onSessionExpired: () => void): QueryClient => {
    const client = createQueryClient(onSessionExpired)
    const queries = client.getDefaultOptions().queries
    client.setDefaultOptions({ queries: retries ? { ...queries, retryDelay: 0 } : { ...queries, retry: false } })
    return client
  }
}

/** The whole app as the browser would show it at `path`, with an in-memory address bar. */
export function appAt(path = '/', { retries = false }: { retries?: boolean } = {}) {
  return (
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App createClient={testQueryClient(retries)} />
      </AuthProvider>
      <Browser />
    </MemoryRouter>
  )
}

export function renderApp(path = '/', options: { retries?: boolean } = {}) {
  return render(appAt(path, options))
}
