import { Link, useOutletContext } from 'react-router'

import type { OrganizationDetail, SorterSummary } from '../api/organizations.ts'
import { sorterStatusLabel } from '../pages/labels.ts'
import { sorterPath } from '../router/paths.ts'
import { SettingsFrame } from './SettingsLayout.tsx'
import { BRANCHES_NAME } from './settingsText.ts'

const NOT_APPLICABLE = 'Not applicable'

interface Row {
  /** The branch's name, as the API gives it. */
  branch: string
  primary: boolean
  /** The sorting machine installed at the branch, when the API lists one there. */
  sorter: SorterSummary | null
  /** Keeps a row its own; never shown. */
  key: string
}

/**
 * One row for each branch the API lists, with the sorting machine it says is
 * installed there -- matched by the machine's own host branch, which is how
 * the API relates the two. A branch with none says so.
 *
 * Nothing is inferred: a machine is shown only because the API listed it,
 * and a machine whose branch is not in the branch list is still shown, on a
 * row of its own, rather than dropped.
 */
function inventory(organization: OrganizationDetail): Row[] {
  const rows: Row[] = organization.branches.map((branch) => ({
    branch: branch.name,
    primary: branch.is_primary,
    sorter: organization.sorters.find((sorter) => sorter.host_branch.slug === branch.slug) ?? null,
    key: `branch:${branch.slug}`,
  }))
  for (const sorter of organization.sorters) {
    if (!rows.some((row) => row.sorter === sorter)) {
      rows.push({ branch: sorter.host_branch.name, primary: false, sorter, key: `sorter:${sorter.slug}` })
    }
  }
  return rows
}

function collectors(sorter: SorterSummary): string {
  // More than one cannot be told apart: said in words, as beside the machine's own reports.
  return sorter.collector_count > 1 ? `${sorter.collector_count} — their figures are combined` : String(sorter.collector_count)
}

/**
 * /organizations/:orgSlug/settings/branches -- the organization's branches,
 * and which of them has a sorting machine.
 *
 * An inventory, to be read: nothing here adds, changes or removes a branch or
 * a machine. It is the one page that reads the organization's branch list,
 * and it reads it as a list of branches -- a machine is never made out of one.
 */
export function BranchesPage() {
  const organization = useOutletContext<OrganizationDetail>()
  const rows = inventory(organization)

  return (
    <SettingsFrame organization={organization} section={BRANCHES_NAME}>
      <section className="panel settings-panel" aria-labelledby="branches-heading">
        <h3 id="branches-heading">Branches and sorting machines</h3>
        {rows.length === 0 ? (
          <p>No branches are registered for this organization yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table text-table" aria-labelledby="branches-heading">
              <thead>
                <tr>
                  <th scope="col">Branch</th>
                  <th scope="col">Sorting machine</th>
                  <th scope="col">Status</th>
                  <th scope="col">Collectors</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ key, branch, primary, sorter }) => (
                  <tr key={key}>
                    <th scope="row">
                      {branch}
                      {primary && ' · Primary'}
                    </th>
                    <td>{sorter === null ? 'No sorting machine' : <Link to={sorterPath(organization.slug, sorter.slug)}>{sorter.name}</Link>}</td>
                    {/* The machine's state, in the words used for it everywhere. A branch with no machine has none. */}
                    <td>{sorter === null ? NOT_APPLICABLE : (sorterStatusLabel(sorter.status) ?? 'Active')}</td>
                    <td>{sorter === null ? NOT_APPLICABLE : collectors(sorter)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="quiet">Branches and sorting machines are set up by SortView support and cannot be changed here.</p>
      </section>
    </SettingsFrame>
  )
}
