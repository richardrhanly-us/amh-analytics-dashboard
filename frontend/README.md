# SortView frontend

The SortView customer web app: a React + TypeScript single-page app built
with Vite. It is a static SPA. There is no server-side rendering.

**Status: Block F5.6, sorter navigation.** The app restores a session,
signs in and signs out against the customer API (`/api/auth/*`), lists the
signed-in user's organizations, lists each organization's sorting machines,
and shows a sorter's Live Today dashboard. It is not deployed anywhere yet:
there is no production hosting for this frontend. Nothing in this directory
is used by the Python backend, the Streamlit app or the collector.

## Routes

Routing is React Router in the browser, and exists only once signed in.
Signed out, every address shows the sign-in form and keeps its place, so
signing in lands on the page that was asked for.

| Address | Page |
| --- | --- |
| `/` | Redirects to `/organizations` |
| `/organizations` | The user's organizations (`GET /api/organizations`) |
| `/organizations/:orgSlug` | One organization and its sorting machines (`GET /api/organizations/{org_slug}`) |
| `/organizations/:orgSlug/sorters/:sorterSlug` | One sorter and its Live Today dashboard |
| `/organizations/:orgSlug/sorters/:sorterSlug/reports` | That sorter's reports over a range of days |
| `/organizations/:orgSlug/branches/:branchSlug` | The address a sorter used to have: redirects to the sorter hosted at that branch, or is not found |
| anything else | Not found |

Organizations and sorters appear in addresses by slug only. The API decides
what a user can see: an organization it does not return, and a sorter that is
not in the organization it returns, both get the same "not found" page as an
unknown address. A `401` from the API returns the app to the sign-in form.

## Sorting machines, host branches and destinations

Three things that are easy to confuse, and are kept apart:

- **A sorter** is a machine the organization runs SortView on. The
  organization page lists the `sorters` the API returns, which come from
  registered collector installations (retired ones excluded). It never lists
  branches as such, and never makes a sorter out of a routing destination.
- **A host branch** is where a sorter is. It is shown beside the sorter's
  name, and it is the scope the dashboard's reads are addressed by
  (`/api/organizations/{org}/branches/{host branch}/...`). A branch with no
  sorter has no dashboard.
- **A routing destination** is somewhere a sorter sends items. Destinations
  are configured per sorter and appear only inside that sorter's Routing
  figures. A place can be a destination of one sorter and the host of another;
  the two are unrelated here.

**Current limit: one separable sorter per host branch.** Events, pipeline
status and the routing configuration are stored per (organization, branch),
so the API returns one sorter entry per host branch and the sorter's slug is
its host branch's slug. An organization with sorters at several branches is
fully supported. Two machines at the *same* branch are shown as one sorter
whose figures are combined (`collector_count` above 1, and the page says so);
telling them apart needs per-installation attribution of events, which is
deferred.

## Reports

A sorter has two views, linked from under its name: **Live Today** and
**Reports**. Reports covers a range of days in five sections every member
sees, each read from its own endpoint under the sorter's host branch
(`.../reports/{overview|volume|routing|bins|reliability}?from=&to=`), in this
order:

- **Overview**: check-ins, average per day, active days, transit, rejects,
  busiest day, and a daily chart.
- **Volume & capacity**: averages per calendar day and per active day, the
  busiest day, weekday and hour, a typical week and a typical day.
- **Routing**: home, each configured destination in the API's order, and
  "Other" when there is any; daily transit.
- **Bin volume**: which physical sorter bins received check-ins. Known-bin
  check-ins, bins observed, bin coverage (known over all check-ins), a bar for
  each observed bin, and a bin-by-hour table. Only bins OBSERVED in the range
  are listed: the sorter's configured bin inventory is not stored, so a
  missing bin is not a zero. Check-ins with no recognized bin are a separate
  count, never a bin. A bin is its number ("Bin 0", "Bin 12") and nothing
  else: not how full it was, not a routing destination, not a kind of item.
- **Reliability**: rejects, the reject rate, daily rejects and reasons, under
  the API's own classification.

A sixth section, **Efficiency**, follows for the organization's owners and
admins only.

**The range.** Presets for the last 7, 30 and 90 days, or two dates. It opens
on the last 30 days ending today. Dates are calendar dates in the product's
time zone (from pipeline status), never the browser's. A range may not be
reversed, end after today, or cover more than 92 days; such a range is refused
before anything is asked of the API. The 92 days are what the API accepts at
present (`MAX_RANGE_DAYS` in `src/reports/dateRange.ts`), not a product rule.
A range that includes today says its figures will still rise. The range is
held by the page, not the address: reloading returns to the default.

**The arithmetic.** The API returns whole-number counts only. Every rate and
average is worked out in one place, `src/reports/derive.ts`, from totals: a
rate is one total over another, never an average of daily rates. A figure with
no denominator is shown as "Not available", never as 0, and a range with no
check-ins is an ordinary report.

