import { useOutletContext } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { roleLabel } from '../pages/labels.ts'
import { SettingsFrame } from './SettingsLayout.tsx'
import { GENERAL_NAME } from './settingsText.ts'

/**
 * /organizations/:orgSlug/settings/general -- what the organization is, as
 * the API describes it: its name, whether it is active, and the role the
 * signed-in person holds in it.
 *
 * To be read, not changed: there is nothing here to type in and nothing to
 * save. Only these three things are shown; nothing else the API says of an
 * organization belongs on this page.
 */
export function GeneralPage() {
  const organization = useOutletContext<OrganizationDetail>()

  return (
    <SettingsFrame organization={organization} section={GENERAL_NAME}>
      <section className="panel settings-panel" aria-labelledby="general-organization-heading">
        <h3 id="general-organization-heading">Organization</h3>
        <dl className="account-facts">
          <div>
            <dt>Organization name</dt>
            <dd>{organization.name}</dd>
          </div>
          <div>
            <dt>Status</dt>
            {/* In words. A suspended organization can be read and not changed; the notice above the page says so. */}
            <dd>{organization.access_mode === 'full' ? 'Active' : 'Suspended'}</dd>
          </div>
          <div>
            <dt>Your role</dt>
            {/* A role this app does not know gets no label rather than the API's own word for it. */}
            <dd>{roleLabel(organization.role) ?? 'Not available'}</dd>
          </div>
        </dl>
      </section>
    </SettingsFrame>
  )
}
