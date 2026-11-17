#!/usr/bin/env node
/**
 * Frontend CI's dependency-audit gate: `npm audit --audit-level=high`, with ONE temporary, narrowly scoped exception.
 *
 * Why (docs/PROJECT_MANIFEST.md, Parked, 2026-11-17): picomatch GHSA-fc5q-39xc-vjpf (high, ReDoS through crafted
 * extglobs) has no patched release -- 4.0.7 is both the latest version and the last vulnerable one -- and picomatch
 * reaches this project only through development tools (Vite, Vitest, tinyglobby/fdir, typescript-eslint), never the
 * shipped app. npm's only offered "fixes" are downgrades (Vite 6.2.7, Vitest 3.1.4), which we do not take.
 *
 * The build FAILS (exit 1) when:
 *   - any high or critical advisory other than the allowlisted one is reported;
 *   - the allowlisted advisory reaches any package that is not exclusively a development dependency;
 *   - the allowlisted advisory has changed (another package or vulnerable range): it must be reviewed again;
 *   - a release outside the vulnerable range has been published (update to it, then remove the exception);
 *   - the exception has expired (after its `expires` date, UTC);
 *   - anything cannot be established: `npm audit` or `npm view` fails unexpectedly, their output cannot be parsed,
 *     the lockfile is missing or unreadable, or any advisory's information is incomplete or inconsistent.
 * Moderate and low advisories never fail the build, as with `--audit-level=high`; they are listed.
 *
 * TO REMOVE THE EXCEPTION, once a fixed picomatch is published: update the lockfile to it, check that
 * `npm audit --audit-level=high` passes, then delete this script and its test and put the workflow step back to
 * `npm audit --audit-level=high` (.github/workflows/frontend-ci.yml). Until then the gate itself fails as soon as the
 * fixed version appears, so the exception cannot outlive its reason.
 */

import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

/** The only exceptions. Each names exactly the advisory, package and vulnerable range it was reviewed for. */
export const ALLOWLIST = Object.freeze([
  Object.freeze({
    id: 'GHSA-fc5q-39xc-vjpf',
    package: 'picomatch',
    vulnerableRange: '<=4.0.7',
    /** The last day (UTC) the exception applies. */
    expires: '2026-12-17',
    reason: 'No patched picomatch release exists; it reaches only development tools (Vite, Vitest, typescript-eslint).',
  }),
])

const SEVERITIES = new Set(['info', 'low', 'moderate', 'high', 'critical'])
const BLOCKING = new Set(['high', 'critical'])
const GHSA = /\/(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})$/
const DATE = /^\d{4}-\d{2}-\d{2}$/

const isObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value)

/**
 * Parses `npm audit --json` output. Returns { audit } or { error }: never throws.
 */
export function parseAudit(text) {
  if (typeof text !== 'string' || text.trim() === '') {
    return { error: 'npm audit produced no output' }
  }
  let audit
  try {
    audit = JSON.parse(text)
  } catch (error) {
    return { error: `npm audit output is not JSON (${error.message})` }
  }
  if (!isObject(audit)) {
    return { error: 'npm audit output is not a JSON object' }
  }
  if ('error' in audit) {
    return { error: `npm audit reported an error: ${JSON.stringify(audit.error)}` }
  }
  if (audit.auditReportVersion !== 2 || !isObject(audit.vulnerabilities) || !isObject(audit.metadata) || !isObject(audit.metadata.vulnerabilities)) {
    return { error: 'npm audit output is not an audit report (version 2) with vulnerabilities and metadata' }
  }
  return { audit }
}

/** A version as [major, minor, patch, prerelease?], or null if it is not one. */
function parseVersion(version) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(version)
  return match ? [Number(match[1]), Number(match[2]), Number(match[3]), match[4] ?? null] : null
}

function compare(a, b) {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) {
      return a[i] - b[i]
    }
  }
  return 0
}

/** Whether a stable version is inside a `<=X.Y.Z` or `<X.Y.Z` range; null if the range is not one of those. */
function inRange(version, range) {
  const match = /^(<=|<)\s*(\d+\.\d+\.\d+)$/.exec(range.trim())
  if (!match) {
    return null
  }
  const order = compare(version, parseVersion(match[2]))
  return match[1] === '<=' ? order <= 0 : order < 0
}

/**
 * The decision, from already-gathered facts -- no I/O, so every case can be tested.
 *
 * @param {object} facts
 * @param {object} facts.audit            parsed `npm audit --json` (parseAudit)
 * @param {object} facts.lockfile         parsed package-lock.json (lockfileVersion 2 or 3)
 * @param {Record<string, unknown>} facts.publishedVersions  each allowlisted package's published versions (`npm view`)
 * @param {string} facts.today            today, YYYY-MM-DD, UTC
 * @param {readonly object[]} [facts.allowlist]
 * @returns {{ ok: boolean, failures: string[], allowed: string[], notes: string[] }}
 */
