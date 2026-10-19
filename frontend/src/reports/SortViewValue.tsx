import { useRef, type RefObject } from 'react'

import type { OrganizationDetail } from '../api/organizations.ts'
import type { CategoryResults, SortViewValueReport } from '../api/sortviewValue.ts'
import { MetricCard, type Figure } from '../liveToday/MetricCard.tsx'
import { hasExports } from '../pages/capabilities.ts'
import { formatDecimal, formatHours, formatMoney } from './efficiencyText.ts'
import { PrintButton } from './ReportActions.tsx'
import { ReportSection } from './ReportSections.tsx'
import { ValueAssumptionsPanel } from './SortViewValueForm.tsx'
import {
  basisText,
  DISCLOSURE,
  formatMonths,
  formatPercentText,
  formatSignedHours,
  isNegative,
  isZeroOrLess,
  laborRateText,
  missingText,
  NO_LABOR_RATE,
  NOT_AVAILABLE,
  perYear,
} from './sortviewValueText.ts'
import { useSortViewValueReport } from './useSortViewValue.ts'

/**
 * An organization's SortView Value report: an ESTIMATE of what its reporting
 * and troubleshooting work takes now beside what it took before SortView,
 * and what the difference is worth beside what SortView costs -- every part
 * of it from assumptions the organization entered itself.
 *
 * Every figure is the API's: nothing is worked out in the browser. A figure
 * that cannot be worked out is "Not available", in words, never 0. A figure
 * below zero is written as it is, with its minus sign and a plain sentence
 * saying what it means: it is not an error, and nothing marks it as one.
 *
 * There is no range of days here, and nothing to compare or download: the
 * report is not counted from a sorter's events.
 */

const NEEDS_BOTH = 'Needs both kinds of work to be complete.'

/** A figure, or words saying why there is none. */
const figure = (value: string | null, write: (value: string) => string, note: string, missingNote: string): Figure =>
  value === null ? { tone: 'empty', text: NOT_AVAILABLE, note: missingNote } : { tone: 'value', text: write(value), note }

function Headline({ report }: { report: SortViewValueReport }) {
  const { assumptions, results } = report
  const cost = assumptions.subscription_annual_cost
  const noValue = results.recovered_hours_per_month === null ? NEEDS_BOTH : 'Needs a labor rate.'
  const noNet = results.annual_value === null ? noValue : 'Needs the annual subscription cost.'

  return (
    <dl className="metrics metrics-wide" aria-labelledby="sortview-value-estimate-heading">
      <MetricCard
        label="Recovered hours per month"
        {...figure(results.recovered_hours_per_month, formatSignedHours, 'Reporting and troubleshooting together', NEEDS_BOTH)}
      />
      <MetricCard label="Monthly estimated value" {...figure(results.monthly_value, formatMoney, 'Recovered hours at the labor rate', noValue)} />
      <MetricCard label="Annual estimated value" {...figure(results.annual_value, formatMoney, 'Twelve months of the monthly value', noValue)} />
      <MetricCard label="Subscription annual cost" {...figure(cost, formatMoney, 'Entered by your organization', 'Not entered.')} />
      <MetricCard
        label="Net estimated annual value"
        {...figure(results.net_annual_value, formatMoney, 'Annual estimated value less the subscription cost', noNet)}
      />
      <MetricCard
        label="Estimated ROI"
        {...figure(
          results.roi_percent,
          formatPercentText,
          'Net estimated annual value over the subscription cost',
          // A return on a cost of nothing does not exist: said so, not shown as 0%.
          results.net_annual_value !== null && cost !== null ? 'Not available for a subscription cost of zero.' : noNet,
        )}
      />
      <MetricCard
        label="Payback period"
        {...figure(
          results.payback_months,
          formatMonths,
          'Months of estimated value needed to cover one year of subscription cost.',
          isZeroOrLess(results.monthly_value) && cost !== null
            ? 'The estimated monthly value is zero or less, so it does not cover the subscription cost.'
            : noNet,
        )}
      />
    </dl>
  )
}

const KINDS = [
  ['reporting', 'Reporting'],
  ['troubleshooting', 'Troubleshooting'],
] as const

