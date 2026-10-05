/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Prefix for API requests. Empty or unset means same-origin /api. See .env.example. */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