export function evaluate({ audit, lockfile, publishedVersions, today, allowlist = ALLOWLIST }) {
  const failures = []
  const allowed = []
  const notes = []
  const done = () => ({ ok: failures.length === 0, failures, allowed, notes })

  if (typeof today !== 'string' || !DATE.test(today)) {
    failures.push(`today's date is not known (${JSON.stringify(today)})`)
    return done()
  }
  if (!isObject(lockfile) || !(lockfile.lockfileVersion >= 2) || !isObject(lockfile.packages)) {
    failures.push('package-lock.json is not a lockfile (version 2 or later) with a packages map')
    return done()
  }

  // --- every vulnerability entry, and every advisory, must be complete --------------------------------------------
  const entries = audit.vulnerabilities
  const advisories = new Map()
  for (const [name, entry] of Object.entries(entries)) {
    if (!isObject(entry) || entry.name !== name || !SEVERITIES.has(entry.severity) || !Array.isArray(entry.via) ||
        !Array.isArray(entry.nodes) || entry.nodes.length === 0 || !entry.nodes.every((node) => typeof node === 'string')) {
      failures.push(`incomplete audit entry for ${name}`)
      continue
    }
    for (const via of entry.via) {
      if (typeof via === 'string') {
        if (!isObject(entries[via])) {
          failures.push(`audit entry ${name} depends on ${via}, which the report does not describe`)
        }
        continue
      }
      const id = isObject(via) && typeof via.url === 'string' ? GHSA.exec(via.url)?.[1] : undefined
      if (!id || typeof via.name !== 'string' || !SEVERITIES.has(via.severity) || typeof via.range !== 'string' || via.range.trim() === '') {
        failures.push(`incomplete advisory information in the audit entry for ${name}: ${JSON.stringify(via)}`)
        continue
      }
      const seen = advisories.get(id)
      if (seen && (seen.package !== via.name || seen.range !== via.range || seen.severity !== via.severity)) {
        failures.push(`advisory ${id} is described inconsistently (${seen.package} ${seen.range} ${seen.severity}; ${via.name} ${via.range} ${via.severity})`)
        continue
      }
      advisories.set(id, { id, package: via.name, range: via.range, severity: via.severity, title: via.title ?? '' })
    }
  }
  const counted = audit.metadata.vulnerabilities.total
  if (counted !== Object.keys(entries).length) {
    failures.push(`the audit's total (${JSON.stringify(counted)}) does not match its ${Object.keys(entries).length} entries`)
  }
  if (failures.length > 0) {
    return done()
  }

  // --- which advisories each entry reaches, through the packages it depends on ------------------------------------
  const reach = new Map()
  const reachOf = (name, path = new Set()) => {
    if (reach.has(name)) {
      return reach.get(name)
    }
    const found = new Set()
    if (!path.has(name)) {
      path.add(name)
      for (const via of entries[name].via) {
        if (typeof via === 'string') {
          for (const id of reachOf(via, path)) found.add(id)
        } else {
          found.add(GHSA.exec(via.url)[1])
        }
      }
      path.delete(name)
      reach.set(name, found)
    }
    return found
  }
  for (const name of Object.keys(entries)) {
    reachOf(name)
  }

  // A high or critical entry must be explained by a high or critical advisory it reaches.
  for (const [name, entry] of Object.entries(entries)) {
    if (BLOCKING.has(entry.severity) && ![...reach.get(name)].some((id) => BLOCKING.has(advisories.get(id).severity))) {
      failures.push(`${name} is reported ${entry.severity}, but no ${entry.severity} advisory it depends on explains that`)
    }
  }

  // --- every advisory: blocking unless allowlisted, and the exception's conditions all hold -----------------------
  const listed = new Map(allowlist.map((exception) => [exception.id, exception]))
  for (const advisory of advisories.values()) {
    const affected = Object.keys(entries).filter((name) => reach.get(name).has(advisory.id)).sort()
    if (!BLOCKING.has(advisory.severity)) {
      notes.push(`not blocking (${advisory.severity}): ${advisory.id} ${advisory.package} ${advisory.range}, affecting ${affected.join(', ')}`)
      continue
    }
    const exception = listed.get(advisory.id)
    if (!exception) {
      failures.push(`${advisory.severity} advisory ${advisory.id} in ${advisory.package} ${advisory.range}: ${advisory.title}`.trim())
      continue
    }

    const problems = []
    if (advisory.package !== exception.package || advisory.range !== exception.vulnerableRange) {
      problems.push(`the advisory now names ${advisory.package} ${advisory.range}, not the reviewed ${exception.package} ${exception.vulnerableRange} -- review it again`)
    }
    if (!DATE.test(exception.expires) || today > exception.expires) {
      problems.push(`the exception expired after ${exception.expires}`)
    }

    // Every package the advisory reaches must be installed ONLY as a development dependency.
    for (const name of affected) {
      for (const node of entries[name].nodes) {
        const record = lockfile.packages[node]
        if (!isObject(record)) {
          problems.push(`${node} (${name}) is not in package-lock.json, so it cannot be shown to be a development dependency`)
        } else if (record.dev !== true) {
          problems.push(`${node} (${name}) is not exclusively a development dependency`)
        }
      }
    }

    // A published release outside the vulnerable range is the fix: take it and remove the exception.
    const published = publishedVersions?.[exception.package]
    if (!Array.isArray(published) || published.length === 0 || !published.every((version) => typeof version === 'string')) {
      problems.push(`the published versions of ${exception.package} are not known, so a patched release cannot be ruled out`)
    } else {
      for (const version of published) {
        const parsed = parseVersion(version)
        if (parsed === null) {
          problems.push(`${exception.package} lists a version that cannot be read: ${JSON.stringify(version)}`)
          continue
        }
        if (parsed[3] !== null) {
          continue // a prerelease is not a release to update to
        }
        const vulnerable = inRange(parsed, exception.vulnerableRange)
        if (vulnerable === null) {
          problems.push(`the reviewed range ${exception.vulnerableRange} cannot be evaluated`)
          break
        }
        if (!vulnerable) {
          problems.push(`${exception.package} ${version} is published outside ${exception.vulnerableRange}: update to it and remove this exception`)
        }
      }
    }

    if (problems.length > 0) {
      failures.push(`${advisory.id} (allowlisted) no longer qualifies for its exception: ${problems.join('; ')}`)
    } else {
      allowed.push(
        `${advisory.id} ${advisory.package} ${advisory.range} (${advisory.severity}): allowed until ${exception.expires}, ` +
          `development dependencies only -- ${affected.join(', ')}`,
      )
    }
  }

  for (const exception of allowlist) {
    if (!advisories.has(exception.id)) {
      notes.push(`${exception.id} is no longer reported: remove its exception (scripts/audit-gate.mjs)`)
    }
  }
  return done()
}