**Loading and failure.** Each section loads on its own. One that fails says so
in its own panel with its own "Try again", and the other three stay. A `404`
shows "Reports are not available for this sorter yet." A `401` returns to the
sign-in form. Nothing is kept between ranges or sorters: a new range starts
empty, so one range's figures are never shown under another's dates.

**Charts** are plain SVG bars with no library. Each is one named image with a
sentence saying what it shows and a "Show table" button for the exact
figures; nothing depends on hovering.

## Live Today

The sorter page shows, for today: the pipeline's last reported state and when
it reported, and the day's figures in three groups: **Operations** (check-ins,
current hour, busiest hour), **Routing** (total in transit and one figure for
each destination configured for the site) and **Rejects** (total and rate),
followed by check-ins by hour and the reject reasons that occurred.

- **Routing.** `GET .../checkins/by-destination?date=` returns the day's
  check-ins as home, each configured destination in configured order (zero
  included) and everything else. The three always add up to the day's
  check-ins. Percentages are worked out in the browser, as a share of the
  day's check-ins. Destination names come only from the API: none is written
  into this app.

- **Hourly chart.** Check-ins by hour are drawn as a bar chart: plain SVG
  rendered by React, with no chart library. It stretches to its container, so
  it fits a phone without sideways scrolling, and labels fewer hours when
  narrow.
- **Not SVG-only.** The chart is a single named image, described by a sentence
  on the page (busiest hour, current hour, time zone). The exact 24 figures
  are a real table behind the "Show hourly table" button. The chart has no
  tooltips and nothing in it takes focus. Summary figures and reject reasons
  are a description list and a table.
- **Keyboard and screen readers.** Following a link, or Back and Forward,
  moves focus to the new page's heading; signing in, signing out and an
  expired session move it to the page content. Only what the person asked for
  is announced (pause, resume, a manual refresh); a timed refresh is silent.

- **"Today" is the product's day, not the browser's.** Pipeline status is read
  first and names the product time zone; the date and current hour are worked
  out in that zone and the five dated reads are made for that date. If
  pipeline status cannot be read, no date is guessed and nothing else is asked.
- **Refresh.** Everything refreshes together every 3 minutes, and from the
  Refresh button. Pause stops the timer only; Refresh still works while
  paused. A refresh after midnight in the product zone moves to the new day.
  A browser tab in the background does not refresh until it is visible again.
- **A sorter with no live data.** If the API answers `404` for a sorter the
  organization lists, the page says live data is not available for it yet.
- **Failures.** A section that cannot be loaded says so and the rest stay
  visible. Network failures and `5xx` answers are retried twice; nothing else
  is retried.

Data is fetched with TanStack Query and held in memory only, per signed-in
session.

## Requirements

- Node 24 (see `.nvmrc`; `package.json` declares `"engines": { "node": "^24.0.0" }`)
- npm (the lockfile is `package-lock.json`; do not use pnpm or yarn)

## Commands

Run these from this `frontend/` directory.

| Command | What it does |
| --- | --- |
| `npm ci` | Install exactly what `package-lock.json` records |
| `npm run dev` | Start the Vite dev server (http://localhost:5173) |
| `npm run lint` | Lint with ESLint |
| `npm run build` | Type-check (`tsc -b`), then build for production |
| `npm test` | Run the tests in watch mode (Vitest + jsdom) |
| `npm run test:run` | Run the tests once and exit (use this in CI) |

Tests stub `fetch`; none of them talks to a real backend.

## Build output

`npm run build` writes the production site to `dist/`. That directory is
git-ignored and is what will be uploaded to static hosting.

## Configuration

`.env.example` documents both variables.

- `VITE_API_BASE_URL`: prefix for API requests. Left empty, the app calls the
  API on its own origin under `/api`, which is the intended setup.
- `DEV_API_PROXY_TARGET`: local development only. When set, the Vite dev
  server forwards `/api` to that address. It is not prefixed with `VITE_`, so
  it is never bundled, and it has no effect on `npm run build`.

## Running against a local backend

The session is an HttpOnly cookie, so the page and the API must share an
origin. In development the Vite proxy provides that:

1. Start a **local** backend (for example on `http://127.0.0.1:8000`) with:

   ```
   SORTVIEW_CUSTOMER_ALLOWED_ORIGINS=http://localhost:5173
   SORTVIEW_CUSTOMER_COOKIE_SECURE=false
   ```

2. Put `DEV_API_PROXY_TARGET=http://127.0.0.1:8000` in `frontend/.env.local`.
3. Run `npm run dev` and open http://localhost:5173.

The browser only ever requests `http://localhost:5173/api/...`. Do not point
the proxy at production. Without a proxy target the app still loads, and
reports that it could not check the session.
