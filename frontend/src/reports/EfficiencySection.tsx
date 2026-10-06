import type { AssumedRate, EfficiencyReport } from '../api/efficiency.ts'
import { MetricCard, type Figure } from '../liveToday/MetricCard.tsx'
import type { DateRange } from './dateRange.ts'
import { formatCount, formatDate, NOT_AVAILABLE } from './derive.ts'
import { EfficiencyAssumptionsPanel } from './EfficiencyAssumptionsPanel.tsx'
import { formatDecimal, formatHours, formatMoney } from './efficiencyText.ts'
import { count, days } from './figures.ts'
import { ReportSection } from './ReportSections.tsx'
import { useEfficiencyReport } from './useEfficiency.ts'

/**
 * A sorter's Efficiency report: what was OBSERVED, and what is ESTIMATED
 * from it under the organization's own assumptions -- kept visibly apart,
 * because they are not equally certain.
 *
 *   observed    how many items were processed, and what the sorter's
 *               configured recurring cost comes to for those days
 *   estimated   the manual workload that volume would be at the assumed
 *               manual processing rate, and what that workload would cost
 *               at the configured labor rate
 *
 * The estimates say what the volume WOULD HAVE TAKEN by hand under an
 * assumption. They are not time, money or positions anyone is known to have
 * been spared, and nothing here says they are. Every figure is the API's:
 * nothing is worked out in the browser, and no figure is set against another.
 */

const SOURCE_TEXT = { organization: 'organization default', sorter: 'set for this sorter' } as const

/** A figure that needs an assumption: the figure, or words saying which assumption is not there. */
const estimate = (value: string | null, write: (value: string) => string, note: string, missingNote: string): Figure =>
  value === null ? { tone: 'empty', text: NOT_AVAILABLE, note: missingNote } : { tone: 'value', text: write(value), note }

function assumed(rate: AssumedRate | null, write: (value: string) => string): string {
  return rate === null ? 'Not configured' : `${write(rate.value)} (${SOURCE_TEXT[rate.source]})`
}

