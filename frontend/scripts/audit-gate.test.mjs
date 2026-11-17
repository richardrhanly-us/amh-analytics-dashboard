// @vitest-environment node
import { describe, expect, it } from 'vitest'

import { ALLOWLIST, evaluate, gate, parseAudit } from './audit-gate.mjs'

const PICOMATCH = 'https://github.com/advisories/GHSA-fc5q-39xc-vjpf'
const MINIMATCH = 'https://github.com/advisories/GHSA-g3hv-75wg-m249'

const advisory = (overrides = {}) => ({
  source: 1247710,
  name: 'picomatch',
  dependency: 'picomatch',
  title: 'picomatch vulnerable to ReDoS through single-branch repeated extglobs',
  url: PICOMATCH,
  severity: 'high',
  cwe: ['CWE-1333'],
  range: '<=4.0.7',
  ...overrides,
})

const entry = (name, severity, via, nodes = [`node_modules/${name}`]) => ({
  name,
  severity,
  isDirect: false,
  via,
  effects: [],
  range: '*',
  nodes,
  fixAvailable: false,
})

const report = (entries) => ({
  auditReportVersion: 2,
  vulnerabilities: Object.fromEntries(entries.map((item) => [item.name, item])),
  metadata: { vulnerabilities: { total: entries.length } },
})

/** Today's findings, in the shape npm reports them: picomatch through the build and test tools, and minimatch. */
const current = (extra = []) =>
  report([
    entry('picomatch', 'high', [advisory()]),
    entry('fdir', 'high', ['picomatch']),
    entry('tinyglobby', 'high', ['fdir', 'picomatch']),
    entry('vite', 'high', ['picomatch', 'tinyglobby']),
    entry('vitest', 'high', ['picomatch', 'tinyglobby', 'vite']),
    entry('@typescript-eslint/typescript-estree', 'high', ['tinyglobby']),
    entry('minimatch', 'moderate', [
      { source: 1244623, name: 'minimatch', dependency: 'minimatch', title: 'minimatch globparts amplification', url: MINIMATCH, severity: 'moderate', range: '>=7.0.0 <11.0.1' },
    ]),
    entry('eslint-plugin-jsx-a11y-x', 'moderate', ['minimatch']),
    ...extra,
  ])

/** A lockfile in which every package the audit names is a development dependency, unless overridden. */
const lockfileFor = (audit, overrides = {}) => ({
  lockfileVersion: 3,
  packages: {
    '': { name: 'sortview-frontend' },
    ...Object.fromEntries(
      Object.values(audit.vulnerabilities)
        .flatMap((item) => item.nodes)
        .map((node) => [node, { version: '1.0.0', dev: true }]),
    ),
    ...overrides,
  },
})

const PUBLISHED = ['2.3.1', '3.0.1', '4.0.6', '4.0.7']

const facts = (overrides = {}) => {
  const audit = overrides.audit ?? current()
  return { audit, lockfile: lockfileFor(audit), publishedVersions: { picomatch: PUBLISHED }, today: '2026-11-17', ...overrides }
}

const failuresOf = (result) => result.failures.join('\n')

describe('the audit gate, on findings like today’s', () => {
  it('allows only the reviewed picomatch advisory, development-only, and lists moderate findings without failing', () => {
    const result = evaluate(facts())
    expect(result.failures).toEqual([])
    expect(result.ok).toBe(true)
    expect(result.allowed).toHaveLength(1)
    expect(result.allowed[0]).toContain('GHSA-fc5q-39xc-vjpf picomatch <=4.0.7 (high): allowed until 2026-12-17')
    expect(result.allowed[0]).toContain('vite, vitest')
    expect(result.notes.join('\n')).toContain('not blocking (moderate): GHSA-g3hv-75wg-m249')
  })

  it('has exactly one exception: GHSA-fc5q-39xc-vjpf, picomatch <=4.0.7, until 17 December 2026', () => {
    expect(ALLOWLIST).toHaveLength(1)
    expect(ALLOWLIST[0]).toMatchObject({ id: 'GHSA-fc5q-39xc-vjpf', package: 'picomatch', vulnerableRange: '<=4.0.7', expires: '2026-12-17' })
    expect(Object.isFrozen(ALLOWLIST) && Object.isFrozen(ALLOWLIST[0])).toBe(true)
  })

  it('passes with nothing reported, and says the exception can be removed', () => {
    const audit = report([])
    const result = evaluate(facts({ audit, lockfile: lockfileFor(audit) }))
    expect(result.ok).toBe(true)
    expect(result.notes.join('\n')).toContain('GHSA-fc5q-39xc-vjpf is no longer reported: remove its exception')
  })
})

