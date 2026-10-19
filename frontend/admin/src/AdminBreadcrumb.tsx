import { Link } from 'react-router'

/**
 * The way back up from an admin view: links to the views above it, then the view itself, marked as the current one.
 *
 * The admin app's copy of the customer app's Breadcrumb (the boundary keeps the admin app off the customer
 * components), with the same markup, so it shares the `.breadcrumb` styles and their decorative separator.
 */
export function AdminBreadcrumb({ trail, current }: { trail: Array<{ to: string; label: string }>; current: string }) {
  return (
    <nav aria-label="Breadcrumb" className="breadcrumb">
      <ol>
        {trail.map((step) => (
          <li key={step.to}>
            <Link to={step.to}>{step.label}</Link>
          </li>
        ))}
        <li>
          <span aria-current="page">{current}</span>
        </li>
      </ol>
    </nav>
  )
}