function Figures({ report }: { report: EfficiencyReport }) {
  const { assumptions, results } = report
  const counted = results.in_service_checkin_count
  const crossed = counted !== report.checkin_count
  const nothingConfigured = assumptions.manual_items_per_hour === null && assumptions.labor_rate === null && assumptions.recurring_annual_cost === null
  const inServiceDays = `For ${days(results.in_service_days)} in service`

  return (
    <>
      <p className="quiet">
        These figures are estimates based on configured assumptions. The counts and the recurring cost are what was
        recorded and configured; the workload and labor-value figures are estimates, not measurements.
      </p>
      {nothingConfigured && (
        <p className="notice" role="note">
          No Efficiency assumptions are configured yet, so only the processing count can be shown. Open Efficiency
          settings below to enter them.
        </p>
      )}

      <h5 id="efficiency-observed-heading">Observed</h5>
      <dl className="metrics metrics-wide" aria-labelledby="efficiency-observed-heading">
        <MetricCard label="Items processed" {...count(counted, crossed ? 'In-service processing' : `Over ${days(report.range.days)}`)} />
        <MetricCard
          label="Recurring cost for this period"
          {...estimate(results.recurring_cost, formatMoney, inServiceDays, 'Configure a recurring annual cost to show this.')}
        />
      </dl>
      {/* Only part of the range counts. Said under the cards, in a sentence: the card stays a figure. */}
      {crossed && (
        <p className="quiet">
          {formatCount(counted)} of {formatCount(report.checkin_count)} processed items occurred on or after the configured
          in-service date.
        </p>
      )}
      {assumptions.in_service_date === null && <p className="quiet">No in-service date is configured, so the full selected range is used.</p>}

      <h5 id="efficiency-estimated-heading">Estimated from assumptions</h5>
      <dl className="metrics metrics-wide" aria-labelledby="efficiency-estimated-heading">
        <MetricCard
          label="Estimated manual-workload equivalent"
          {...estimate(
            results.staff_time_equivalent_hours,
            formatHours,
            'Staff labor-hours at the assumed manual rate',
            'Configure a manual processing rate assumption to estimate equivalent manual workload.',
          )}
        />
        <MetricCard
          label="Estimated labor-value equivalent"
          {...estimate(
            results.labor_value_equivalent,
            formatMoney,
            'Estimated workload at the configured labor rate',
            assumptions.manual_items_per_hour === null
              ? 'Needs a manual processing rate assumption.'
              : 'Configure a labor rate to estimate a labor-value equivalent.',
          )}
        />
      </dl>
      <p className="estimate-caveat">
        <strong>Estimated manual-workload equivalent.</strong> Estimated from the configured manual processing rate. This
        represents the manual processing workload that the selected volume would require at that assumed rate; it is not a
        measurement of staff hours actually saved.
      </p>
      <p className="estimate-caveat">
        <strong>Estimated labor-value equivalent.</strong> Estimated manual-workload equivalent multiplied by the configured
        labor rate. This is not a budget or payroll saving; staff time may have been redirected to other work.
      </p>

      <h5 id="efficiency-details-heading">Assumptions and details</h5>
      <table className="data-table" aria-labelledby="efficiency-details-heading">
        <thead>
          <tr>
            <th scope="col">Detail</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Items processed in the selected range</th>
            <td>{formatCount(report.checkin_count)}</td>
          </tr>
          <tr>
            <th scope="row">Recurring cost per processed item</th>
            <td>{results.recurring_cost_per_item === null ? NOT_AVAILABLE : formatMoney(results.recurring_cost_per_item)}</td>
          </tr>
          <tr>
            <th scope="row">Configured manual processing rate (an assumption)</th>
            <td>{assumed(assumptions.manual_items_per_hour, (value) => `${formatDecimal(value)} items per staff labor-hour`)}</td>
          </tr>
          <tr>
            <th scope="row">Configured labor rate</th>
            <td>{assumed(assumptions.labor_rate, (value) => `${formatMoney(value)} per hour`)}</td>
          </tr>
          <tr>
            <th scope="row">Configured recurring annual cost</th>
            <td>{assumptions.recurring_annual_cost === null ? 'Not configured' : `${formatMoney(assumptions.recurring_annual_cost)} per year`}</td>
          </tr>
          <tr>
            <th scope="row">In-service date</th>
            <td>{assumptions.in_service_date === null ? 'Not configured' : formatDate(assumptions.in_service_date)}</td>
          </tr>
        </tbody>
      </table>
    </>
  )
}

/**
 * The fifth of a sorter's reports, for the organization's owners and admins.
 * It is read on its own: nothing that happens to it changes the other four.
 * `today` is the product's date, the latest an in-service date may be.
 */
export function EfficiencySection({ orgSlug, branchSlug, range, today }: { orgSlug: string; branchSlug: string; range: DateRange; today: string }) {
  const read = useEfficiencyReport(orgSlug, branchSlug, range)

  return (
    <ReportSection name="report-efficiency" heading="Efficiency" read={read}>
      {(view) => {
        switch (view.kind) {
          case 'forbidden':
            // The API's answer, whatever this page thought the user's role was. Nothing of the report is shown.
            return (
              <p className="notice" role="note">
                Efficiency is available to this organization&rsquo;s owners and administrators.
              </p>
            )
          case 'not_available':
            return <p className="quiet">Efficiency is not available for this sorter yet.</p>
          case 'unreadable':
            return (
              <>
                <p className="notice" role="note">
                  The Efficiency settings stored for this sorter or its organization could not be read, so no figures can
                  be shown. Open Efficiency settings below and save them again to repair this.
                </p>
                <EfficiencyAssumptionsPanel orgSlug={orgSlug} branchSlug={branchSlug} today={today} />
              </>
            )
          case 'report':
            return (
              <>
                <Figures report={view.report} />
                <EfficiencyAssumptionsPanel orgSlug={orgSlug} branchSlug={branchSlug} today={today} />
              </>
            )
        }
      }}
    </ReportSection>
  )
}
