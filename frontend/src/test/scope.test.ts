import { describe, expect, it } from 'vitest'

import packageJson from '../../package.json?raw'
import css from '../index.css?raw'

/**
 * Guards on what this block is allowed to contain. They read the shipped
 * source as text, so they look for specific things that must not be there and
 * say nothing about formatting.
 */

const allSources = import.meta.glob<string>('../**/*.{ts,tsx,css}', { query: '?raw', import: 'default', eager: true })

// The code a browser runs: everything under src/ except tests and test helpers.
const shipped = Object.entries(allSources).filter(([path]) => !path.includes('.test.') && !path.startsWith('./'))

function offenders(pattern: RegExp): string[] {
  return shipped.filter(([, text]) => pattern.test(text)).map(([path]) => path)
}

const manifest = JSON.parse(packageJson) as {
  dependencies?: Record<string, string>
  devDependencies?: Record<string, string>
}
const installed = [...Object.keys(manifest.dependencies ?? {}), ...Object.keys(manifest.devDependencies ?? {})]

describe('scope of this block', () => {
  it('finds the shipped source to check', () => {
    const paths = shipped.map(([path]) => path)

    expect(paths).toContain('../api/client.ts')
    expect(paths).toContain('../auth/AuthProvider.tsx')
    expect(paths).toContain('../components/LoginForm.tsx')
    expect(paths.some((path) => path.includes('test'))).toBe(false)
  })

  it.each([
    ['axios', /^axios$/],
    ['a state library', /^(redux|@reduxjs\/.*|react-redux|zustand|jotai|mobx.*|recoil)$/],
    ['a UI or chart framework', /tailwind|^@mui\/|bootstrap|chart|recharts|^d3|visx|nivo|victory|plotly/],
    ['a date library', /^(moment.*|dayjs|date-fns.*|luxon|@js-joda\/.*|spacetime|temporal-polyfill|@date-io\/.*)$/],
    ['a browser test runner', /playwright|cypress/],
    ['a component library', /^@radix-ui\/|^@headlessui\/|^@chakra-ui\/|^antd$|^@mantine\/|^react-aria|^@emotion\/|styled-components|^sass$|^less$|postcss/],
    ['an icon library', /icon|lucide|fontawesome|feather|phosphor/],
    ['an accessibility test library', /axe|pa11y|lighthouse/],
  ])('does not depend on %s', (_label, pattern) => {
    expect(installed.filter((name) => pattern.test(name))).toEqual([])
  })

  it('runs on exactly the four libraries it had before the dashboard was drawn', () => {
    expect(Object.keys(manifest.dependencies ?? {}).sort()).toEqual(['@tanstack/react-query', 'react', 'react-dom', 'react-router'])
  })

  it('imports none of those libraries either', () => {
    expect(offenders(/from\s+['"](axios|redux|zustand|moment|dayjs|date-fns|luxon|chart|recharts|d3)/)).toEqual([])
  })

  it('uses TanStack Query for data, and keeps its cache in memory only', () => {
    expect(installed.filter((name) => name.startsWith('@tanstack/'))).toEqual(['@tanstack/react-query'])
    expect(offenders(/from\s+['"]@tanstack\/react-query['"]/)).toContain('../liveToday/useLiveToday.ts')
    expect(offenders(/persistQueryClient|Persister|dehydrate|hydrate\(/i)).toEqual([])
  })

  it('routes with react-router, and with nothing hand-rolled', () => {
    expect(installed).toContain('react-router')
    expect(installed.filter((name) => /router/.test(name))).toEqual(['react-router'])
    expect(offenders(/from\s+['"]react-router['"]/)).toContain('../router/AppRouter.tsx')
    expect(offenders(/\bhistory\.|pushState|replaceState|window\.location|location\.href/)).toEqual([])
  })

  it('calls fetch only from the API client', () => {
    expect(offenders(/\bfetch\s*\(/)).toEqual(['../api/client.ts'])
    expect(offenders(/XMLHttpRequest|sendBeacon|WebSocket|EventSource/)).toEqual([])
  })

  it('names no host: every request is a relative /api path', () => {
    expect(offenders(/https?:\/\//)).toEqual([])
    expect(offenders(/sortview\.com|digitalocean|ondigitalocean|neon\.tech|amazonaws|cloudfront/i)).toEqual([])
  })

  it('uses no browser storage', () => {
    expect(offenders(/localStorage|sessionStorage|indexedDB/)).toEqual([])
  })

  it('never touches document.cookie', () => {
    expect(offenders(/\.cookie\b/)).toEqual([])
  })

  it('invents no token header and never sends cross-site credentials', () => {
    expect(offenders(/Authorization|Bearer|X-Api-Key|X-CSRF/i)).toEqual([])
    expect(offenders(/credentials:\s*['"]include['"]/)).toEqual([])
  })

  it('logs nothing', () => {
    expect(offenders(/console\./)).toEqual([])
  })

  it('calls no API beyond auth, the two organization endpoints and the six Live Today reads', () => {
    const paths = shipped.flatMap(([, text]) => text.match(/['"`]\/api\/[^'"`]*['"`]/g) ?? [])

    expect([...new Set(paths.map((path) => path.slice(1, -1)))].sort()).toEqual([
      '/api/',
      '/api/auth/login',
      '/api/auth/logout',
      '/api/auth/session',
      '/api/organizations',
      '/api/organizations/${encodeURIComponent(orgSlug)}',
      '/api/organizations/${segment(orgSlug)}/branches/${segment(branchSlug)}',
    ])

    // Under a branch: exactly the six Live Today reads, each named once, in the one module that makes them.
    const endpoints = shipped.flatMap(([path, text]) =>
      (text.match(/['"`](pipeline-status|checkins\/[a-z-]+|rejects\/[a-z-]+)['"`]/g) ?? []).map(
        (found) => `${path} ${found.slice(1, -1)}`,
      ),
    )
    expect(endpoints.filter((found) => found.startsWith('../api/liveToday.ts ')).sort()).toEqual([
      '../api/liveToday.ts checkins/by-destination',
      '../api/liveToday.ts checkins/by-hour',
      '../api/liveToday.ts checkins/count',
      '../api/liveToday.ts rejects/by-reason',
      '../api/liveToday.ts rejects/count',
    ])
    expect(offenders(/\/pipeline-status/)).toEqual(['../api/liveToday.ts'])
  })

  it('never uses the older ingest-status endpoint, or any other operational one', () => {
    expect(offenders(/ingest[-_]?status|\/ingest\b|transits|heartbeat/i)).toEqual([])
  })

  it('judges nothing by age: there is no staleness threshold', () => {
    expect(offenders(/STALE_|stale_?after|stale_?threshold|max_?age|isStale|staleTime/i)).toEqual([])
  })

  it('builds addresses from slugs, never from database ids', () => {
    expect(offenders(/customer_id|branch_id|organization_id|tenant_id|org_id|\.id\b/)).toEqual([])
    const paths = shipped.find(([path]) => path === '../router/paths.ts')?.[1] ?? ''
    expect(paths).toMatch(/encodeURIComponent\(orgSlug\)/)
    expect(paths).toMatch(/encodeURIComponent\(sorterSlug\)/)
    expect(paths).not.toMatch(/\d/)
  })

  it('offers nothing beyond today: no other dates, reports, exports or administration', () => {
    expect(offenders(/type="date"|datetime-local|<select|download=|text\/csv|Blob\(|createObjectURL/)).toEqual([])
    expect(offenders(/\b(Overview|Reports?|Historical|Export|Administration|Settings|Reset password|Change password)\b(?! dashboard data)/)).toEqual([])
  })
})

describe('how the dashboard is drawn', () => {
  const chart = shipped.find(([path]) => path === '../liveToday/HourlyCheckinsChart.tsx')?.[1] ?? ''
  const markup = shipped.filter(([path]) => path.endsWith('.tsx'))

  it('draws the chart as SVG that stretches, and measures nothing', () => {
    expect(chart).toMatch(/<svg viewBox=/)
    expect(chart).toMatch(/preserveAspectRatio="none"/)
    expect(offenders(/ResizeObserver|getBoundingClientRect|offsetWidth|clientWidth|innerWidth|matchMedia|addEventListener\(\s*['"]resize/)).toEqual([])
    expect(offenders(/<canvas|<img\b|\.(png|jpe?g|gif|webp|svg)['"]/)).toEqual([])
  })

  it('gives the chart no hover-only details and no bar anyone can focus', () => {
    expect(chart).not.toMatch(/onMouse|onPointer|onFocus|onClick|onKey|tabIndex|<title|\btitle=/)
    expect(offenders(/\btitle=|onMouseEnter|onMouseOver|onPointerEnter|:hover/)).toEqual([])
  })

  it('lays out with grid and flex that reflow, never a fixed desktop width', () => {
    expect(css).toMatch(/\.metrics \{[^}]*grid-template-columns: repeat\(auto-fill, minmax\([\d.]+rem, 1fr\)\)/)
    expect(css).toMatch(/\.live-controls \{[^}]*flex-wrap: wrap/)
    expect(css).toMatch(/\.live-buttons \{[^}]*flex-wrap: wrap/)
    expect(css).toMatch(/\.breadcrumb ol \{[^}]*flex-wrap: wrap/)
    // No length in pixels except hairlines and the 1px box that hides text from sight.
    const pixels = (css.match(/\b\d+(\.\d+)?px\b/g) ?? []).filter((length) => !['1px', '3px', '2px'].includes(length))
    expect(pixels).toEqual([])
    // No element is given a width, or a least width, that a narrow screen could not hold.
    expect(css).not.toMatch(/^\s*min-width:\s*[1-9]|^\s*width:\s*\d{2,}(rem|em|px)|overflow-x:\s*(scroll|auto)/m)
    expect(css).not.toMatch(/\b\d+vw\b/)
  })

  it('keeps a visible focus ring on everything, and never removes an outline', () => {
    expect(css).toMatch(/^:focus-visible \{\s*outline: 3px solid var\(--accent\);\s*outline-offset: 2px;/m)
    expect(css).not.toMatch(/outline:\s*(none|0)\b|outline-width:\s*0\b|outline-style:\s*none/)
    expect(css).not.toMatch(/:focus(?!-visible)/)
  })

  it('moves nothing: no animation and no transition, so none to turn off', () => {
    const moving = /animation|transition|@keyframes|scroll-behavior:\s*smooth/
    // If motion is ever added, it must come with a reduced-motion rule.
    expect(moving.test(css) && !/prefers-reduced-motion:\s*reduce/.test(css)).toBe(false)
    expect(css).not.toMatch(moving)
    expect(offenders(/requestAnimationFrame|\.animate\(/)).toEqual([])
  })

  it('sets no tab order of its own and makes no control out of a div or span', () => {
    expect(offenders(/tabIndex=\{\s*[1-9]|tabIndex="[1-9]|tabindex="[1-9]/)).toEqual([])
    expect(markup.filter(([, text]) => /tabIndex=\{-1\}/.test(text)).map(([path]) => path).sort()).toEqual([
      '../App.tsx',
      '../components/PageHeading.tsx',
    ])
    expect(offenders(/<(div|span|p|li|td|th|tr|svg|rect|section)\b[^>]*\bon(Click|KeyDown|KeyUp|KeyPress)=/)).toEqual([])
    expect(offenders(/role="(button|link|tab|menuitem|checkbox|switch)"/)).toEqual([])
    expect(offenders(/aria-live=/)).toEqual([])
    // A busy button is aria-disabled and ignores the click: a `disabled` one would drop keyboard focus.
    expect(offenders(/\sdisabled=/)).toEqual([])
  })

  it('has one timer, the refresh interval, with no countdown and nothing to configure', () => {
    expect(offenders(/setInterval|setTimeout|Date\.now\(|countdown|secondsLeft|remaining/i)).toEqual([])
    expect(offenders(/refetchInterval/)).toEqual(['../liveToday/useLiveToday.ts'])
    expect(offenders(/REFRESH_INTERVAL_MS\s*=/)).toEqual(['../liveToday/useLiveToday.ts'])
  })

  it('names no destination and no library of its own: routing comes from the API', () => {
    expect(offenders(/westside|library express|library_express|braunfels|nbpl|ultrasort|tech logic/i)).toEqual([])
  })

  it('treats a destination as an outcome, never as a place to go: no routing card links anywhere', () => {
    const liveToday = shipped.find(([path]) => path === '../liveToday/LiveToday.tsx')?.[1] ?? ''
    expect(liveToday).not.toMatch(/<Link\b|<a\b|href=|branchPath|useNavigate/)
    // No installation-level address or attribution: a sorter is addressed by its site, never by a machine id.
    expect(offenders(/installation_?id|installations?\//i)).toEqual([])
  })

  it('lists sorting machines from the sorters the API returns, never from branches or destinations', () => {
    const pages = shipped.filter(([path]) => path.startsWith('../pages/') || path.startsWith('../router/'))
    // Nothing that draws a page or builds an address reads the organization's branch list.
    expect(pages.filter(([, text]) => /organization\.branches|\.branches\.|BranchSummary/.test(text)).map(([path]) => path)).toEqual([])
    expect(pages.filter(([, text]) => /organization\.sorters/.test(text)).map(([path]) => path).sort()).toEqual([
      '../pages/LegacyBranchRedirect.tsx',
      '../pages/OrganizationPage.tsx',
      '../pages/SorterPage.tsx',
    ])
    // One dashboard page, at the sorter address; the old branch address only redirects to it.
    const router = shipped.find(([path]) => path === '../router/AppRouter.tsx')?.[1] ?? ''
    expect(router.match(/<Route path="[^"]*"/g)).toEqual([
      '<Route path="organizations"',
      '<Route path="organizations/:orgSlug"',
      '<Route path="sorters/:sorterSlug"',
      '<Route path="branches/:branchSlug"',
      '<Route path="*"',
    ])
    expect(offenders(/<LiveToday\s/).sort()).toEqual(['../pages/SorterPage.tsx'])
    expect(allSources['../pages/BranchPage.tsx']).toBeUndefined()
  })

  it('groups today as three named groups without adding landmarks', () => {
    const liveToday = shipped.find(([path]) => path === '../liveToday/LiveToday.tsx')?.[1] ?? ''
    expect(liveToday.match(/<SummaryGroup name="(\w+)"/g)).toEqual([
      '<SummaryGroup name="operations"',
      '<SummaryGroup name="routing"',
      '<SummaryGroup name="rejects"',
    ])
    expect(liveToday).toMatch(/role="group" aria-labelledby=/)
    expect(css).toMatch(/\.metric dt \{[^}]*overflow-wrap: anywhere/)
  })

  it('adds no theme switch: light and dark still follow the system, as before', () => {
    expect(offenders(/data-theme|theme-toggle|ThemeProvider|prefers-color-scheme/i)).toEqual([])
    expect(css).toMatch(/color-scheme: light dark/)
  })
})