function ByKindOfWork({ report }: { report: SortViewValueReport }) {
  const cell = (value: string | null, write: (value: string) => string) => (value === null ? NOT_AVAILABLE : write(value))
  const longer = KINDS.filter(([kind]) => isNegative(report.results[kind].recovered_hours_per_month))

  return (
    <>
      <h5 id="sortview-value-kinds-heading">By kind of work</h5>
      {/* eslint-disable-next-line jsx-a11y-x/no-noninteractive-tabindex -- a labelled table that scrolls sideways must take keyboard focus to be scrolled (axe: scrollable-region-focusable) */}
      <div className="table-scroll" role="group" aria-labelledby="sortview-value-kinds-heading" tabIndex={0}>
        <table className="data-table" aria-labelledby="sortview-value-kinds-heading">
          <thead>
            <tr>
              <th scope="col">Kind of work</th>
              <th scope="col">Baseline hours/month</th>
              <th scope="col">Current hours/month</th>
              <th scope="col">Recovered hours/month</th>
              <th scope="col">Monthly value</th>
              <th scope="col">Annual value</th>
            </tr>
          </thead>
          <tbody>
            {KINDS.map(([kind, name]) => {
              const results: CategoryResults = report.results[kind]
              return (
                <tr key={kind}>
                  <th scope="row">{name}</th>
                  <td>{cell(results.baseline_hours_per_month, formatHours)}</td>
                  <td>{cell(results.current_hours_per_month, formatHours)}</td>
                  <td>{cell(results.recovered_hours_per_month, formatSignedHours)}</td>
                  <td>{cell(results.monthly_value, formatMoney)}</td>
                  <td>{cell(results.annual_value, formatMoney)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {/* In words, beside the figure: what a minus sign here means. Not a fault, and not marked as one. */}
      {longer.map(([kind, name]) => (
        <p className="quiet" key={kind}>
          {name}: Current time is higher than the baseline for this work.
        </p>
      ))}
    </>
  )
}

function Assumptions({ report }: { report: SortViewValueReport }) {
  const { labor_rate: rate, subscription_annual_cost: cost, reporting, troubleshooting } = report.assumptions
  const set = (value: string | null, unit: string) => (value === null ? 'Not set' : `${formatDecimal(value)} ${unit}`)
  const rows: Array<[string, string]> = [
    ['Labor rate', laborRateText(rate) ?? `${NOT_AVAILABLE}. ${NO_LABOR_RATE}`],
    ['Annual subscription cost', cost === null ? 'Not set' : perYear(cost)],
    ['Reporting: tasks per month', set(reporting.tasks_per_month, 'tasks')],
    ['Reporting: baseline minutes per task', set(reporting.baseline_minutes_per_task, 'minutes')],
    ['Reporting: current minutes per task', set(reporting.current_minutes_per_task, 'minutes')],
    ['Reporting: basis of these figures', basisText(reporting.basis, reporting.as_of)],
    ['Troubleshooting: incidents per month', set(troubleshooting.incidents_per_month, 'incidents')],
    ['Troubleshooting: staff per incident', set(troubleshooting.staff_per_incident, 'staff')],
    ['Troubleshooting: baseline minutes per person', set(troubleshooting.baseline_minutes, 'minutes')],
    ['Troubleshooting: current minutes per person', set(troubleshooting.current_minutes, 'minutes')],
    ['Troubleshooting: basis of these figures', basisText(troubleshooting.basis, troubleshooting.as_of)],
  ]

  return (
    <>
      <h5 id="sortview-value-assumptions-heading">Assumptions used</h5>
      <table className="data-table" aria-labelledby="sortview-value-assumptions-heading">
        <thead>
          <tr>
            <th scope="col">Assumption</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td>{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function Estimate({ report, heading }: { report: SortViewValueReport; heading: RefObject<HTMLHeadingElement | null> }) {
  return (
    <>
      {report.missing.length > 0 && (
        <div className="notice" role="note" aria-labelledby="sortview-value-missing-heading">
          <p id="sortview-value-missing-heading">Some assumptions are not set, so some figures cannot be estimated. Not set:</p>
          <ul>
            {report.missing.map((input) => (
              <li key={input}>
                {missingText(input)}
                {input === 'labor_rate' && `. ${NO_LABOR_RATE}`}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Where focus goes once a save has been stored and these figures answered again. Not a tab stop. */}
      <h5 id="sortview-value-estimate-heading" ref={heading} tabIndex={-1}>
        Estimated value
      </h5>
      <Headline report={report} />
      <ByKindOfWork report={report} />
      <Assumptions report={report} />
    </>
  )
}

/**
 * The Value view of an organization's reports, for its owners and admins.
 * The report is read on arrival; the stored settings only when "Edit
 * assumptions" is opened. After a save the report is asked for again, and
 * focus moves to its heading.
 */
export function SortViewValue({ organization }: { organization: OrganizationDetail }) {
  const orgSlug = organization.slug
  const read = useSortViewValueReport(orgSlug)
  const estimateHeading = useRef<HTMLHeadingElement>(null)
  const panel = (
    <ValueAssumptionsPanel orgSlug={orgSlug} readOnly={organization.access_mode !== 'full'} onSaved={() => estimateHeading.current?.focus()} />
  )

  return (
    <>
      {/* Offered where the plan includes exports, as on every report. The print styles decide what goes on paper. */}
      <PrintButton enabled={hasExports(organization)} />
      <div className="report-sections">
        <ReportSection name="report-value" heading="SortView Value" read={read}>
          {(view) => {
            switch (view.kind) {
              case 'forbidden':
                // The API's answer, whatever this page thought the user's role was. Nothing of the report is shown.
                return (
                  <p className="notice" role="note">
                    SortView Value is available to this organization&rsquo;s owners and administrators.
                  </p>
                )
              case 'unreadable':
                return (
                  <>
                    <p className="estimate-caveat">{DISCLOSURE}</p>
                    <p className="notice" role="note">
                      The SortView Value assumptions stored for this organization could not be read, so no figures can be
                      shown. Open the assumptions below and save them again to repair this.
                    </p>
                    {panel}
                  </>
                )
              case 'report':
                return (
                  <>
                    <p className="estimate-caveat">{DISCLOSURE}</p>
                    <Estimate report={view.report} heading={estimateHeading} />
                    {panel}
                  </>
                )
            }
          }}
        </ReportSection>
      </div>
    </>
  )
}
