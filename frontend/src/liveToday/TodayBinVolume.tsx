import type { BinVolumeBin, BinVolumeReport } from '../api/reports.ts'
import { BinBars } from '../reports/BinVolumeSection.tsx'
import { binLabel, checkIns } from '../reports/binText.ts'
import { Figure } from '../reports/ChartFigure.tsx'
import { formatCount, formatPercent, percentOf } from '../reports/derive.ts'
import { count, derived } from '../reports/figures.ts'
import { MetricCard } from './MetricCard.tsx'
import { SectionPlaceholder } from './SectionPlaceholder.tsx'
import type { Section } from './useLiveToday.ts'

/**
 * Today's figures, drawn as the Operations report draws a range's: the same
 * cards, bars and shares. A bin's share is of the KNOWN-bin check-ins; bin
 * coverage says how many of all of today's check-ins had a recognized bin.
 */
function BinsToday({ report }: { report: BinVolumeReport }) {
  const { bins } = report
  const total = report.checkin_count
  const known = report.known_bin_count
  const unknown = report.unknown_bin_count

  if (total === 0) {
    return <p className="report-empty">No check-ins yet today.</p>
  }

  const busiest = bins.reduce<BinVolumeBin | null>((top, bin) => (top === null || bin.checkin_count > top.checkin_count ? bin : top), null)

  return (
    <>
      <dl className="metrics">
        <MetricCard label="Known-bin check-ins" {...count(known, `Of ${checkIns(total)}`)} />
        <MetricCard label="Bins observed" {...count(bins.length, 'With check-ins today')} />
        {unknown > 0 && <MetricCard label="Unknown-bin check-ins" {...count(unknown, 'No recognized bin value')} />}
        <MetricCard label="Bin coverage" {...derived(percentOf(known, total), formatPercent, 'Check-ins with a recognized bin')} />
      </dl>

      {unknown > 0 && known > 0 && (
        <p className="notice" role="note">
          {checkIns(unknown)} did not have a recognized bin value. {unknown === 1 ? 'It is' : 'They are'} counted here
          and {unknown === 1 ? 'is' : 'are'} not part of any bin below.
        </p>
      )}

      {busiest === null ? (
        <p className="report-empty">Check-ins were recorded today, but none had a recognized bin value.</p>
      ) : (
        <Figure
          name="today-bin-checkins"
          headingLevel={4}
          heading="Check-ins by bin today"
          summary={`${checkIns(known)} across ${formatCount(bins.length)} observed ${bins.length === 1 ? 'bin' : 'bins'}. Most in one bin: ${formatCount(busiest.checkin_count)}, in ${binLabel(busiest)}. Shares are of known-bin check-ins.`}
          columns={['Bin', 'Check-ins', 'Share of known-bin check-ins']}
          rows={bins.map((bin) => [binLabel(bin), formatCount(bin.checkin_count), formatPercent(percentOf(bin.checkin_count, known) as number)])}
        >
          {(summaryName) => (
            <BinBars label="Bar chart of today's check-ins in each observed bin" describedBy={summaryName} bins={bins} known={known} />
          )}
        </Figure>
      )}
    </>
  )
}

/**
 * Which sort bins received today's check-ins: the Operations report's Bin
 * volume for today alone. Only the bins observed today are listed, however
 * many that is -- which bins the sorter has is not known.
 */
export function TodayBinVolume({ binVolume }: { binVolume: Section<BinVolumeReport> }) {
  return (
    <section className="panel" aria-labelledby="today-bins-heading">
      <h3 id="today-bins-heading">Bin volume today</h3>
      {binVolume.status !== 'ready' ? (
        <SectionPlaceholder status={binVolume.status} />
      ) : (
        <>
          <p className="section-intro quiet">
            Only bins that received check-ins today are listed. SortView does not store the sorter’s configured bin
            inventory.
          </p>
          <BinsToday report={binVolume.data} />
        </>
      )}
    </section>
  )
}
