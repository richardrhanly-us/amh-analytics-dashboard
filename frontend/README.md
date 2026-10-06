# SortView frontend

The SortView customer web app: a React + TypeScript single-page app built
with Vite. It is a static SPA. There is no server-side rendering.

**Status: Block F5, Live Today presentation.** The app restores a session,
signs in and signs out against the customer API (`/api/auth/*`), lists the
signed-in user's organizations, lets them choose a branch, and shows that
branch's Live Today dashboard. It is not deployed anywhere yet: there is no
production hosting for this frontend. Nothing in this directory is used by
the Python backend, the Streamlit app or the collector.

## Routes

Routing is React Router in the browser, and exists only once signed in.
Signed out, every address shows the sign-in form and keeps its place, so
signing in lands on the page that was asked for.

| Address | Page |
| --- | --- |
| `/` | Redirects to `/organizations` |
| `/organizations` | The user's organizations (`GET /api/organizations`) |
| `/organizations/:orgSlug` | One organization and its branches (`GET /api/organizations/{org_slug}`) |
| `/organizations/:orgSlug/branches/:branchSlug` | The selected branch and its Live Today dashboard |
| anything else | Not found |

Organizations and branches appear in addresses by slug only. The API decides
what a user can see: an organization it does not return, and a branch that is
not in the organization it returns, both get the same "not found" page as an
unknown address. A `401` from the API returns the app to the sign-in form.

## Live Today

The branch page shows, for today: the pipeline's last reported state and when
it reported, check-ins (total, current hour, busiest hour, and by hour),
rejects (total and rate) and the reject reasons that occurred.

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
  out in that zone and the four dated reads are made for that date. If
  pipeline status cannot be read, no date is guessed and nothing else is asked.
- **Refresh.** Everything refreshes together every 3 minutes, and from the
  Refresh button. Pause stops the timer only; Refresh still works while
  paused. A refresh after midnight in the product zone moves to the new day.
  A browser tab in the background does not refresh until it is visible again.
- **A branch with no live data.** If the API answers `404` for a branch the
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
