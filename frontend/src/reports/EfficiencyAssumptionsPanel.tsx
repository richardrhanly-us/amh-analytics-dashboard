import { useState, type FormEvent, type ReactNode } from 'react'

import {
  EFFICIENCY_SETTINGS_INVALID,
  settingProblems,
  type OrganizationEfficiencySettings,
  type SorterEfficiencySettings,
  type SorterRateSetting,
} from '../api/efficiency.ts'
import { isApiError } from '../api/client.ts'
import { messageFor } from '../components/errorText.ts'
import { SectionPlaceholder } from '../liveToday/SectionPlaceholder.tsx'
import { fieldProblem, fieldValue, formatDecimal, formatMoney, problemText, type EfficiencyField } from './efficiencyText.ts'
import {
  useOrganizationAssumptions,
  useSaveOrganizationAssumptions,
  useSaveSorterAssumptions,
  useSorterAssumptions,
  type StoredAssumptions,
} from './useEfficiency.ts'

/**
 * Where an organization's owners and admins enter what Efficiency is worked
 * out from. Closed until asked for, and nothing is read until it is opened:
 * the report is the subject of the page, and this is beside it.
 *
 * Two forms, saved separately: the organization's defaults, and this one
 * sorter's own figures. A value is sent exactly as typed, or not at all --
 * nothing is rounded, and nothing shown elsewhere changes until the API has
 * stored it.
 *
 * THE MANUAL PROCESSING RATE IS AN ASSUMPTION, and is called one wherever it
 * appears. There is no built-in rate, and none is suggested.
 */

type Drafts<F extends EfficiencyField> = Record<F, string>

interface FieldSpec<F extends EfficiencyField> {
  field: F
  label: string
  /** Shown under the field, always: what the value is, and what it is used for. */
  help: ReactNode
  kind: 'decimal' | 'date'
}

const perHour = (rate: string) => `${formatMoney(rate)} per hour`
const itemsPerHour = (rate: string) => `${formatDecimal(rate)} items per staff labor-hour`

/** A form for one level. `onSave` resolves with what is then stored, as the form's own drafts. */
function AssumptionsForm<F extends EfficiencyField>({
  name,
  heading,
  intro,
  fields,
  initial,
  today,
  saving,
  onSave,
}: {
  name: string
  heading: string
  intro: ReactNode
  fields: ReadonlyArray<FieldSpec<F>>
  initial: Drafts<F>
  today: string
  saving: boolean
  onSave: (values: Record<F, string | null>) => Promise<Drafts<F>>
}) {
  const [drafts, setDrafts] = useState<Drafts<F>>(initial)
  const [problems, setProblems] = useState<Partial<Record<F, string>>>({})
  const [failure, setFailure] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving) {
      return
    }
    setSaved(false)
    setFailure(null)

    const found: Partial<Record<F, string>> = {}
    for (const { field } of fields) {
      const code = fieldProblem(field, drafts[field], today)
      if (code !== null) {
        found[field] = problemText(field, code)
      }
    }
    setProblems(found)
    if (Object.keys(found).length > 0) {
      setFailure('Nothing was saved. Check the fields marked below.')
      return
    }

    const values = Object.fromEntries(fields.map(({ field }) => [field, fieldValue(drafts[field])])) as Record<F, string | null>
    try {
      setDrafts(await onSave(values))
      setSaved(true)
    } catch (caught) {
      // The API's own word on each field, by its stable code. Never the value that was sent.
      const refused = settingProblems(caught)
      const mine = (refused ?? []).filter((problem) => fields.some(({ field }) => field === problem.field))
      if (refused !== null && mine.length > 0) {
        setProblems(Object.fromEntries(mine.map((problem) => [problem.field, problemText(problem.field as F, problem.code)])) as Partial<Record<F, string>>)
        setFailure('Nothing was saved. Check the fields marked below.')
      } else if (isApiError(caught) && caught.code === EFFICIENCY_SETTINGS_INVALID) {
        setFailure('Nothing was saved: the organization defaults that are stored could not be read. Save the organization defaults first, then save this again.')
      } else {
        setFailure(`Nothing was saved. ${messageFor(caught)}`)
      }
    }
  }

  return (
    <form className="assumptions-form" onSubmit={handleSubmit} aria-labelledby={`${name}-heading`} noValidate>
      <h6 id={`${name}-heading`}>{heading}</h6>
      <div className="quiet">{intro}</div>

      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}

      {fields.map(({ field, label, help, kind }) => {
        const problem = problems[field]
        const inputName = `${name}-${field}`
        return (
          <div className="field" key={field}>
            <label htmlFor={inputName}>{label}</label>
            <input
              id={inputName}
              name={field}
              type={kind === 'date' ? 'date' : 'text'}
              inputMode={kind === 'date' ? undefined : 'decimal'}
              autoComplete="off"
              spellCheck={false}
              max={kind === 'date' ? today : undefined}
              value={drafts[field]}
              aria-invalid={problem !== undefined}
              aria-describedby={problem === undefined ? `${inputName}-help` : `${inputName}-help ${inputName}-problem`}
              onChange={(event) => {
                setDrafts({ ...drafts, [field]: event.target.value })
                setSaved(false)
              }}
            />
            <div className="field-help" id={`${inputName}-help`}>
              {help}
            </div>
            {problem !== undefined && (
              <p className="field-problem" id={`${inputName}-problem`}>
                {problem}
              </p>
            )}
          </div>
        )
      })}

      <div className="assumptions-actions">
        {/* Unavailable while saving, but not `disabled`: a disabled button drops keyboard focus. */}
        <button type="submit" className="button-secondary" aria-disabled={saving}>
          {saving ? 'Saving…' : `Save ${heading.toLowerCase()}`}
        </button>
        {saved && (
          <p className="assumptions-saved" role="status">
            Saved. The Efficiency figures are being worked out again.
          </p>
        )}
      </div>
    </form>
  )
}

