import { Link, Navigate, Route, Routes } from 'react-router'

import { CollectorConfigPage } from './collector/CollectorConfigPage.tsx'
import { SorterDiagnosticsPage } from './diagnostics/SorterDiagnosticsPage.tsx'
import { InstallationsPage } from './installations/InstallationsPage.tsx'
import { LibrariesPage } from './libraries/LibrariesPage.tsx'
import { LibraryPage } from './libraries/LibraryPage.tsx'
import { LifecyclePage } from './lifecycle/LifecyclePage.tsx'

function NotFoundPage() {
  return (
    <section aria-labelledby="not-found-heading">
      <h2 id="not-found-heading">Page not found</h2>
      <p>
        <Link to="/libraries">Libraries</Link>
      </p>
    </section>
  )
}

/** The signed-in administrator's pages. Rendered only once GET /api/admin/session has answered 200. */
export function AdminRoutes() {
  return (
    <Routes>
      <Route index element={<Navigate to="/libraries" replace />} />
      <Route path="libraries" element={<LibrariesPage />} />
      <Route path="libraries/:orgSlug" element={<LibraryPage />} />
      <Route path="libraries/:orgSlug/collector" element={<CollectorConfigPage />} />
      <Route path="libraries/:orgSlug/installations" element={<InstallationsPage />} />
      <Route path="libraries/:orgSlug/lifecycle" element={<LifecyclePage />} />
      <Route path="libraries/:orgSlug/sorters/:sorterSlug" element={<SorterDiagnosticsPage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
