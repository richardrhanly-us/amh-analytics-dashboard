import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useOutletContext } from 'react-router'

import { fieldProblems } from '../api/account.ts'
import { isApiError } from '../api/client.ts'
import type { OrganizationDetail } from '../api/organizations.ts'
import { INVALID_ROUTING_SETTINGS, type RoutingSettings } from '../api/routingSettings.ts'
import { messageFor } from '../components/errorText.ts'
import { LoadFailure } from '../components/LoadFailure.tsx'
import { hasTransits } from '../pages/capabilities.ts'
import { NotFoundPage } from '../pages/NotFoundPage.tsx'
import { SettingsFrame } from './SettingsLayout.tsx'
import { MANAGED_BY_OWNERS_AND_ADMINS, ROUTING_NAME, ROUTING_NOT_AVAILABLE } from './settingsText.ts'
import { useRoutingSettings } from './useRoutingSettings.ts'

/** The most destinations an organization may have. The API decides; this only stops the page offering one more. */
const MAX_DESTINATIONS = 20

const CHECK_FIELDS = 'Nothing was saved. Check the fields marked below.'
const NOT_VALID = 'This is not valid. Check it and try again.'

// What to say under a destination the API refused, by its stable word for why.
const LABEL_PROBLEMS: Record<string, string> = {
  required: 'Enter a label, or remove this destination.',
  duplicate: 'This is the same destination as an earlier one.',
  means_home: 'This label means the home branch, so it cannot also be a destination.',
}
const TOO_MANY = `An organization can have up to ${MAX_DESTINATIONS} destinations.`

/** One destination being edited. `row` only tells the rows apart while they are on the page; it is never sent. */
interface Row {
  row: number
  label: string
  enabled: boolean
}

interface Problems {
  home?: string
  list?: string
  /** By `row`. */
  rows: Record<number, string>
}

const NO_PROBLEMS: Problems = { rows: {} }

// A number no other row on the page has had. It tells rows apart while they are edited, and means nothing else.
let lastRow = 0
const newRow = () => ++lastRow
const rowsOf = (settings: RoutingSettings): Row[] =>
  settings.destinations.map((destination) => ({ row: newRow(), label: destination.label, enabled: destination.enabled }))

function Intro() {
  return (
    <p className="quiet">
      Routing tells SortView how to group check-ins in reports: which destination labels count as kept at a sorting
      machine&rsquo;s own branch, and which are transit to another place.
    </p>
  )
}

/**
 * The routing of an organization that cannot be changed at present: what is
 * stored, in words, with nothing to type in, tick or press.
 */
