# SortView frontend

The SortView customer web app: a React + TypeScript single-page app built
with Vite. It is a static SPA. There is no server-side rendering.

**Status: Block F2, authentication shell.** The app restores a session, signs
in and signs out against the customer API (`/api/auth/*`), and shows a
placeholder once signed in. There is no routing, organization or branch
selection, or dashboard data yet. Nothing in this directory is used by the
Python backend, the Streamlit app or the collector.

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
