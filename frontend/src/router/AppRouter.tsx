import { Navigate, Route, Routes } from 'react-router'

import { BranchPage } from '../pages/BranchPage.tsx'
import { NotFoundPage } from '../pages/NotFoundPage.tsx'
import { OrganizationLayout } from '../pages/OrganizationLayout.tsx'
import { OrganizationPage } from '../pages/OrganizationPage.tsx'
import { OrganizationsPage } from '../pages/OrganizationsPage.tsx'
import { ORGANIZATIONS_PATH } from './paths.ts'

/**
 * The signed-in app's pages. Rendered only for an authenticated user, inside
 * whichever router the caller provides (the browser's in main.tsx, an
 * in-memory one in tests) -- so no page, and no request a page makes, exists
 * before sign-in.
 */
export function AppRouter() {
  return (
    <Routes>
      <Route index element={<Navigate to={ORGANIZATIONS_PATH} replace />} />
      <Route path="organizations" element={<OrganizationsPage />} />
      <Route path="organizations/:orgSlug" element={<OrganizationLayout />}>
        <Route index element={<OrganizationPage />} />
        <Route path="branches/:branchSlug" element={<BranchPage />} />
      </Route>
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