describe('new vulnerabilities', () => {
  it.each(['high', 'critical'])('fails on any other %s advisory', (severity) => {
    const audit = current([entry('lodash', severity, [advisory({ name: 'lodash', url: 'https://github.com/advisories/GHSA-aaaa-bbbb-cccc', severity, range: '<4.17.21' })])])
    const result = evaluate(facts({ audit, lockfile: lockfileFor(audit) }))
    expect(result.ok).toBe(false)
    expect(failuresOf(result)).toContain(`${severity} advisory GHSA-aaaa-bbbb-cccc in lodash <4.17.21`)
  })

  it('fails on a second, different high advisory in picomatch itself', () => {
    const audit = current()
    audit.vulnerabilities.picomatch.via.push(advisory({ url: 'https://github.com/advisories/GHSA-dddd-eeee-ffff', range: '<=4.0.7' }))
    const result = evaluate(facts({ audit }))
    expect(result.ok).toBe(false)
    expect(failuresOf(result)).toContain('GHSA-dddd-eeee-ffff')
  })

  it('does not fail on another moderate or low advisory', () => {
    const audit = current([entry('debug', 'low', [advisory({ name: 'debug', url: 'https://github.com/advisories/GHSA-gggg-hhhh-iiii', severity: 'low', range: '<2.6.9' })])])
    expect(evaluate(facts({ audit, lockfile: lockfileFor(audit) })).ok).toBe(true)
  })
})

describe('production dependencies', () => {
  it('fails if any package the advisory reaches is not exclusively a development dependency', () => {
    const audit = current()
    const result = evaluate(facts({ audit, lockfile: lockfileFor(audit, { 'node_modules/vite': { version: '8.3.4' } }) }))
    expect(result.ok).toBe(false)
    expect(failuresOf(result)).toContain('node_modules/vite (vite) is not exclusively a development dependency')
  })

  it('fails if picomatch itself is a production dependency, or an optional one', () => {
    const audit = current()
    for (const record of [{ version: '4.0.7' }, { version: '4.0.7', devOptional: true }, { version: '4.0.7', dev: false }]) {
      const result = evaluate(facts({ audit, lockfile: lockfileFor(audit, { 'node_modules/picomatch': record }) }))
      expect(result.ok).toBe(false)
      expect(failuresOf(result)).toContain('node_modules/picomatch (picomatch) is not exclusively a development dependency')
    }
  })

  it('fails closed if an affected package is not in the lockfile', () => {
    const audit = current()
    const lockfile = lockfileFor(audit)
    delete lockfile.packages['node_modules/fdir']
    const result = evaluate(facts({ audit, lockfile }))
    expect(result.ok).toBe(false)
    expect(failuresOf(result)).toContain('node_modules/fdir (fdir) is not in package-lock.json')
  })

  it('ignores the production status of packages the allowlisted advisory does not reach', () => {
    const audit = current()
    const lockfile = lockfileFor(audit, { 'node_modules/minimatch': { version: '10.2.6' } })
    expect(evaluate(facts({ audit, lockfile })).ok).toBe(true)
  })
})

describe('expiry', () => {
  it('applies through 17 December 2026, UTC, and fails from the 18th', () => {
    expect(evaluate(facts({ today: '2026-12-17' })).ok).toBe(true)
    const result = evaluate(facts({ today: '2026-12-18' }))
    expect(result.ok).toBe(false)
    expect(failuresOf(result)).toContain('the exception expired after 2026-12-17')
  })

  it('fails closed without a valid date for today', () => {
    expect(evaluate(facts({ today: '17/11/2026' })).ok).toBe(false)
    expect(evaluate(facts({ today: undefined })).ok).toBe(false)
  })
})

describe('a patched picomatch', () => {
  it.each(['4.0.8', '4.1.0', '5.0.0'])('fails once %s is published, to take it and remove the exception', (version) => {
    const result = evaluate(facts({ publishedVersions: { picomatch: [...PUBLISHED, version] } }))
    expect(result.ok).toBe(false)
    expect(failuresOf(result)).toContain(`picomatch ${version} is published outside <=4.0.7: update to it and remove this exception`)
  })

  it('does not count a prerelease as a patched release', () => {
    expect(evaluate(facts({ publishedVersions: { picomatch: [...PUBLISHED, '4.0.8-beta.1'] } })).ok).toBe(true)
  })

  it.each([
    ['unknown', {}],
    ['empty', { picomatch: [] }],
    ['not a list', { picomatch: '4.0.7' }],
    ['unreadable', { picomatch: [...PUBLISHED, 'four'] }],
  ])('fails closed if the published versions are %s', (_case, publishedVersions) => {
    expect(evaluate(facts({ publishedVersions })).ok).toBe(false)
  })

  it('fails if the advisory now names another range or package: it must be reviewed again', () => {
    const widened = current()
    widened.vulnerabilities.picomatch.via = [advisory({ range: '<=4.0.8' })]
    expect(failuresOf(evaluate(facts({ audit: widened })))).toContain('review it again')

    const moved = current([entry('micromatch', 'high', [advisory({ name: 'micromatch', range: '<=4.0.8' })])])
    const result = evaluate(facts({ audit: moved, lockfile: lockfileFor(moved) }))
    expect(result.ok).toBe(false)
  })
})

