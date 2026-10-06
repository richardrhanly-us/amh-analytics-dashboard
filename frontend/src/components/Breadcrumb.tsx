import { Link } from 'react-router'

/** The way back up from a page: links to the pages above it, then the page itself, marked as the current one. */
export function Breadcrumb({ trail, current }: { trail: Array<{ to: string; label: string }>; current: string }) {
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
