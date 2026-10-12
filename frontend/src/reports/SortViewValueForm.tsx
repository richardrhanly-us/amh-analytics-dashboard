import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'

import { valueSettingProblems, type SortViewValueSettings } from '../api/sortviewValue.ts'
import { messageFor } from '../components/errorText.ts'
import { SectionPlaceholder } from '../liveToday/SectionPlaceholder.tsx'
import {
  basisText,
  draftsOf,
  FIELD_LABELS,
  fieldKind,
  inputOf,
  isValueField,
  perHour,
  perYear,
  VALUE_FIELDS,
  valueFieldProblem,
  valueProblemText,
  type ValueDrafts,
  type ValueField,
} from './sortviewValueText.ts'
import { useSaveSortViewValue, useSortViewValueSettings, type StoredValueSettings } from './useSortViewValue.ts'

/**
 * Where an organization's owners and admins enter what SortView Value is
 * worked out from. Closed until asked for, and nothing is read until it is
 * opened: the report is the subject of the page, and this is beside it.
 *
 * One form, saved whole. A value is sent exactly as typed, or not at all --
 * nothing is rounded, and nothing shown elsewhere changes until the API has
 * stored it and answered the report again. No figure is worked out here.
 *
 * EVERY FIGURE IS THE ORGANIZATION'S OWN. There is no built-in rate, count or
 * duration, and none is suggested.
 */

const FORM = 'sortview-value'
const PANEL = 'sortview-value-assumptions'
const NOTHING_SAVED = 'Nothing was saved. Check the fields marked below.'

const inputId = (field: ValueField) => `${FORM}-${field.replace('.', '-')}`

/** What is under each field, always: what the value is, and what it is used for. */
const HELP: Record<ValueField, string> = {
  labor_rate: 'What an hour of this staff time costs. Leave blank to use the organization Efficiency default.',
  subscription_annual_cost: 'What SortView costs your organization in a year. Blank means not known; 0 means none.',
  'reporting.tasks_per_month': 'How many reports or statistics tasks are prepared in a typical month.',
  'reporting.baseline_minutes_per_task': 'Minutes one task took before SortView.',
  'reporting.current_minutes_per_task': 'Minutes one task takes now, with SortView.',
  'reporting.basis': 'Whether these figures are an estimate or were measured. It describes the figures and changes none of them.',
  'reporting.as_of': 'The date these figures describe. It changes none of them.',
  'troubleshooting.incidents_per_month': 'How many sorter problems are investigated in a typical month.',
  'troubleshooting.staff_per_incident': 'How many staff a typical incident involves.',
  'troubleshooting.baseline_minutes': 'Minutes each of them spent on one incident before SortView.',
  'troubleshooting.current_minutes': 'Minutes each of them spends on one incident now, with SortView.',
  'troubleshooting.basis': 'Whether these figures are an estimate or were measured. It describes the figures and changes none of them.',
  'troubleshooting.as_of': 'The date these figures describe. It changes none of them.',
}

const GROUPS: ReadonlyArray<{ name: string; legend: string; fields: readonly ValueField[] }> = [
  { name: 'shared', legend: 'Labor rate and subscription', fields: ['labor_rate', 'subscription_annual_cost'] },
  { name: 'reporting', legend: 'Reporting', fields: VALUE_FIELDS.filter((field) => field.startsWith('reporting.')) },
  { name: 'troubleshooting', legend: 'Troubleshooting', fields: VALUE_FIELDS.filter((field) => field.startsWith('troubleshooting.')) },
]

/** In words: the organization's Efficiency default, which a blank labor rate falls back on. */
function EfficiencyDefault({ settings }: { settings: SortViewValueSettings | null }) {
  if (settings === null) {
    return null
  }
  const fallback = settings.labor_rate.organization_efficiency
  return <p>Organization Efficiency default: {fallback === null ? 'none set' : perHour(fallback)}.</p>
}

