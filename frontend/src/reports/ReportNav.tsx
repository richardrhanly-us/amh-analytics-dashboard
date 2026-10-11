import { NavLink, useLocation } from 'react-router'

/**
 * The views of a Reports page, as a strip of links: each has an address, so
 * each is a link, and the current one says so. Every link keeps the address's
 * query, so the range chosen in one view is the range of the next.
 *
 * With only one view there is nothing to choose between, and no strip.
 */
export function ReportNav({ views }: { views: ReadonlyArray<{ to: string; label: string }> }) {
  const { search } = useLocation()

  if (views.length < 2) {
    return null
  }
  return (
    <nav aria-label="Report views" className="view-nav">
      <ul>
        {views.map((view) => (
          <li key={view.to}>
            <NavLink to={{ pathname: view.to, search }} end>
              {view.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
