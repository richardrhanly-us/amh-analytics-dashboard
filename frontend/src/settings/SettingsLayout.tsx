import type { ReactNode } from 'react'
import { NavLink, Outlet, useOutletContext } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { MEMBERS_PAGE_NAME } from '../members/memberText.ts'
import {
  ORGANIZATIONS_PATH,
  organizationMembersPath,
  organizationPath,
  organizationSettingsPath,
  settingsBranchesPath,
  settingsEfficiencyPath,
  settingsGeneralPath,
  settingsRoutingPath,
} from '../router/paths.ts'
import {
  BRANCHES_NAME,
  canManageOrganization,
  EFFICIENCY_NAME,
  GENERAL_NAME,
  MANAGED_BY_OWNERS_AND_ADMINS,
  ROUTING_NAME,
  SETTINGS_NAME,
} from './settingsText.ts'

/**
 * The sections of an organization's own configuration, as a strip of links.
 * Each has an address, so each is a link, and the current one says so. A
 * section is listed once it exists: there is no link to something not built.
 *
 * The members page is one of them and keeps the address it always had.
 */
function SectionNav({ orgSlug }: { orgSlug: string }) {
  const sections = [
    { to: settingsGeneralPath(orgSlug), label: GENERAL_NAME },
    { to: organizationMembersPath(orgSlug), label: MEMBERS_PAGE_NAME },
    { to: settingsBranchesPath(orgSlug), label: BRANCHES_NAME },
    { to: settingsRoutingPath(orgSlug), label: ROUTING_NAME },
    { to: settingsEfficiencyPath(orgSlug), label: EFFICIENCY_NAME },
  ]
  return (
    <nav aria-label={SETTINGS_NAME} className="view-nav">
      <ul>
        {sections.map((section) => (
          <li key={section.to}>
            <NavLink to={section.to} end>
              {section.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}

/**
 * What every section is drawn inside: the way back up, the section's name as
 * the page's heading, and the strip of sections.
 *
 * `offered` is false for someone the area is not for. They get the heading
 * and whatever the page has to say to them, with no strip and no link into
 * the area.
 */
export function SettingsFrame({
  organization,
  section,
  offered = true,
  children,
}: {
  organization: OrganizationDetail
  section: string
  offered?: boolean
  children: ReactNode
}) {
  const trail = [
    { to: ORGANIZATIONS_PATH, label: 'Organizations' },
    { to: organizationPath(organization.slug), label: organization.name },
  ]
  if (offered) {
    trail.push({ to: organizationSettingsPath(organization.slug), label: SETTINGS_NAME })
  }
  return (
    <>
      <Breadcrumb trail={trail} current={section} />
      <PageHeading>{section}</PageHeading>
      {offered && <SectionNav orgSlug={organization.slug} />}
      {children}
    </>
  )
}

/**
 * Everything under /organizations/:orgSlug/settings.
 *
 * For the organization's owners and admins. Anyone else is told so and is
 * shown nothing else, whichever section's address they opened. That is only
 * this app not showing it: what is here is either what every member is
 * already told about the organization, or is asked of an API that decides
 * for itself.
 *
 * The organization is the one the layout above read from the API, handed on
 * unchanged.
 */
export function SettingsLayout() {
  const organization = useOutletContext<OrganizationDetail>()

  if (!canManageOrganization(organization.role)) {
    return (
      <SettingsFrame organization={organization} section={SETTINGS_NAME} offered={false}>
        <p className="notice" role="note">
          {MANAGED_BY_OWNERS_AND_ADMINS}
        </p>
      </SettingsFrame>
    )
  }
  return <Outlet context={organization} />
}
