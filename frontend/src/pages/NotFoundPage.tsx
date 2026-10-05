import { Link } from 'react-router'

/**
 * The one page for everything that cannot be shown: an address this app does
 * not have, an organization or branch that does not exist, and one the user
 * may not see. It is identical in every case and names nothing from the
 * address, so it cannot be used to learn what exists.
 */
export function NotFoundPage() {
  return (
    <section aria-labelledby="not-found-heading">
      <h2 id="not-found-heading">Page not found</h2>
      <p>This page does not exist, or you do not have access to it.</p>
      <p>
        <Link to="/organizations">Go to organizations</Link>
      </p>
    </section>
  )
}