function run(command, args, cwd) {
  const options = { cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }
  // On Windows npm is npm.cmd, which Node runs only through a shell; the command line is fixed text, never input.
  return process.platform === 'win32'
    ? spawnSync([command, ...args].join(' '), { ...options, shell: true })
    : spawnSync(command, args, options)
}

/**
 * Gathers the facts and decides. Every source is injectable for tests; any failure to gather one fails closed.
 */
export function gate({
  cwd = join(dirname(fileURLToPath(import.meta.url)), '..'),
  allowlist = ALLOWLIST,
  runAudit = () => run('npm', ['audit', '--json'], cwd),
  readLockfile = () => readFileSync(join(cwd, 'package-lock.json'), 'utf8'),
  viewVersions = (name) => run('npm', ['view', name, 'versions', '--json'], cwd),
  today = () => new Date().toISOString().slice(0, 10),
} = {}) {
  const fail = (message) => ({ ok: false, failures: [message], allowed: [], notes: [] })

  let result
  try {
    result = runAudit()
  } catch (error) {
    return fail(`npm audit could not be run (${error.message})`)
  }
  // npm audit exits 0 with nothing found and 1 with findings; anything else is a failure of the audit itself.
  if (!result || result.error || (result.status !== 0 && result.status !== 1)) {
    return fail(`npm audit failed (status ${result?.status ?? 'none'}${result?.error ? `, ${result.error.message}` : ''}): ${(result?.stderr ?? '').trim()}`)
  }
  const parsed = parseAudit(result.stdout)
  if (parsed.error) {
    return fail(parsed.error)
  }

  let lockfile
  try {
    lockfile = JSON.parse(readLockfile())
  } catch (error) {
    return fail(`package-lock.json could not be read (${error.message})`)
  }

  const publishedVersions = {}
  for (const exception of allowlist) {
    let view
    try {
      view = viewVersions(exception.package)
    } catch (error) {
      return fail(`npm view ${exception.package} could not be run (${error.message})`)
    }
    if (!view || view.error || view.status !== 0) {
      return fail(`npm view ${exception.package} versions failed (status ${view?.status ?? 'none'}): ${(view?.stderr ?? '').trim()}`)
    }
    try {
      const versions = JSON.parse(view.stdout)
      publishedVersions[exception.package] = typeof versions === 'string' ? [versions] : versions
    } catch (error) {
      return fail(`npm view ${exception.package} output is not JSON (${error.message})`)
    }
  }

  return evaluate({ audit: parsed.audit, lockfile, publishedVersions, today: today(), allowlist })
}

function main() {
  const outcome = gate()
  for (const note of outcome.notes) console.log(`note: ${note}`)
  for (const item of outcome.allowed) console.log(`ALLOWED (temporary exception): ${item}`)
  for (const failure of outcome.failures) console.error(`FAIL: ${failure}`)
  console.log(outcome.ok ? 'Audit gate passed.' : `Audit gate FAILED (${outcome.failures.length} problem(s)).`)
  process.exitCode = outcome.ok ? 0 : 1
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main()
}
