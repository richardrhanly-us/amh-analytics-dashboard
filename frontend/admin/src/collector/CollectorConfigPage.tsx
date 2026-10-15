import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'

import { isApiError } from '../../../src/api/client.ts'
import { formatInstant, isValidTimeZone } from '../../../src/time/productTime.ts'
import {
  getCollectorSettings,
  getInternalRouting,
  refusedFields,
  ROUTING_LISTS,
  saveCollectorSettings,
  saveInternalRouting,
  type CollectorSettings,
  type EnrollmentProblem,
  type InternalRouting,
  type RoutingList,
  type RoutingLists,
} from './collectorApi.ts'

// Saved to SortView, never "to the collector": an installed Contract v2 Collector keeps what it received at enrollment.
export const SAVED = 'Saved to SortView. Contract v2 collectors that are already installed keep the rules they received at enrollment.'
const CHECK_FIELDS = 'Nothing was saved. Check the fields marked below.'
const CANCELLED = 'Nothing was saved. This library is cancelled; its settings cannot be changed.'

const LIST_LABELS: Record<RoutingList, string> = {
  branch_services_names: 'Branch services account names',
  collection_services_names: 'Collection services account names',
  branch_services_da_patterns: 'Branch services destination markers',
  collection_services_da_patterns: 'Collection services destination markers',
}

const LIST_HELP: Record<RoutingList, string> = {
  branch_services_names: "Patron accounts whose holds are the library's own branch services, not public holds.",
  collection_services_names: "Patron accounts whose holds are the library's collection services, not public holds.",
  branch_services_da_patterns: 'Destination markers that mark an item for branch services.',
  collection_services_da_patterns: 'Destination markers that mark an item for collection services.',
}

const ENROLLMENT_PROBLEM_TEXT: Record<EnrollmentProblem, string> = {
  timezone_not_configured: 'No Collector time zone is recorded.',
  timezone_invalid: 'The recorded Collector time zone is not one this server knows.',
  rules_not_reviewed: 'The routing configuration has not been recorded as reviewed.',
}

const ENTRY_PROBLEM_TEXT: Record<string, string> = {
  too_long: 'is longer than 200 characters',
  control_character: 'contains a control character',
  separator_character: 'contains "|", which no account name or marker can contain',
  not_text: 'is not text',
}

function failureText(caught: unknown): string {
  if (isApiError(caught) && caught.status === 409) {
    return CANCELLED
  }
  return `Nothing was saved. ${isApiError(caught) ? caught.message : 'Please try again.'}`
}

function entries(textValue: string): string[] {
  return textValue.split('\n')
}

function entryCount(textValue: string): number {
  return entries(textValue).filter((line) => line.trim() !== '').length
}