function Field({
  field,
  value,
  problem,
  extra,
  onChange,
}: {
  field: ValueField
  value: string
  problem: string | undefined
  extra?: ReactNode
  onChange: (value: string) => void
}) {
  const id = inputId(field)
  const kind = fieldKind(field)
  const describedBy = problem === undefined ? `${id}-help` : `${id}-help ${id}-problem`

  return (
    <div className="field">
      <label htmlFor={id}>{FIELD_LABELS[field]}</label>
      {kind === 'basis' ? (
        <select id={id} name={field} value={value} aria-invalid={problem !== undefined} aria-describedby={describedBy} onChange={(event) => onChange(event.target.value)}>
          <option value="">Not set</option>
          <option value="estimate">Estimate</option>
          <option value="measured">Measured</option>
        </select>
      ) : (
        <input
          id={id}
          name={field}
          type={kind === 'date' ? 'date' : 'text'}
          inputMode={kind === 'date' ? undefined : 'decimal'}
          autoComplete="off"
          spellCheck={false}
          value={value}
          aria-invalid={problem !== undefined}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      <div className="field-help" id={`${id}-help`}>
        <p>{HELP[field]}</p>
        {extra}
      </div>
      {problem !== undefined && (
        <p className="field-problem" id={`${id}-problem`}>
          {problem}
        </p>
      )}
    </div>
  )
}

type Problems = Partial<Record<ValueField, string>>

function ValueForm({
  stored,
  saving,
  onSave,
}: {
  /** What is stored, or null when it could not be read: the form then starts empty. */
  stored: SortViewValueSettings | null
  saving: boolean
  onSave: (drafts: ValueDrafts) => Promise<SortViewValueSettings>
}) {
  const [drafts, setDrafts] = useState<ValueDrafts>(() => draftsOf(stored))
  const [problems, setProblems] = useState<Problems>({})
  const [failure, setFailure] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  // Counts the saves that failed: each one moves focus to the first thing that is wrong.
  const [failed, setFailed] = useState(0)
  const summary = useRef<HTMLDivElement>(null)
  const atFault = VALUE_FIELDS.filter((field) => problems[field] !== undefined)

  useEffect(() => {
    if (failed === 0) {
      return
    }
    const first = VALUE_FIELDS.find((field) => problems[field] !== undefined)
    // The first field at fault; or, when no field is, the sentence that says what went wrong.
    const target = first === undefined ? summary.current : document.getElementById(inputId(first))
    target?.focus()
    // Only a failed save moves focus: not each change to what is marked.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [failed])

  function fail(found: Problems, message: string) {
    setProblems(found)
    setFailure(message)
    setFailed((count) => count + 1)
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving) {
      return
    }
    setSaved(false)
    setFailure(null)

    const found: Problems = {}
    for (const field of VALUE_FIELDS) {
      const code = valueFieldProblem(field, drafts[field])
      if (code !== null) {
        found[field] = valueProblemText(field, code)
      }
    }
    if (Object.keys(found).length > 0) {
      fail(found, NOTHING_SAVED)
      return
    }
    setProblems({})

    try {
      // What the API stored is what the form then shows: every value as it is kept.
      setDrafts(draftsOf(await onSave(drafts)))
      setSaved(true)
    } catch (caught) {
      // The API's own word on each field, by its stable code. Never the value that was sent.
      const refused = (valueSettingProblems(caught) ?? []).filter((problem) => isValueField(problem.field))
      if (refused.length > 0) {
        fail(Object.fromEntries(refused.map((problem) => [problem.field, valueProblemText(problem.field as ValueField, problem.code)])) as Problems, NOTHING_SAVED)
      } else {
        fail({}, `Nothing was saved. ${messageFor(caught)}`)
      }
    }
  }

  return (
    <form className="assumptions-form value-form" onSubmit={handleSubmit} aria-labelledby={`${FORM}-form-heading`} noValidate>
      <h5 id={`${FORM}-form-heading`}>Assumptions</h5>
      <div className="quiet">
        <p>
          These are assumptions entered by your organization. They are saved together, stored exactly as entered and never
          rounded. A field left blank is not set: it is not the same as 0.
        </p>
        {stored === null && (
          <p className="notice" role="note">
            What is stored here could not be read, so the fields start empty. Saving replaces it.
          </p>
        )}
      </div>

      {failure !== null && (
        <div className="error-message" role="alert" tabIndex={-1} ref={summary}>
          <p>{failure}</p>
          {atFault.length > 0 && (
            <ul>
              {atFault.map((field) => (
                <li key={field}>
                  {FIELD_LABELS[field]}: {problems[field]}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="assumptions-forms">
        {GROUPS.map((group) => (
          <fieldset className="value-fieldset" key={group.name}>
            <legend>{group.legend}</legend>
            {group.fields.map((field) => (
              <Field
                key={field}
                field={field}
                value={drafts[field]}
                problem={problems[field]}
                extra={field === 'labor_rate' ? <EfficiencyDefault settings={stored} /> : undefined}
                onChange={(value) => {
                  setDrafts({ ...drafts, [field]: value })
                  setSaved(false)
                }}
              />
            ))}
          </fieldset>
        ))}
      </div>

      <div className="assumptions-actions">
        {/* Unavailable while saving, but not `disabled`: a disabled button drops keyboard focus. */}
        <button type="submit" className="button-secondary" aria-disabled={saving}>
          {saving ? 'Saving…' : 'Save assumptions'}
        </button>
        {saved && (
          <p className="assumptions-saved" role="status">
            Saved. The SortView Value figures have been worked out again.
          </p>
        )}
      </div>
    </form>
  )
}

/** What is stored, to be read: for an organization whose settings cannot be changed at present. */
function StoredAssumptions({ settings }: { settings: SortViewValueSettings }) {
  const { reporting, troubleshooting } = settings
  const facts: Array<[string, string | null]> = [
    ['SortView Value labor rate', settings.labor_rate.sortview_value === null ? null : perHour(settings.labor_rate.sortview_value)],
    ['Annual subscription cost', settings.subscription_annual_cost === null ? null : perYear(settings.subscription_annual_cost)],
    ['Reporting tasks per month', reporting.tasks_per_month],
    ['Baseline minutes per reporting task', reporting.baseline_minutes_per_task],
    ['Current minutes per reporting task', reporting.current_minutes_per_task],
    ['Reporting figures', reporting.basis === null && reporting.as_of === null ? null : basisText(reporting.basis, reporting.as_of)],
    ['Troubleshooting incidents per month', troubleshooting.incidents_per_month],
    ['Staff per troubleshooting incident', troubleshooting.staff_per_incident],
    ['Baseline minutes per person per incident', troubleshooting.baseline_minutes],
    ['Current minutes per person per incident', troubleshooting.current_minutes],
    ['Troubleshooting figures', troubleshooting.basis === null && troubleshooting.as_of === null ? null : basisText(troubleshooting.basis, troubleshooting.as_of)],
  ]
  return (
    <dl className="account-facts">
      {facts.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value ?? 'Not set'}</dd>
        </div>
      ))}
    </dl>
  )
}

/** What the panel shows in place of the form while the stored values are not there to edit. */
function Unloaded({ stored, retry }: { stored: StoredValueSettings; retry: () => void }) {
  if (stored.status === 'loading') {
    return <p role="status">Loading…</p>
  }
  if (stored.status === 'forbidden') {
    return (
      <p className="notice" role="note">
        These can be changed by the organization&rsquo;s owners and administrators.
      </p>
    )
  }
  return (
    <div className="section-retry">
      <SectionPlaceholder status="error" />
      <button type="button" className="button-secondary" onClick={retry}>
        Try again
      </button>
    </div>
  )
}

function Editor({ orgSlug, readOnly, onSaved }: { orgSlug: string; readOnly: boolean; onSaved: () => void }) {
  const { stored, retry } = useSortViewValueSettings(orgSlug, true)
  const save = useSaveSortViewValue(orgSlug)

  if (stored.status !== 'ready' && stored.status !== 'unreadable') {
    return <Unloaded stored={stored} retry={retry} />
  }
  if (readOnly) {
    // A suspended organization's settings can be read and not changed: shown as they are stored, with nothing to
    // type into and nothing to save them with.
    return (
      <div className="assumptions-form">
        <h5 id={`${FORM}-form-heading`}>Assumptions</h5>
        <p className="notice" role="note">
          This organization is suspended, so its SortView Value assumptions cannot be changed.
        </p>
        {stored.status === 'unreadable' ? (
          <p className="notice" role="note">
            What is stored here could not be read.
          </p>
        ) : (
          <StoredAssumptions settings={stored.data} />
        )}
      </div>
    )
  }
  return (
    <ValueForm
      // What could not be read and then can is a different thing to edit: the form starts again from what is stored.
      key={stored.status}
      stored={stored.status === 'ready' ? stored.data : null}
      saving={save.isPending}
      onSave={async (drafts) => {
        const saved = await save.mutateAsync(inputOf(drafts))
        onSaved()
        return saved
      }}
    />
  )
}

/**
 * The "Edit assumptions" panel. `readOnly`: the organization cannot be
 * changed at present. `onSaved` is called once a save has been stored and
 * the report has been answered again.
 */
export function ValueAssumptionsPanel({ orgSlug, readOnly, onSaved }: { orgSlug: string; readOnly: boolean; onSaved: () => void }) {
  const [open, setOpen] = useState(false)
  const closed = readOnly ? 'View assumptions' : 'Edit assumptions'

  return (
    // `value-edit-panel`: all of this is for editing, and none of it is printed (index.css). What the figures were
    // worked out from is in the report itself, under "Assumptions used".
    <div className="assumptions-panel value-edit-panel">
      <button type="button" className="button-secondary" aria-expanded={open} aria-controls={PANEL} onClick={() => setOpen(!open)}>
        {open ? 'Hide assumptions' : closed}
      </button>
      <div id={PANEL} hidden={!open}>
        {open && <Editor orgSlug={orgSlug} readOnly={readOnly} onSaved={onSaved} />}
      </div>
    </div>
  )
}