/** What a form shows in place of itself while its stored values are not there to edit. */
function Unloaded({ stored, retry }: { stored: StoredAssumptions<unknown>; retry: () => void }) {
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

const UNREADABLE = (
  <p className="notice" role="note">
    What is stored here could not be read, so the fields start empty. Saving replaces it.
  </p>
)

function OrganizationDefaults({ orgSlug, today }: { orgSlug: string; today: string }) {
  const { stored, retry } = useOrganizationAssumptions(orgSlug, true)
  const save = useSaveOrganizationAssumptions(orgSlug)

  if (stored.status !== 'ready' && stored.status !== 'unreadable') {
    return (
      <div className="assumptions-form">
        <h6>Organization defaults</h6>
        <Unloaded stored={stored} retry={retry} />
      </div>
    )
  }
  const drafts = (settings: OrganizationEfficiencySettings | null) => ({
    labor_rate: settings?.labor_rate ?? '',
    manual_items_per_hour: settings?.manual_items_per_hour ?? '',
  })

  return (
    <AssumptionsForm
      // What could not be read and then can is a different thing to edit: the form starts again from what is stored.
      key={stored.status}
      name="efficiency-organization"
      heading="Organization defaults"
      intro={
        <>
          <p>Organization defaults apply to sorting machines that do not have their own override.</p>
          {stored.status === 'unreadable' && UNREADABLE}
        </>
      }
      fields={[
        {
          field: 'labor_rate',
          label: 'Hourly labor rate (USD per hour)',
          kind: 'decimal',
          help: <p>Used to put a labor-value equivalent on the estimated manual workload. Leave blank for none.</p>,
        },
        {
          field: 'manual_items_per_hour',
          label: 'Manual processing rate assumption (items per staff labor-hour)',
          kind: 'decimal',
          help: (
            <>
              <p>Items per staff labor-hour used to estimate equivalent manual workload.</p>
              <p>
                This value is an assumption used for workload estimates. It should ideally be based on local observation or
                a documented workflow study. SortView supplies no rate of its own: leave it blank and no workload is
                estimated.
              </p>
            </>
          ),
        },
      ]}
      initial={drafts(stored.status === 'ready' ? stored.data : null)}
      today={today}
      saving={save.isPending}
      onSave={async (values) => drafts(await save.mutateAsync(values))}
    />
  )
}

/** In words: the organization's default, this sorter's own value, and which of the two is in effect. */
function Inheritance({ rate, write, what }: { rate: SorterRateSetting | null; write: (value: string) => string; what: string }) {
  if (rate === null) {
    return null
  }
  return (
    <p>
      Organization default: {rate.organization === null ? 'none set' : write(rate.organization)}. This sorter&rsquo;s override:{' '}
      {rate.sorter === null ? 'none' : write(rate.sorter)}.{' '}
      {rate.source === 'sorter' && `In effect: ${write(rate.sorter as string)}, this sorter’s own.`}
      {rate.source === 'organization' && `In effect: ${write(rate.organization as string)}, inherited from the organization default.`}
      {rate.source === null && `No ${what} is configured, so the estimate is unavailable until an assumption is configured.`}
    </p>
  )
}

function ThisSorter({ orgSlug, branchSlug, today }: { orgSlug: string; branchSlug: string; today: string }) {
  const { stored, retry } = useSorterAssumptions(orgSlug, branchSlug, true)
  const save = useSaveSorterAssumptions(orgSlug, branchSlug)

  if (stored.status !== 'ready' && stored.status !== 'unreadable') {
    return (
      <div className="assumptions-form">
        <h6>This sorter</h6>
        <Unloaded stored={stored} retry={retry} />
      </div>
    )
  }
  const current = stored.status === 'ready' ? stored.data : null
  const drafts = (settings: SorterEfficiencySettings | null) => ({
    labor_rate: settings?.labor_rate.sorter ?? '',
    manual_items_per_hour: settings?.manual_items_per_hour.sorter ?? '',
    one_time_cost: settings?.one_time_cost ?? '',
    recurring_annual_cost: settings?.recurring_annual_cost ?? '',
    in_service_date: settings?.in_service_date ?? '',
  })

  return (
    <AssumptionsForm
      key={stored.status}
      name="efficiency-sorter"
      heading="This sorter"
      intro={
        <>
          <p>This sorting machine&rsquo;s own figures. A rate left blank is inherited from the organization default.</p>
          {stored.status === 'unreadable' && UNREADABLE}
        </>
      }
      fields={[
        {
          field: 'labor_rate',
          label: 'Labor rate override (USD per hour)',
          kind: 'decimal',
          help: (
            <>
              <p>Leave blank to inherit the organization default.</p>
              <Inheritance rate={current?.labor_rate ?? null} write={perHour} what="labor rate" />
            </>
          ),
        },
        {
          field: 'manual_items_per_hour',
          label: 'Manual processing rate assumption override (items per staff labor-hour)',
          kind: 'decimal',
          help: (
            <>
              <p>
                Items per staff labor-hour used to estimate equivalent manual workload. An assumption, not a measurement.
                Leave blank to inherit the organization default.
              </p>
              <Inheritance rate={current?.manual_items_per_hour ?? null} write={itemsPerHour} what="manual processing rate assumption" />
            </>
          ),
        },
        {
          field: 'one_time_cost',
          label: 'One-time cost (USD)',
          kind: 'decimal',
          help: <p>What the sorter cost to buy and install. Kept for reference: no figure shown here uses it yet. Blank means not known; 0 means none.</p>,
        },
        {
          field: 'recurring_annual_cost',
          label: 'Recurring annual cost (USD per year)',
          kind: 'decimal',
          help: <p>What the sorter costs to keep each year. Charged by the day to the range shown. Blank means not known; 0 means none.</p>,
        },
        {
          field: 'in_service_date',
          label: 'In-service date',
          kind: 'date',
          help: <p>The date the sorter went into service. Only items processed on or after it are counted. Blank: the full selected range is used.</p>,
        },
      ]}
      initial={drafts(current)}
      today={today}
      saving={save.isPending}
      onSave={async (values) => drafts(await save.mutateAsync(values))}
    />
  )
}

export function EfficiencyAssumptionsPanel({ orgSlug, branchSlug, today }: { orgSlug: string; branchSlug: string; today: string }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="assumptions-panel">
      <button type="button" className="button-secondary" aria-expanded={open} aria-controls="efficiency-assumptions" onClick={() => setOpen(!open)}>
        {open ? 'Hide Efficiency settings' : 'Efficiency settings'}
      </button>
      <div id="efficiency-assumptions" hidden={!open}>
        {open && (
          <>
            <p className="quiet">
              These are assumptions and configured costs, entered by your organization. Each form is saved on its own.
              Values are stored exactly as entered and are never rounded.
            </p>
            <div className="assumptions-forms">
              <OrganizationDefaults orgSlug={orgSlug} today={today} />
              <ThisSorter orgSlug={orgSlug} branchSlug={branchSlug} today={today} />
            </div>
          </>
        )}
      </div>
    </div>
  )
}