const plural = (n: number) => `${n} ${n === 1 ? 'entry' : 'entries'}`

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function SettingsForm({ orgSlug, stored, onSaved }: { orgSlug: string; stored: CollectorSettings; onSaved: (settings: CollectorSettings) => void }) {
  const [zone, setZone] = useState(stored.collector_timezone ?? '')
  const [reviewed, setReviewed] = useState(false)
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [zoneProblem, setZoneProblem] = useState<string | null>(null)
  const [reviewProblem, setReviewProblem] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const dirty = zone !== (stored.collector_timezone ?? '') || reviewed
  const reviewedAt =
    stored.rules_reviewed_at === null
      ? null
      : formatInstant(stored.rules_reviewed_at, isValidTimeZone(stored.collector_timezone) ? stored.collector_timezone : 'UTC')

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving) {
      return
    }
    setSaved(false)
    setZoneProblem(zone.trim() === '' ? 'Enter the Collector time zone.' : null)
    setReviewProblem(reviewed ? null : "Confirm that you have reviewed this library's routing configuration.")
    if (zone.trim() === '' || !reviewed) {
      setFailure(CHECK_FIELDS)
      return
    }
    setFailure(null)
    setSaving(true)
    try {
      const answer = await saveCollectorSettings(orgSlug, zone)
      onSaved(answer)
      setZone(answer.collector_timezone ?? '')
      setReviewed(false)
      setSaved(true)
    } catch (caught) {
      const refused = refusedFields(caught, 'invalid_collector_settings')
      const zoneRefusal = refused?.find((problem) => problem.field === 'collector_timezone')
      if (zoneRefusal !== undefined) {
        setZoneProblem(
          zoneRefusal.code === 'required' ? 'Enter the Collector time zone.' : 'Enter a time zone name this server knows, such as America/Chicago.',
        )
        setFailure(CHECK_FIELDS)
      } else {
        setFailure(failureText(caught))
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} aria-labelledby="collector-settings-heading" noValidate>
      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}
      <dl className="account-facts">
        <Fact term="Home branch label">{stored.home_branch_label === '' ? <span className="quiet">Not set</span> : stored.home_branch_label}</Fact>
        <Fact term="Transit destinations">
          {stored.transit_destination_labels.length === 0 ? (
            <span className="quiet">None</span>
          ) : (
            <ul>
              {stored.transit_destination_labels.map((label) => (
                <li key={label}>{label}</li>
              ))}
            </ul>
          )}
        </Fact>
        {ROUTING_LISTS.map((name) => (
          <Fact key={name} term={LIST_LABELS[name]}>
            {plural(stored.internal_routing_counts[name])}
          </Fact>
        ))}
        <Fact term="Last reviewed">{reviewedAt ?? <span className="quiet">Never</span>}</Fact>
      </dl>
      <p className="field-help">The home branch label and transit destinations are edited by the library, in its own Settings › Routing.</p>

      {stored.enrollment_problems.length === 0 ? (
        <p>Ready for a Contract v2 enrollment.</p>
      ) : (
        <div className="notice" role="note">
          <p>Not ready for a Contract v2 enrollment:</p>
          <ul>
            {stored.enrollment_problems.map((problem) => (
              <li key={problem}>{ENROLLMENT_PROBLEM_TEXT[problem]}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="field">
        <label htmlFor="collector-timezone">Collector time zone</label>
        <input
          id="collector-timezone"
          type="text"
          autoComplete="off"
          value={zone}
          aria-invalid={zoneProblem !== null}
          aria-describedby={zoneProblem === null ? 'collector-timezone-help' : 'collector-timezone-problem collector-timezone-help'}
          onChange={(event) => {
            setZone(event.target.value)
            setSaved(false)
            setZoneProblem(null)
          }}
        />
        {zoneProblem !== null && (
          <p className="field-problem" id="collector-timezone-problem">
            {zoneProblem}
          </p>
        )}
        <p className="field-help" id="collector-timezone-help">
          The IANA name of the zone the library&rsquo;s sorting machine keeps its clock in, such as America/Chicago. It does not change how
          reports are drawn.
        </p>
      </div>

      <div className="field">
        <label>
          <input
            type="checkbox"
            checked={reviewed}
            aria-invalid={reviewProblem !== null}
            aria-describedby={reviewProblem === null ? undefined : 'collector-reviewed-problem'}
            onChange={(event) => {
              setReviewed(event.target.checked)
              setSaved(false)
              setReviewProblem(null)
            }}
          />{" I have reviewed this library's routing configuration"}
        </label>
        {reviewProblem !== null && (
          <p className="field-problem" id="collector-reviewed-problem">
            {reviewProblem}
          </p>
        )}
      </div>

      <div className="form-actions">
        {/* Unavailable while saving, but not `disabled`: a disabled button drops keyboard focus. */}
        <button type="submit" aria-disabled={saving}>
          {saving ? 'Saving…' : 'Save collector settings'}
        </button>
        {dirty && !saving && <p className="form-saved">You have unsaved changes.</p>}
        {saved && !dirty && (
          <p className="form-saved" role="status">
            {SAVED}
          </p>
        )}
      </div>
    </form>
  )
}

function RoutingForm({ orgSlug, stored, onSaved }: { orgSlug: string; stored: InternalRouting; onSaved: (routing: InternalRouting) => void }) {
  const joined = (lists: RoutingLists) =>
    Object.fromEntries(ROUTING_LISTS.map((name) => [name, lists[name].join('\n')])) as Record<RoutingList, string>
  const [texts, setTexts] = useState(() => joined(stored))
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [problems, setProblems] = useState<Partial<Record<RoutingList, string[]>>>({})
  const [saved, setSaved] = useState(false)

  const dirty = ROUTING_LISTS.some((name) => texts[name] !== stored[name].join('\n'))

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving) {
      return
    }
    setSaved(false)
    setFailure(null)
    setProblems({})
    setSaving(true)
    try {
      // Sent line by line, as typed: the API trims, leaves out blanks and repeats, and answers with what it stored.
      const answer = await saveInternalRouting(
        orgSlug,
        Object.fromEntries(ROUTING_LISTS.map((name) => [name, entries(texts[name])])) as RoutingLists,
      )
      onSaved(answer)
      setTexts(joined(answer))
      setSaved(true)
    } catch (caught) {
      const refused = refusedFields(caught, 'invalid_internal_routing')
      if (refused === null || refused.length === 0) {
        setFailure(failureText(caught))
        return
      }
      const found: Partial<Record<RoutingList, string[]>> = {}
      for (const problem of refused) {
        const [name, index] = problem.field.split('.') as [RoutingList, string | undefined]
        if (!(ROUTING_LISTS as readonly string[]).includes(name)) {
          continue
        }
        const sentence =
          index === undefined
            ? problem.code === 'too_many'
              ? 'There are more than 5000 entries.'
              : 'This list is not valid.'
            : `Line ${Number(index) + 1} ${ENTRY_PROBLEM_TEXT[problem.code] ?? 'is not valid'}.`
        found[name] = [...(found[name] ?? []), sentence]
      }
      setProblems(found)
      setFailure(Object.keys(found).length > 0 ? CHECK_FIELDS : 'Nothing was saved. Check what you entered and try again.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} aria-labelledby="internal-routing-heading" noValidate>
      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}
      <p className="field-help">One entry per line. Blank lines and repeated entries are left out when saved.</p>
      {ROUTING_LISTS.map((name) => {
        const id = `routing-${name}`
        const listProblems = problems[name] ?? []
        return (
          <div className="field" key={name}>
            <label htmlFor={id}>{LIST_LABELS[name]}</label>
            <textarea
              id={id}
              rows={6}
              spellCheck={false}
              value={texts[name]}
              aria-invalid={listProblems.length > 0}
              aria-describedby={`${listProblems.length > 0 ? `${id}-problem ` : ''}${id}-help ${id}-count`}
              onChange={(event) => {
                setTexts({ ...texts, [name]: event.target.value })
                setSaved(false)
                setProblems({ ...problems, [name]: undefined })
              }}
            />
            {listProblems.length > 0 && (
              <ul className="field-problem" id={`${id}-problem`}>
                {listProblems.map((sentence) => (
                  <li key={sentence}>{sentence}</li>
                ))}
              </ul>
            )}
            <p className="field-help" id={`${id}-help`}>
              {LIST_HELP[name]}
            </p>
            <p className="field-help" id={`${id}-count`}>
              {plural(entryCount(texts[name]))}
            </p>
          </div>
        )
      })}
      <div className="form-actions">
        <button type="submit" aria-disabled={saving}>
          {saving ? 'Saving…' : 'Save Internal Routing'}
        </button>
        <button
          type="button"
          className="button-secondary"
          onClick={() => {
            setTexts(joined(stored))
            setProblems({})
            setFailure(null)
          }}
        >
          Discard changes
        </button>
        {dirty && !saving && <p className="form-saved">You have unsaved changes.</p>}
        {saved && !dirty && (
          <p className="form-saved" role="status">
            {SAVED}
          </p>
        )}
      </div>
    </form>
  )
}

function LoadFailure({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <>
      <p className="error-message" role="alert">
        {`The ${what} could not be loaded. Please try again.`}
      </p>
      <button type="button" onClick={onRetry}>
        Try again
      </button>
    </>
  )
}

/** /libraries/:orgSlug/collector -- a library's collector configuration: its Collector settings and Internal Routing. */
export function CollectorConfigPage() {
  const { orgSlug = '' } = useParams()
  const queryClient = useQueryClient()
  const settings = useQuery({
    queryKey: ['admin', 'collector-settings', orgSlug],
    queryFn: ({ signal }) => getCollectorSettings(orgSlug, signal),
  })
  const routing = useQuery({
    queryKey: ['admin', 'internal-routing', orgSlug],
    queryFn: ({ signal }) => getInternalRouting(orgSlug, signal),
  })
  const notFound = [settings.error, routing.error].some((error) => isApiError(error) && error.status === 404)
  const libraryName = settings.data?.library.name ?? routing.data?.library.name

  return (
    <section aria-labelledby="collector-config-heading">
      <p>
        {notFound ? (
          <Link to="/libraries">Libraries</Link>
        ) : (
          <Link to={`/libraries/${encodeURIComponent(orgSlug)}`}>{libraryName ?? 'Library'}</Link>
        )}
      </p>
      <h2 id="collector-config-heading">{notFound ? 'Library not found' : 'Collector configuration'}</h2>
      {notFound ? (
        <p>There is no library with this address.</p>
      ) : (
        <>
          <p className="notice" role="note">
            These settings are saved to SortView. They are used by v1 hold reports and by the next Contract v2 enrollment. A Contract v2 collector
            that is already installed is not updated: it keeps the rules and time zone it received at enrollment.
          </p>

          <section aria-labelledby="collector-settings-heading">
            <h3 id="collector-settings-heading">Collector settings</h3>
            {settings.isPending && <p role="status">Loading collector settings…</p>}
            {settings.isError && <LoadFailure what="collector settings" onRetry={() => void settings.refetch()} />}
            {settings.data !== undefined && (
              <SettingsForm
                orgSlug={orgSlug}
                stored={settings.data}
                onSaved={(answer) => queryClient.setQueryData(['admin', 'collector-settings', orgSlug], answer)}
              />
            )}
          </section>

          <section aria-labelledby="internal-routing-heading">
            <h3 id="internal-routing-heading">Internal Routing</h3>
            {routing.isPending && <p role="status">Loading Internal Routing…</p>}
            {routing.isError && <LoadFailure what="Internal Routing lists" onRetry={() => void routing.refetch()} />}
            {routing.data !== undefined && (
              <RoutingForm
                orgSlug={orgSlug}
                stored={routing.data}
                onSaved={(answer) => {
                  queryClient.setQueryData(['admin', 'internal-routing', orgSlug], answer)
                  // The settings section shows how many entries each list has.
                  void queryClient.invalidateQueries({ queryKey: ['admin', 'collector-settings', orgSlug] })
                }}
              />
            )}
          </section>
        </>
      )}
    </section>
  )
}
