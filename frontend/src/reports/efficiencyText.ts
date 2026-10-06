import { isCalendarDate } from './dateRange.ts'

/**
 * How an Efficiency figure is written, and what may be typed into an
 * Efficiency field. Pure: no clock, no request.
 *
 * EVERYTHING HERE IS TEXT. The API sends a rate, an amount or a number of
 * hours as a decimal string and wants one back, so a figure is formatted by
 * rearranging its characters and a typed value is checked by reading them --
 * never by turning either into a JavaScript number, which could not hold it
 * exactly. Nothing typed is rounded or tidied: it is sent as written (less
 * the space around it) or it is refused, and the API has the last word.
 */

// --- writing a figure ------------------------------------------------------------------------------------------------

/** The digits before the decimal point, grouped in threes: "1225" is "1,225". */
function grouped(decimal: string): string {
  const [whole, fraction] = decimal.split('.')
  const withSeparators = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return fraction === undefined ? withSeparators : `${withSeparators}.${fraction}`
}

/** A decimal string as US dollars, every digit kept: "1225.30" is "$1,225.30", "0.2199" is "$0.2199". */
export function formatMoney(decimal: string): string {
  return decimal.startsWith('-') ? `-$${grouped(decimal.slice(1))}` : `$${grouped(decimal)}`
}

/** A decimal string of hours: "69.78" is "69.78 hours". */
export function formatHours(decimal: string): string {
  return `${grouped(decimal)} ${decimal === '1.00' ? 'hour' : 'hours'}`
}

/** A decimal string with separators and nothing else: "1250.0" is "1,250.0". */
export function formatDecimal(decimal: string): string {
  return grouped(decimal)
}

// --- what may be typed -----------------------------------------------------------------------------------------------

export type EfficiencyField = 'labor_rate' | 'manual_items_per_hour' | 'one_time_cost' | 'recurring_annual_cost' | 'in_service_date'

interface DecimalRule {
  places: number
  /** The smallest and largest value, as whole numbers. `aboveMinimum`: the smallest itself is not allowed. */
  minimum: number
  maximum: number
  aboveMinimum?: boolean
}

/** The API's own limits (services.efficiency_settings), mirrored so a mistake is caught before it is sent. */
const DECIMAL_RULES: Record<Exclude<EfficiencyField, 'in_service_date'>, DecimalRule> = {
  labor_rate: { places: 2, minimum: 0, maximum: 1000, aboveMinimum: true },
  manual_items_per_hour: { places: 1, minimum: 1, maximum: 1000 },
  one_time_cost: { places: 2, minimum: 0, maximum: 100_000_000 },
  recurring_annual_cost: { places: 2, minimum: 0, maximum: 10_000_000 },
}

/** The codes the API gives for a refused field, which this module gives too. */
export type ProblemCode = 'not_a_decimal' | 'too_many_decimal_places' | 'out_of_range' | 'not_a_date' | 'in_the_future'

const DECIMAL_TEXT = /^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/

function decimalProblem(text: string, rule: DecimalRule): ProblemCode | null {
  const match = text.length <= 32 ? DECIMAL_TEXT.exec(text) : null
  if (match === null) {
    return 'not_a_decimal'
  }
  const fraction = match[2] ?? ''
  if (fraction.length > rule.places) {
    return 'too_many_decimal_places'
  }
  // Compared as whole numbers of the field's smallest step, so no fraction is ever a floating-point one.
  const scale = 10n ** BigInt(rule.places)
  const value = BigInt(match[1]) * scale + BigInt(fraction.padEnd(rule.places, '0') || '0')
  const minimum = BigInt(rule.minimum) * scale
  const tooSmall = rule.aboveMinimum ? value <= minimum : value < minimum
  return tooSmall || value > BigInt(rule.maximum) * scale ? 'out_of_range' : null
}

/**
 * What is wrong with what was typed into a field, or null if it can be
 * sent. Nothing typed (after the space around it is dropped) is always
 * fine: it clears the field. `today` is the product's date, the latest an
 * in-service date may be.
 */
export function fieldProblem(field: EfficiencyField, typed: string, today: string): ProblemCode | null {
  const text = typed.trim()
  if (text === '') {
    return null
  }
  if (field === 'in_service_date') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text) || !isCalendarDate(text)) {
      return 'not_a_date'
    }
    return text > today ? 'in_the_future' : null
  }
  return decimalProblem(text, DECIMAL_RULES[field])
}

/** What a field sends: the text as typed without the space around it, or null for nothing. Never rewritten. */
export function fieldValue(typed: string): string | null {
  const text = typed.trim()
  return text === '' ? null : text
}

const RANGE_TEXT: Record<Exclude<EfficiencyField, 'in_service_date'>, string> = {
  labor_rate: 'Enter an amount above 0, up to 1,000.',
  manual_items_per_hour: 'Enter a rate from 1 to 1,000.',
  one_time_cost: 'Enter an amount from 0 to 100,000,000.',
  recurring_annual_cost: 'Enter an amount from 0 to 10,000,000.',
}

/**
 * The sentence for a refused field, by the field and the code -- this
 * module's own code or the API's. A code this app does not know gets one
 * plain sentence. No sentence ever repeats what was typed.
 */
export function problemText(field: EfficiencyField, code: string): string {
  if (field === 'in_service_date') {
    return code === 'in_the_future' ? 'The in-service date cannot be after today.' : 'Enter a date, or leave it blank.'
  }
  const { places } = DECIMAL_RULES[field]
  switch (code) {
    case 'too_many_decimal_places':
      return places === 1 ? 'Use at most 1 decimal place. The value is not rounded for you.' : `Use at most ${places} decimal places. The value is not rounded for you.`
    case 'out_of_range':
      return RANGE_TEXT[field]
    case 'not_a_decimal':
    case 'not_a_string':
      return 'Enter digits only, with a decimal point if needed: no symbols, commas or spaces.'
    default:
      return 'This value was not accepted.'
  }
}
