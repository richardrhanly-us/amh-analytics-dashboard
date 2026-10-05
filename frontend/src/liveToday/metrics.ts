import type { CheckinHourCount, RejectReason, RejectReasonCount } from '../api/liveToday.ts'

/** The figures Live Today works out from what the API returns. Pure: no clock, no request. */

/** Check-ins in the wall-clock hour `hour`. The API returns every hour, zero included, so this is never a guess. */
export function checkinsInHour(hours: readonly CheckinHourCount[], hour: number): number {
  return hours.find((entry) => entry.hour === hour)?.checkin_count ?? 0
}

/**
 * The hour with the most check-ins, or null when there were none at all --
 * a day with no activity has no busiest hour. When hours tie, the earliest
 * wins: the API lists hours in clock order and the first highest is kept.
 */
export function busiestHour(hours: readonly CheckinHourCount[]): CheckinHourCount | null {
  let busiest: CheckinHourCount | null = null
  for (const entry of hours) {
    if (entry.checkin_count > 0 && (busiest === null || entry.checkin_count > busiest.checkin_count)) {
      busiest = entry
    }
  }
  return busiest
}

/**
 * Rejects as a percentage of check-ins, or null when there were no check-ins:
 * a rate of nothing is not a number, and is not shown as one.
 */
export function rejectRate(rejectCount: number, checkinCount: number): number | null {
  return checkinCount > 0 ? (rejectCount / checkinCount) * 100 : null
}

/** A reject rate to one decimal place: "2.4%". */
export function formatRate(rate: number): string {
  return `${rate.toFixed(1)}%`
}

/**
 * The reasons that occurred, most frequent first. Reasons with the same count
 * stay in the API's own order. Reasons with no rejects are left out.
 */
export function topRejectReasons(reasons: readonly RejectReasonCount[]): RejectReasonCount[] {
  return reasons
    .filter((entry) => entry.reject_count > 0)
    .map((entry, index) => ({ entry, index }))
    .sort((a, b) => b.entry.reject_count - a.entry.reject_count || a.index - b.index)
    .map(({ entry }) => entry)
}

// Plain-language names for the API's reason codes. A name says what the code says and nothing more.
const REASON_LABELS: Record<RejectReason, string> = {
  item_not_found: 'Item not found',
  ils_acs_failure: 'ILS/ACS failure',
  rfid_collision: 'RFID collision',
  configuration_error: 'Configuration error',
  routing_error: 'Routing error',
  communication_error: 'Communication error',
  other: 'Other',
  unknown: 'Unknown',
}

export function reasonLabel(reason: RejectReason): string {
  return REASON_LABELS[reason]
}
