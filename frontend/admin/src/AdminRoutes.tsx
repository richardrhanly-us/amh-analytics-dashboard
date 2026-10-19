import { useRef } from 'react'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router'

import { CollectorConfigPage } from './collector/CollectorConfigPage.tsx'
import { SorterDiagnosticsPage } from './diagnostics/SorterDiagnosticsPage.tsx'
import { InstallationsPage } from './installations/InstallationsPage.tsx'
import { LibrariesPage } from './libraries/LibrariesPage.tsx'
import { LibraryPage } from './libraries/LibraryPage.tsx'
import { LifecyclePage } from './lifecycle/LifecyclePage.tsx'
import { ProvisionPage } from './provision/ProvisionPage.tsx'
import { AdminPageHeading } from './AdminPageHeading.tsx'
import { AdminPageArrivalContext } from './pageArrival.ts'

function NotFoundPage() {
  return (
    <section aria-labelledby="not-found-heading">
      <AdminPageHeading id="not-found-heading">Page not found</AdminPageHeading>
      <p>
        <Link to="/libraries">Libraries</Link>
      </p>
    </section>
  )
}

/** The signed-in administrator's pages. Rendered only once GET /api/admin/session has answered 200. */
export function AdminRoutes() {
  // The entry the signed-in app opens on counts as already introduced: see AdminPageHeading.
  const introducedRef = useRef(useLocation().key)

  return (
    <AdminPageArrivalContext value={introducedRef}>
      <Routes>
        <Route index element={<Navigate to="/libraries" replace />} />
        <Route path="libraries" element={<LibrariesPage />} />
        <Route path="provision" element={<ProvisionPage />} />
        <Route path="libraries/:orgSlug" element={<LibraryPage />} />
        <Route path="libraries/:orgSlug/collector" element={<CollectorConfigPage />} />
        <Route path="libraries/:orgSlug/installations" element={<InstallationsPage />} />
        <Route path="libraries/:orgSlug/lifecycle" element={<LifecyclePage />} />
        <Route path="libraries/:orgSlug/sorters/:sorterSlug" element={<SorterDiagnosticsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </AdminPageArrivalContext>
  )
}
