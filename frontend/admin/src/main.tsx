import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import '../../src/index.css'
import { AdminApp } from './AdminApp.tsx'

const container = document.getElementById('root')
if (container === null) {
  throw new Error('The #root element is missing from index.html.')
}

createRoot(container).render(
  <StrictMode>
    <AdminApp />
  </StrictMode>,
)
