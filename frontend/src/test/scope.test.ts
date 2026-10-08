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
      // R8B: the signed-in person's own account, and password reset -- all in api/account.ts.
      '/api/account',
      '/api/account/change-password',
      '/api/account/profile',
      '/api/auth/login',
      '/api/auth/logout',
      '/api/auth/password-reset/complete',
      '/api/auth/password-reset/request',
      '/api/auth/session',
      '/api/organizations',
      '/api/organizations/${encodeURIComponent(orgSlug)}',
      '/api/organizations/${segment(orgSlug)}/branches/${segment(branchSlug)}',
      // R8E: an organization's members -- the list, and under it the three changes and the recent changes -- all in
      // the one module that knows them (api/members.ts).
      '/api/organizations/${segment(orgSlug)}/members',
      // The organization's three range reports, built in the one module that reads them.
      '/api/organizations/${segment(orgSlug)}/reports/${kind}?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}',
      // An organization's Efficiency defaults, read and replaced by the one module that knows them (api/efficiency.ts).
      '/api/organizations/${segment(orgSlug)}/settings/efficiency',
      // R8H: an organization's routing, read and replaced by the one module that knows it (api/routingSettings.ts).
      '/api/organizations/${segment(orgSlug)}/settings/routing',
    ])
    expect(offenders(/\/settings\/routing[`'"]/)).toEqual(['../api/routingSettings.ts'])
    expect(offenders(/\/api\/organizations\/[^'"`]*\/members/)).toEqual(['../api/members.ts'])
    expect(offenders(/\/reports\/\$\{kind\}/).sort()).toEqual(['../api/organizationReports.ts', '../api/reports.ts'])

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
    // R9C: `transits` is now the plan feature read in pages/capabilities.ts; what stays banned is the old
    // operational endpoint, as a path.
    expect(offenders(/ingest[-_]?status|\/ingest\b|\/transits\b|heartbeat/i)).toEqual([])
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

  it('offers reports, a date range and Efficiency assumptions, and still no exports or administration', () => {
    // Dates are chosen in two places: the report range control, and a sorter's in-service date.
    expect(offenders(/type="date"/)).toEqual(['../reports/DateRangeControl.tsx'])
    expect(offenders(/'date' : 'text'/)).toEqual(['../reports/EfficiencyAssumptionsPanel.tsx'])
    expect(offenders(/datetime-local|download=|text\/csv|Blob\(|createObjectURL|\.pdf/)).toEqual([])
    // R8E: one thing is chosen from a list -- a member's role -- on the one page that manages members.
    expect(offenders(/<select/)).toEqual(['../members/MembersPage.tsx'])
    expect(offenders(/\b(Historical|Export|Administration)\b(?! dashboard data)/)).toEqual([])
    // R8F: an organization's owners and admins have one area for its own configuration. Its name is written once,
    // in the one module that names the area and its sections; everything of it is under settings/.
    expect(offenders(/\bSettings\b/)).toEqual(['../settings/settingsText.ts'])
    const settingsSources = shipped.filter(([path]) => path.startsWith('../settings/'))
    expect(settingsSources.map(([path]) => path).sort()).toEqual([
      '../settings/BranchesPage.tsx',
      '../settings/EfficiencySettingsPage.tsx',
      '../settings/GeneralPage.tsx',
      // R8H: the organization's routing -- its page, and the one hook that reads and replaces it.
      '../settings/RoutingPage.tsx',
      '../settings/SettingsLayout.tsx',
      '../settings/settingsText.ts',
      '../settings/useRoutingSettings.ts',
    ])
    // Nothing in it names what the API keeps to itself about a machine, or what R8F leaves out of an organization.
    expect(
      settingsSources
        .filter(([, text]) => /hostname|installation|token|enroll|ingest|credential|password|secret|plan_|entitlement|subscription|timezone|contact/i.test(text))
        .map(([path]) => path),
    ).toEqual([])
    // It asks the API for nothing of its own -- what it shows is the organization already read, and the one
    // Efficiency form that exists -- except routing, which has one hook, and that hook goes through the one API
    // module for it: no settings source names an API address or sends a request itself.
    expect(settingsSources.filter(([, text]) => /useQuery|useMutation/.test(text)).map(([path]) => path)).toEqual(['../settings/useRoutingSettings.ts'])
    expect(settingsSources.filter(([, text]) => /apiRequest|['"`]\/api\//.test(text)).map(([path]) => path)).toEqual([])
    // R8H: routing is the organization's TRANSIT routing and nothing else. A destination is a label and whether it
    // is enabled: it has no identifier of its own here, in what is read, typed or sent. Nothing of it is about a
    // branch's own configuration, and nothing is about the internal lists, which are another thing altogether.
    const routingSources = shipped.filter(([path]) => /routing/i.test(path) && (path.startsWith('../settings/') || path.startsWith('../api/')))
    expect(routingSources.map(([path]) => path).sort()).toEqual([
      '../api/routingSettings.ts',
      '../settings/RoutingPage.tsx',
      '../settings/useRoutingSettings.ts',
    ])
    expect(routingSources.filter(([, text]) => /internal|workflow|branch_services|collection_services|da_pattern/i.test(text)).map(([path]) => path)).toEqual([])
    expect(routingSources.filter(([, text]) => /\bkey\b(?!=)|destination_key|\bslug:/.test(text)).map(([path]) => path)).toEqual([])
    expect(routingSources.filter(([, text]) => /branch_settings|override|per-branch|branchSlug|collector/i.test(text)).map(([path]) => path)).toEqual([])
    // Nothing is reordered by dragging, and nothing is moved up or down.
    expect(routingSources.filter(([, text]) => /draggable|onDrag|onDrop|Move up|Move down/.test(text)).map(([path]) => path)).toEqual([])
    // General and the inventory are to be read: neither has a form, a field or a button.
    expect(
      settingsSources
        .filter(([path]) => /GeneralPage|BranchesPage/.test(path))
        .filter(([, text]) => /<form|<input|<select|<textarea|<button|onSubmit|onClick/.test(text))
        .map(([path]) => path),
    ).toEqual([])
    // R8B: a person can change and reset their OWN password, in the account pages and nowhere else. Nothing
    // about anyone else's account is offered, and no page has a password field for another person.
    expect(offenders(/\b(Reset password|Change password)\b/).sort()).toEqual([
      '../account/AccountPage.tsx',
      '../account/ResetPasswordPage.tsx',
    ])
    expect(offenders(/invit|\bMFA\b|two-factor|authenticator|change (your )?email|new email/i)).toEqual([])
    // R8E: an organization's owners and admins manage its members on one page. Its name is written once, and
    // everything about members is under members/ and in the one API module -- with no password anywhere in it.
    expect(offenders(/Users (&|and) access/i)).toEqual(['../members/memberText.ts'])
    const memberSources = shipped.filter(([path]) => path.startsWith('../members/') || path === '../api/members.ts')
    expect(memberSources.map(([path]) => path).sort()).toEqual([
      '../api/members.ts',
      '../members/MembersPage.tsx',
      '../members/memberText.ts',
      '../members/useMembers.ts',
    ])
    expect(memberSources.filter(([, text]) => /type="password"|PasswordField|password:|_password|user_id|member_id|: number/.test(text)).map(([path]) => path)).toEqual([])
    // Efficiency is shown as an ESTIMATE under assumptions -- a manual-workload equivalent and a labor-value
    // equivalent -- and never as time or money anyone is known to have been spared. None of these words is in the
    // app, in any form a person could read:
    expect(
      offenders(
        /\bROI\b|return on investment|payback|break[- ]?even|hours saved|labor savings|payroll savings|budget savings|net savings|cost savings|\bFTEs?\b|staff[- ]time equivalent|labor value|measured productivity|staff productivity/i,
      ),
    ).toEqual([])
    // The API also works out a net figure. Setting an estimate against a cost is not something this app shows yet:
    // the field is read and checked where it arrives (api/efficiency.ts) and appears nowhere a person reads.
    expect(offenders(/net operational value|net value/i)).toEqual([])
    expect(offenders(/net_operational_value/)).toEqual(['../api/efficiency.ts'])
    // Nothing is annualized, projected or dated from installation.
    expect(offenders(/annualiz|run[- ]rate|since[- ]install|useful life|amortiz/i)).toEqual([])
    const efficiencySources = shipped
      .filter(([path]) => /efficiency/i.test(path))
      .map(([, text]) => text)
      .join('\n')
    expect(efficiencySources).toMatch(/Estimated manual-workload equivalent/)
    expect(efficiencySources).toMatch(/Estimated labor-value equivalent/)
    expect(efficiencySources).toMatch(/Manual processing rate assumption/)
    expect(efficiencySources).toMatch(/Recurring cost for this period/)
    // The app supplies no manual rate or labor rate of its own: the old dashboard's figures are nowhere in it.
    expect(efficiencySources).not.toMatch(/\b45(\.0)?\b|17\.56|\b130\b|8400|118003/)
    // Anything that diagnoses or advises is still not in this app.
    expect(offenders(/top issues|recommended attention|correlat|caused by|exception bin|estimated holds/i)).toEqual([])
    // Bin volume (Reports R7B) counts check-ins by bin and says nothing more about a bin: it is not called
    // utilization or routing, and no bin is an overflow, an exception or a place for holds.
    expect(offenders(/bin utili[sz]ation|bin routing|bin_utili|overflow bin|estimated hold|hold shelf/i)).toEqual([])
    const binVolume = shipped.find(([path]) => path === '../reports/BinVolumeSection.tsx')?.[1] ?? ''
    expect(binVolume).toMatch(/heading="Bin volume"/)
    // A bin's label is made in one place, from its key alone; no bin number is written into the section.
    expect(binVolume).toMatch(/const binLabel = \(bin: BinVolumeBin\) => `Bin \$\{bin\.key\}`/)
    expect(binVolume).not.toMatch(/key === ['"]\d|=== 7\b|length === 7|Bin [0-9]/)
    // It is every member's report, and a bin is not a destination or a measure of fullness.
    expect(binVolume).not.toMatch(/admin|owner|capacity|destination|exception|overflow/i)
    // Holds (R8K) is two counts, read in one module and shown in one section, only where the plan has it: the
    // plan's feature is looked at in one place, and no hold is shown by patron, item or destination.
    expect(offenders(/public_hold_count|ill_hold_count/).sort()).toEqual(['../api/reports.ts', '../reports/HoldsSection.tsx'])
    expect(offenders(/internal_workflow/)).toEqual(['../pages/SorterReportsPage.tsx'])
    // R9C: transit routing and the history window are read from the plan in one module, by feature key and never
    // by the plan's name, and nothing else reads an organization's entitlements directly.
    expect(offenders(/history_days|entitlements\.transits/)).toEqual(['../pages/capabilities.ts'])
    expect(offenders(/entitlements\.\w/).sort()).toEqual(['../pages/SorterReportsPage.tsx', '../pages/capabilities.ts'])
    expect(offenders(/\bstarter\b|\benterprise\b|plan_code\s*===|plan_name\s*===/i)).toEqual([])
    const holds = shipped.find(([path]) => path === '../reports/HoldsSection.tsx')?.[1] ?? ''
    expect(holds).toMatch(/heading="Holds"/)
    expect(holds).not.toMatch(/patron_id|barcode|item_key|transit_|is_ill|is_branch|is_collection|programming|canSee|role/)
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
    // Cards wrap whole: each has a least width (never more than the screen's), and a row takes as many as fit.
    expect(css).toMatch(/\.metrics \{[^}]*grid-template-columns: repeat\(auto-fill, minmax\(min\(100%, 11\.5rem\), 1fr\)\)/)
    // One shell width for every page -- a dashboard's, with gutters -- and a line of reading that does not grow with it.
    expect(css).toMatch(/--shell-width: 90rem;/)
    expect(css).toMatch(/\.app \{[^}]*max-width: var\(--shell-width\);[^}]*margin: 0 auto;[^}]*padding: 2rem var\(--shell-gutter\) 3rem;/)
    expect(css.match(/var\(--shell-width\)/g)).toHaveLength(1)
    expect(css).toMatch(/\.quiet,[^{]*\.estimate-caveat,[^{]*\{\s*max-width: var\(--prose-width\);/)
    // Cards that hold money are wide enough for it, and never wider than a narrow screen.
    expect(css).toMatch(/\.metrics-wide \{\s*grid-template-columns: repeat\(auto-fill, minmax\(min\(100%, [\d.]+rem\), 1fr\)\);/)
    expect(css).toMatch(/\.live-controls \{[^}]*flex-wrap: wrap/)
    expect(css).toMatch(/\.live-buttons \{[^}]*flex-wrap: wrap/)
    expect(css).toMatch(/\.breadcrumb ol \{[^}]*flex-wrap: wrap/)
    // No length in pixels except hairlines and the 1px box that hides text from sight.
    const pixels = (css.match(/\b\d+(\.\d+)?px\b/g) ?? []).filter((length) => !['1px', '3px', '2px'].includes(length))
    expect(pixels).toEqual([])
    // No element is given a width, or a least width, that a narrow screen could not hold. One box may scroll
    // sideways: the one a wide table sits in, so that the page itself never has to.
    expect(css.match(/overflow-x:/g)).toHaveLength(1)
    expect(css).toMatch(/\.table-scroll \{\s*overflow-x: auto;\s*\}/)
    const withoutTableScroll = css.replace(/\.table-scroll \{[^}]*\}/, '')
    expect(withoutTableScroll).not.toMatch(/^\s*min-width:\s*[1-9]|^\s*width:\s*\d{2,}(rem|em|px)|overflow-x:\s*(scroll|auto)/m)
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
      // The heading of a view that replaces another at the same address: sign in / forgot password / reset result.
      '../components/ArrivalHeading.tsx',
      '../components/PageHeading.tsx',
      // The Members heading takes focus when a member has been removed and the row that had focus is gone.
      '../members/MembersPage.tsx',
      // A report section's heading takes focus when its "Try again" succeeds and the button goes.
      '../reports/ReportSections.tsx',
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
    // R8F: one module in the whole app reads it, and reads it as what it is -- the inventory of branches. It lists
    // a machine only from the sorters the API returned, matched by the machine's own host branch.
    expect(offenders(/organization\.branches|\.branches\.|BranchSummary/).filter((path) => path !== '../api/organizations.ts')).toEqual([
      '../settings/BranchesPage.tsx',
    ])
    const inventory = shipped.find(([path]) => path === '../settings/BranchesPage.tsx')?.[1] ?? ''
    expect(inventory).toMatch(/organization\.sorters\.find\(\(sorter\) => sorter\.host_branch\.slug === branch\.slug\)/)
    expect(pages.filter(([, text]) => /organization\.sorters/.test(text)).map(([path]) => path).sort()).toEqual([
      '../pages/LegacyBranchRedirect.tsx',
      '../pages/OrganizationPage.tsx',
      '../pages/SorterLayout.tsx',
    ])
    // One dashboard page, at the sorter address; the old branch address only redirects to it.
    const router = shipped.find(([path]) => path === '../router/AppRouter.tsx')?.[1] ?? ''
    expect(router.match(/<Route path="[^"]*"/g)).toEqual([
      // R8B: the signed-in person's own account, beside the organizations and under none of them.
      '<Route path="account"',
      '<Route path="organizations"',
      '<Route path="organizations/:orgSlug"',
      // The organization's own reports, beside its sorters and under none of them.
      '<Route path="reports"',
      // R8E: who belongs to the organization, beside its reports.
      '<Route path="members"',
      // R8F: the organization's own configuration, a section to an address.
      '<Route path="settings"',
      '<Route path="general"',
      '<Route path="branches"',
      // R8H: the organization's routing, between its branches and its Efficiency defaults.
      '<Route path="routing"',
      '<Route path="efficiency"',
      '<Route path="sorters/:sorterSlug"',
      '<Route path="reports"',
      '<Route path="branches/:branchSlug"',
      '<Route path="*"',
    ])
    expect(offenders(/<LiveToday\s/).sort()).toEqual(['../pages/SorterPage.tsx'])
    // One reports page too, under the same sorter address.
    expect(offenders(/<SorterReports\s/).sort()).toEqual(['../pages/SorterReportsPage.tsx'])
    expect(offenders(/<OrganizationReports\s/).sort()).toEqual(['../pages/OrganizationReportsPage.tsx'])
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
    // The zones are bands with an accent each, and the pipeline's state is a pill in its own colour. Every one of
    // them is beside a word that says the same thing, and none of it moves.
    expect(css).toMatch(/--zone-operations: light-dark\(/)
    expect(css).toMatch(/--zone-routing: light-dark\(/)
    expect(css).toMatch(/--zone-rejects: light-dark\(/)
    expect(liveToday).toMatch(/\{live\.paused \? 'Paused' : 'Live'\}/)
    expect(css).toMatch(/\.live-today \.metrics \{\s*grid-template-columns: repeat\(auto-fit, minmax\(min\(100%, 11\.5rem\), 1fr\)\);/)
  })

  it('adds no theme switch: light and dark still follow the system, as before', () => {
    expect(offenders(/data-theme|theme-toggle|ThemeProvider|prefers-color-scheme/i)).toEqual([])
    expect(css).toMatch(/color-scheme: light dark/)
  })
})