function StoredRoutingView({ stored }: { stored: RoutingSettings }) {
  return (
    <div className="settings-sections">
      <p className="notice" role="note">
        This organization is suspended, so its routing cannot be changed.
      </p>
      <Intro />

      <section className="panel settings-panel" aria-labelledby="routing-home-heading">
        <h3 id="routing-home-heading">Home branch</h3>
        <dl className="account-facts">
          <div>
            <dt>Home branch label</dt>
            <dd>{stored.home_branch_label === '' ? 'Not set — each sorting machine’s branch name is used' : stored.home_branch_label}</dd>
          </div>
        </dl>
      </section>

      <section className="panel settings-panel" aria-labelledby="routing-destinations-heading">
        <h3 id="routing-destinations-heading">Transit destinations</h3>
        {stored.destinations.length === 0 ? (
          <p>No transit destinations are configured.</p>
        ) : (
          <ol className="nav-list" aria-labelledby="routing-destinations-heading">
            {stored.destinations.map((destination, index) => (
              // A list that is only ever shown whole, in its stored order: its order is its identity.
              <li key={index}>
                {destination.label}
                <span className="nav-list-meta">{destination.enabled ? 'Enabled' : 'Disabled'}</span>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  )
}

/**
 * The form. What is typed stays on the page until it is saved; then the page
 * shows what the API stored, which is the last word on every value.
 *
 * `stored` is what the API last said: what was loaded, and after a save,
 * what that save answered with. Discarding goes back to it.
 */
function RoutingForm({ stored, saving, onSave }: { stored: RoutingSettings; saving: boolean; onSave: (wanted: RoutingSettings) => Promise<RoutingSettings> }) {
  const [home, setHome] = useState(stored.home_branch_label)
  const [rows, setRows] = useState<Row[]>(() => rowsOf(stored))
  const [problems, setProblems] = useState<Problems>(NO_PROBLEMS)
  const [failure, setFailure] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const inputs = useRef(new Map<number, HTMLInputElement>())
  const addButton = useRef<HTMLButtonElement>(null)
  // Where focus goes once the rows have been drawn again: a row's own field, or the button that adds one.
  const [focus, setFocus] = useState<{ row: number | null } | null>(null)

  useEffect(() => {
    if (focus === null) {
      return
    }
    if (focus.row === null) {
      addButton.current?.focus()
    } else {
      inputs.current.get(focus.row)?.focus()
    }
  }, [focus])

  const dirty =
    home !== stored.home_branch_label ||
    rows.length !== stored.destinations.length ||
    rows.some((row, index) => row.label !== stored.destinations[index].label || row.enabled !== stored.destinations[index].enabled)
  const full = rows.length >= MAX_DESTINATIONS

  function show(settings: RoutingSettings) {
    setHome(settings.home_branch_label)
    setRows(rowsOf(settings))
    setProblems(NO_PROBLEMS)
    setFailure(null)
  }

  function edit(row: number, change: Partial<Row>) {
    setRows(rows.map((current) => (current.row === row ? { ...current, ...change } : current)))
    setSaved(false)
    if (change.label !== undefined && problems.rows[row] !== undefined) {
      // The field was changed: what was said about it is no longer about what is in it.
      const others = { ...problems.rows }
      delete others[row]
      setProblems({ ...problems, rows: others })
    }
  }

  function add() {
    if (full) {
      return
    }
    const row = newRow()
    setRows([...rows, { row, label: '', enabled: true }])
    setSaved(false)
    setFocus({ row })
  }

  function remove(row: number) {
    const index = rows.findIndex((current) => current.row === row)
    const others = rows.filter((current) => current.row !== row)
    setRows(others)
    setSaved(false)
    setProblems({ ...problems, list: undefined })
    // The row that had focus is gone: the next one takes it, else the one before, else the way to add one.
    const neighbour = others[index] ?? others[index - 1]
    setFocus({ row: neighbour === undefined ? null : neighbour.row })
  }

  function discard() {
    show(stored)
    setSaved(false)
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving) {
      return
    }
    setSaved(false)

    // The one thing checked before asking: a destination with nothing typed. Every other rule is the API's.
    const blank = rows.filter((row) => row.label.trim() === '')
    if (blank.length > 0) {
      setProblems({ rows: Object.fromEntries(blank.map((row) => [row.row, LABEL_PROBLEMS.required])) })
      setFailure(CHECK_FIELDS)
      return
    }

    setProblems(NO_PROBLEMS)
    setFailure(null)
    // Which row each destination is, in the order it is sent: the API names a refused one by its place in it.
    const sent = rows
    try {
      const answer = await onSave({ home_branch_label: home, destinations: sent.map((row) => ({ label: row.label, enabled: row.enabled })) })
      // What the API stored is what is now shown: a label it trimmed is shown trimmed.
      show(answer)
      setSaved(true)
    } catch (caught) {
      if (isApiError(caught) && caught.status === 401) {
        return
      }
      const refused = fieldProblems(caught, INVALID_ROUTING_SETTINGS)
      if (refused === null || refused.length === 0) {
        // What was typed stays where it is.
        setFailure(`Nothing was saved. ${messageFor(caught)}`)
        return
      }
      const found: Problems = { rows: {} }
      for (const problem of refused) {
        const destination = /^destinations\.(\d+)\.label$/.exec(problem.field)
        const row = destination === null ? undefined : sent[Number(destination[1])]
        if (row !== undefined) {
          found.rows[row.row] ??= Object.hasOwn(LABEL_PROBLEMS, problem.code) ? LABEL_PROBLEMS[problem.code] : NOT_VALID
        } else if (problem.field === 'destinations') {
          found.list ??= problem.code === 'too_many' ? TOO_MANY : NOT_VALID
        } else if (problem.field === 'home_branch_label') {
          found.home ??= NOT_VALID
        }
      }
      setProblems(found)
      // A problem with something this form has no field for is still a refusal: it is said, without the field.
      const placed = found.home !== undefined || found.list !== undefined || Object.keys(found.rows).length > 0
      setFailure(placed ? CHECK_FIELDS : 'Nothing was saved. Check what you entered and try again.')
    }
  }

  return (
    <form className="settings-sections routing-form" onSubmit={handleSubmit} aria-label={ROUTING_NAME} noValidate>
      <Intro />
      <p className="notice" role="note">
        Changes apply to every report as soon as you save, including reports for past dates. Past check-ins are not
        changed; they are grouped by the labels as they are now. Check-ins whose label no longer matches appear under
        &ldquo;Other routing&rdquo;.
      </p>

      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}

      <section className="panel settings-panel" aria-labelledby="routing-home-heading">
        <h3 id="routing-home-heading">Home branch</h3>
        <div className="field">
          <label htmlFor="routing-home-label">Home branch label</label>
          <input
            id="routing-home-label"
            name="home_branch_label"
            type="text"
            autoComplete="off"
            value={home}
            aria-invalid={problems.home !== undefined}
            aria-describedby={problems.home === undefined ? 'routing-home-help' : 'routing-home-problem routing-home-help'}
            onChange={(event) => {
              setHome(event.target.value)
              setSaved(false)
              setProblems({ ...problems, home: undefined })
            }}
          />
          {problems.home !== undefined && (
            <p className="field-problem" id="routing-home-problem">
              {problems.home}
            </p>
          )}
          <p className="field-help" id="routing-home-help">
            The label your sorting machines use for items that stay at their own branch. Leave blank to use each
            sorting machine&rsquo;s branch name.
          </p>
        </div>
      </section>

      <section className="panel settings-panel" aria-labelledby="routing-destinations-heading">
        <h3 id="routing-destinations-heading">Transit destinations</h3>
        {rows.length === 0 ? (
          <p>No transit destinations are configured.</p>
        ) : (
          <ol className="routing-rows">
            {rows.map(({ row, label, enabled }, index) => {
              const number = index + 1
              const problem = problems.rows[row]
              return (
                <li key={row}>
                  <div className="routing-row" role="group" aria-labelledby={`routing-destination-${row}-name`}>
                    <div className="field">
                      <label htmlFor={`routing-destination-${row}`} id={`routing-destination-${row}-name`}>
                        Destination {number}
                      </label>
                      <input
                        id={`routing-destination-${row}`}
                        ref={(element) => {
                          if (element === null) {
                            inputs.current.delete(row)
                          } else {
                            inputs.current.set(row, element)
                          }
                        }}
                        type="text"
                        autoComplete="off"
                        value={label}
                        aria-invalid={problem !== undefined}
                        aria-describedby={problem === undefined ? undefined : `routing-destination-${row}-problem`}
                        onChange={(event) => edit(row, { label: event.target.value })}
                      />
                      {problem !== undefined && (
                        <p className="field-problem" id={`routing-destination-${row}-problem`}>
                          {problem}
                        </p>
                      )}
                    </div>
                    <label className="routing-enabled">
                      <input type="checkbox" checked={enabled} onChange={(event) => edit(row, { enabled: event.target.checked })} /> Enabled
                    </label>
                    <button type="button" className="button-secondary" onClick={() => remove(row)}>
                      Remove <span className="visually-hidden">destination {number}</span>
                    </button>
                  </div>
                </li>
              )
            })}
          </ol>
        )}

        {problems.list !== undefined && (
          <p className="field-problem" id="routing-destinations-problem">
            {problems.list}
          </p>
        )}

        <div className="form-actions">
          {/* Unavailable at the limit, but not `disabled`: a disabled button drops keyboard focus. */}
          <button type="button" className="button-secondary" ref={addButton} onClick={add} aria-disabled={full}>
            Add destination
          </button>
          {full && <p className="form-saved">{TOO_MANY}</p>}
        </div>

        <p className="field-help">
          Enter each label as it appears on the sorter. Capital letters, spacing and punctuation do not matter. A
          disabled destination stays in this list but is not shown in reports; its check-ins count as Other routing.
        </p>
      </section>

      <div className="form-actions">
        {/* Unavailable while saving, but not `disabled`: a disabled button drops keyboard focus. */}
        <button type="submit" aria-disabled={saving}>
          {saving ? 'Saving…' : 'Save routing'}
        </button>
        <button type="button" className="button-secondary" onClick={discard}>
          Discard changes
        </button>
        {dirty && !saving && <p className="form-saved">You have unsaved changes.</p>}
        {saved && !dirty && (
          <p className="form-saved" role="status">
            Saved. Reports now use these labels.
          </p>
        )}
      </div>
    </form>
  )
}

/**
 * /organizations/:orgSlug/settings/routing -- the organization's routing:
 * what its sorting machines call their own branch, and the other places
 * their items go.
 *
 * It is the organization's own, read and replaced whole. Nothing here works
 * out what a label matches or whether two are the same place: it is asked,
 * and the API's answer is shown beside the field it is about.
 *
 * A suspended organization's routing can be read and not changed, so it is
 * shown as it is stored, with nothing to change it with.
 *
 * Routing is part of transit routing, a plan feature. For an organization
 * whose plan does not include it -- an address typed in, or one kept from
 * before -- this says so and asks the API for nothing, which would refuse it.
 */
export function RoutingPage() {
  const organization = useOutletContext<OrganizationDetail>()

  if (!hasTransits(organization)) {
    return (
      <SettingsFrame organization={organization} section={ROUTING_NAME}>
        <p className="notice" role="note">
          {ROUTING_NOT_AVAILABLE}
        </p>
      </SettingsFrame>
    )
  }
  return <RoutingSettings organization={organization} />
}

function RoutingSettings({ organization }: { organization: OrganizationDetail }) {
  const { routing, retry, save, saving } = useRoutingSettings(organization.slug)

  if (routing.status === 'not_found') {
    // The organization is no longer this person's to see: the one page for that, and nothing of this one.
    return <NotFoundPage />
  }

  return (
    <SettingsFrame organization={organization} section={ROUTING_NAME}>
      {routing.status === 'loading' && <p role="status">Loading routing…</p>}
      {routing.status === 'error' && <LoadFailure message={routing.message} onRetry={retry} />}
      {routing.status === 'forbidden' && (
        <p className="notice" role="note">
          {MANAGED_BY_OWNERS_AND_ADMINS}
        </p>
      )}
      {routing.status === 'ready' &&
        (organization.access_mode === 'full' ? (
          <RoutingForm stored={routing.data} saving={saving} onSave={save} />
        ) : (
          <StoredRoutingView stored={routing.data} />
        ))}
    </SettingsFrame>
  )
}
