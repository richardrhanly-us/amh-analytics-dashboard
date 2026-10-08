import { useRef } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router'

import { AccountPage } from '../account/AccountPage.tsx'
import { MembersPage } from '../members/MembersPage.tsx'
import { LegacyBranchRedirect } from '../pages/LegacyBranchRedirect.tsx'
import { NotFoundPage } from '../pages/NotFoundPage.tsx'
import { OrganizationLayout } from '../pages/OrganizationLayout.tsx'
import { OrganizationPage } from '../pages/OrganizationPage.tsx'
import { OrganizationReportsPage } from '../pages/OrganizationReportsPage.tsx'
import { OrganizationsPage } from '../pages/OrganizationsPage.tsx'
import { SorterLayout } from '../pages/SorterLayout.tsx'
import { SorterPage } from '../pages/SorterPage.tsx'
import { SorterReportsPage } from '../pages/SorterReportsPage.tsx'
import { BranchesPage } from '../settings/BranchesPage.tsx'
import { EfficiencySettingsPage } from '../settings/EfficiencySettingsPage.tsx'
import { GeneralPage } from '../settings/GeneralPage.tsx'
import { RoutingPage } from '../settings/RoutingPage.tsx'
import { SettingsLayout } from '../settings/SettingsLayout.tsx'
import { PageArrivalContext } from './PageArrivalContext.ts'
import { ORGANIZATIONS_PATH } from './paths.ts'

/**
 * The signed-in app's pages. Rendered only for an authenticated user, inside
 * whichever router the caller provides (the browser's in main.tsx, an
 * in-memory one in tests) -- so no page, and no request a page makes, exists
 * before sign-in.
 */
export function AppRouter() {
  // The entry the signed-in app opens on counts as already introduced: see PageHeading.
  const introducedRef = useRef(useLocation().key)

  return (
    <PageArrivalContext value={introducedRef}>
      <Routes>
        <Route index element={<Navigate to={ORGANIZATIONS_PATH} replace />} />
        {/* The signed-in person's own account. Beside the organizations, not under one. */}
        <Route path="account" element={<AccountPage />} />
        <Route path="organizations" element={<OrganizationsPage />} />
        <Route path="organizations/:orgSlug" element={<OrganizationLayout />}>
          <Route index element={<OrganizationPage />} />
          {/* The organization's own reports: every sorter together. A sorter's reports are under that sorter. */}
          <Route path="reports" element={<OrganizationReportsPage />} />
          {/* Who belongs to the organization. The page itself says so to anyone it is not for. */}
          <Route path="members" element={<MembersPage />} />
          {/* The organization's own configuration, a section to an address. The first section is where it opens. */}
          <Route path="settings" element={<SettingsLayout />}>
            <Route index element={<Navigate to="general" replace />} />
            <Route path="general" element={<GeneralPage />} />
            <Route path="branches" element={<BranchesPage />} />
            <Route path="routing" element={<RoutingPage />} />
            <Route path="efficiency" element={<EfficiencySettingsPage />} />
          </Route>
          <Route path="sorters/:sorterSlug" element={<SorterLayout />}>
            <Route index element={<SorterPage />} />
            <Route path="reports" element={<SorterReportsPage />} />
          </Route>
          {/* The address a sorter's dashboard used to have: redirects to the sorter hosted there, if one is. */}
          <Route path="branches/:branchSlug" element={<LegacyBranchRedirect />} />
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </PageArrivalContext>
  )
}
