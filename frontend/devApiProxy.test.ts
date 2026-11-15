import { describe, expect, it } from 'vitest'

import { devApiProxy } from './devApiProxy.ts'

const TARGET = 'https://backend.example.invalid'

describe('the admin development /api proxy', () => {
  it('is off without a target', () => {
    expect(devApiProxy({})).toBeUndefined()
    expect(devApiProxy({ DEV_API_PROXY_TARGET: '  ', DEV_API_PROXY_CHANGE_ORIGIN: 'true' })).toBeUndefined()
  })

  it('keeps the browser host by default, as for a local backend', () => {
    expect(devApiProxy({ DEV_API_PROXY_TARGET: ` ${TARGET} ` })).toEqual({ '/api': { target: TARGET, changeOrigin: false } })
  })

  it.each(['true', ' true '])('sends the target host only when the variable is exactly true (%j)', (value) => {
    expect(devApiProxy({ DEV_API_PROXY_TARGET: TARGET, DEV_API_PROXY_CHANGE_ORIGIN: value })).toEqual({
      '/api': { target: TARGET, changeOrigin: true },
    })
  })

  it.each(['', 'false', 'TRUE', 'True', '1', 'yes', 'on'])('treats any other value as the default (%j)', (value) => {
    expect(devApiProxy({ DEV_API_PROXY_TARGET: TARGET, DEV_API_PROXY_CHANGE_ORIGIN: value })).toEqual({
      '/api': { target: TARGET, changeOrigin: false },
    })
  })
})
