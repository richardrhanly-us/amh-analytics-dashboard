import { Link, useOutletContext } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { OrganizationDefaults } from '../reports/EfficiencyAssumptionsPanel.tsx'
import { sorterReportsPath } from '../router/paths.ts'
import { SettingsFrame } from './SettingsLayout.tsx'
import { EFFICIENCY_NAME } from './settingsText.ts'

/**
 * /organizations/:orgSlug/settings/efficiency -- the organization's
 * Efficiency defaults, and the way to each sorting machine's own figures.
 *
 * The defaults are the same form, with the same rules and the same words, as
 * beside every machine's Efficiency report: it is that form, shown here too,
 * and not a second one. A machine's own figures stay beside its report, where
 * the effect of changing them can be seen; this page only leads there.
 *
 * A suspended organization's defaults can be read and not changed, so they
 * are shown as they are stored, with nothing to save them with.
 */
export function EfficiencySettingsPage() {
  const organization = useOutletContext<OrganizationDetail>()
  const writable = organization.access_mode === 'full'

  return (
    <SettingsFrame organization={organization} section={EFFICIENCY_NAME}>
      <div className="settings-sections">
        {!writable && (
          <p className="notice" role="note">
            This organization is suspended, so its Efficiency assumptions cannot be changed.
          </p>
        )}

        <section className="panel settings-panel" aria-labelledby="efficiency-organization-heading">
          <p className="quiet">
            These are assumptions, entered by your organization. Values are stored exactly as entered and are never
            rounded.
          </p>
          <OrganizationDefaults orgSlug={organization.slug} headingLevel="h3" readOnly={!writable} />
        </section>

        <section className="panel settings-panel" aria-labelledby="efficiency-sorters-heading">
          <h3 id="efficiency-sorters-heading">Sorting machine overrides</h3>
          <p className="quiet">
            A sorting machine can have its own rates, costs and in-service date. They are entered beside that
            machine&rsquo;s Efficiency report.
          </p>
          {organization.sorters.length === 0 ? (
            <p>No sorting machines are registered for this organization yet.</p>
          ) : (
            <ul className="nav-list" aria-labelledby="efficiency-sorters-heading">
              {organization.sorters.map((sorter) => (
                <li key={sorter.slug}>
                  <Link className="nav-list-link" to={sorterReportsPath(organization.slug, sorter.slug)}>
                    {sorter.name} reports
                  </Link>
                  <span className="nav-list-meta">{sorter.host_branch.name}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </SettingsFrame>
  )
}
