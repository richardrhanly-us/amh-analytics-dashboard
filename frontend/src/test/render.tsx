/* eslint-disable react-refresh/only-export-components -- a test helper: never hot-reloaded */
import { render } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router'

import App from '../App.tsx'
import { AuthProvider } from '../auth/AuthProvider.tsx'

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

/** The whole app as the browser would show it at `path`, with an in-memory address bar. */
export function appAt(path = '/') {
  return (
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
      <Browser />
    </MemoryRouter>
  )
}

export function renderApp(path = '/') {
  return render(appAt(path))
}
