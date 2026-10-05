# SortView frontend

The SortView customer web app: a React + TypeScript single-page app built
with Vite. It is a static SPA. There is no server-side rendering.

**Status: Block F1, scaffold only.** The app renders a placeholder page. It
makes no API calls and has no login, routing or data fetching yet; those
arrive in later blocks. Nothing in this directory is used by the Python
backend, the Streamlit app or the collector.

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

## Build output

`npm run build` writes the production site to `dist/`. That directory is
git-ignored and is what will be uploaded to static hosting.

## Configuration

`.env.example` documents the two variables this app will use. Neither is read
by any code in F1.

- `VITE_API_BASE_URL`: where the browser sends API requests. Left empty, the
  app will call the API on its own origin under `/api`, which is the intended
  setup.
- `DEV_API_PROXY_TARGET`: reserved for local development. A later block will
  have the Vite dev server forward `/api` to a locally running backend at this
  address. It is not prefixed with `VITE_`, so it is never bundled.