describe('malformed or incomplete audit output fails closed', () => {
  it.each([
    ['empty output', ''],
    ['not JSON', 'npm ERR! network request failed'],
    ['a JSON list', '[]'],
    ['an npm error', JSON.stringify({ error: { code: 'ENOAUDIT', summary: 'Your configured registry does not support audit requests' } })],
    ['an old report format', JSON.stringify({ auditReportVersion: 1, advisories: {}, metadata: {} })],
    ['no vulnerabilities map', JSON.stringify({ auditReportVersion: 2, metadata: { vulnerabilities: { total: 0 } } })],
  ])('rejects %s', (_case, text) => {
    expect(parseAudit(text).error).toEqual(expect.any(String))
  })

  it('parses a real report shape', () => {
    expect(parseAudit(JSON.stringify(current())).audit).toEqual(current())
  })

  const broken = {
    'an advisory without its URL': (audit) => delete audit.vulnerabilities.picomatch.via[0].url,
    'an advisory URL that is not a GHSA': (audit) => (audit.vulnerabilities.picomatch.via[0].url = 'https://example.org/advisory/1'),
    'an advisory without a range': (audit) => delete audit.vulnerabilities.picomatch.via[0].range,
    'an unknown severity': (audit) => (audit.vulnerabilities.picomatch.via[0].severity = 'severe'),
    'an entry without nodes': (audit) => (audit.vulnerabilities.vite.nodes = []),
    'a dependency the report does not describe': (audit) => audit.vulnerabilities.vite.via.push('rollup'),
    'a total that does not match the entries': (audit) => (audit.metadata.vulnerabilities.total = 99),
    'a high entry no high advisory explains': (audit) => (audit.vulnerabilities['eslint-plugin-jsx-a11y-x'].severity = 'high'),
    'one advisory described two ways': (audit) => audit.vulnerabilities.fdir.via.push(advisory({ severity: 'moderate' })),
  }
  it.each(Object.entries(broken))('fails on %s', (_case, breakIt) => {
    const audit = current()
    breakIt(audit)
    const result = evaluate(facts({ audit, lockfile: lockfileFor(current()) }))
    expect(result.ok).toBe(false)
    expect(result.failures.length).toBeGreaterThan(0)
  })

  it('fails on a lockfile that is not a version 2+ lockfile', () => {
    expect(evaluate(facts({ lockfile: { lockfileVersion: 1, dependencies: {} } })).ok).toBe(false)
    expect(evaluate(facts({ lockfile: null })).ok).toBe(false)
  })
})

describe('gathering the facts', () => {
  const sources = (overrides = {}) => ({
    runAudit: () => ({ status: 1, stdout: JSON.stringify(current()), stderr: '' }),
    readLockfile: () => JSON.stringify(lockfileFor(current())),
    viewVersions: () => ({ status: 0, stdout: JSON.stringify(PUBLISHED), stderr: '' }),
    today: () => '2026-11-17',
    ...overrides,
  })

  it('passes on today’s findings gathered from npm', () => {
    expect(gate(sources()).ok).toBe(true)
  })

  it.each([
    ['npm audit exits with an unexpected status', { runAudit: () => ({ status: 2, stdout: '', stderr: 'npm ERR! audit endpoint returned an error' }) }],
    ['npm audit cannot be started', { runAudit: () => ({ status: null, error: new Error('spawn npm ENOENT'), stdout: '', stderr: '' }) }],
    ['npm audit throws', { runAudit: () => { throw new Error('boom') } }],
    ['npm audit prints garbage', { runAudit: () => ({ status: 1, stdout: '<html>502 Bad Gateway</html>', stderr: '' }) }],
    ['the lockfile cannot be read', { readLockfile: () => { throw new Error('ENOENT') } }],
    ['the lockfile is not JSON', { readLockfile: () => 'lockfileVersion: 3' }],
    ['npm view fails', { viewVersions: () => ({ status: 1, stdout: '', stderr: 'npm ERR! 404' }) }],
    ['npm view prints garbage', { viewVersions: () => ({ status: 0, stdout: 'not json', stderr: '' }) }],
  ])('fails closed when %s', (_case, overrides) => {
    const result = gate(sources(overrides))
    expect(result.ok).toBe(false)
    expect(result.failures).toHaveLength(1)
  })

  it('reads a single published version, as npm prints it, as a list', () => {
    const result = gate(sources({ viewVersions: () => ({ status: 0, stdout: JSON.stringify('4.0.8'), stderr: '' }) }))
    expect(result.ok).toBe(false)
    expect(result.failures.join('\n')).toContain('picomatch 4.0.8 is published outside <=4.0.7')
  })
})
