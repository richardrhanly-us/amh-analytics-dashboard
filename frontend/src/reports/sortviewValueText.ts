import type { SortViewValueInput, SortViewValueSettings, ValueInput } from '../api/sortviewValue.ts'
import { isCalendarDate } from './dateRange.ts'
import { formatDate } from './derive.ts'
import { decimalProblem, formatDecimal, formatHours, formatMoney, type DecimalRule } from './efficiencyText.ts'

/**
 * How a SortView Value figure is written, what may be typed into one of its
 * fields, and the words the page says. Pure: no clock, no request.
 *
 * EVERYTHING HERE IS TEXT, as for Efficiency (efficiencyText.ts): a figure is
 * formatted by rearranging its characters and a typed value is checked by
 * reading them -- never by turning either into a JavaScript number. Nothing
 * typed is rounded or tidied: it is sent as written (less the space around
 * it) or it is refused, and the API has the last word.
 *
 * NOTHING IS WORKED OUT. Whether a figure is negative is read from its first
 * character; no figure is compared with, added to or made from another.
 */

export const DISCLOSURE =
  'SortView Value figures are estimates based on workflow assumptions entered by your organization. They are intended for planning and evaluation and do not represent guaranteed cost savings.'

export const NOT_AVAILABLE = 'Not available'

// --- writing a figure ------------------------------------------------------------------------------------------------

export const perHour = (rate: string) => `${formatMoney(rate)} per hour`
export const perYear = (amount: string) => `${formatMoney(amount)} per year`

/** A percentage as the API wrote it: "49.0" is "49.0%", "-121.7" is "-121.7%". */
export const formatPercentText = (percent: string) => `${percent}%`

/** A number of months: "8.1" is "8.1 months", "1.0" is "1.0 month". */
export const formatMonths = (months: string) => `${formatDecimal(months)} ${months === '1.0' ? 'month' : 'months'}`

/** Hours that may be negative: "-6.00" is "-6.00 hours". The sign is kept in front, as written. */
export const formatSignedHours = (hours: string) => (hours.startsWith('-') ? `-${formatHours(hours.slice(1))}` : formatHours(hours))

/** A figure that is below zero, read from how it is written -- "-0.01", never "0.00". */
export const isNegative = (figure: string | null) => figure !== null && figure.startsWith('-')

/** A figure that is zero or below, read from how it is written. */
export const isZeroOrLess = (figure: string | null) => figure !== null && (figure.startsWith('-') || /^0\.0+$/.test(figure))

const BASIS_TEXT = { estimate: 'Estimate', measured: 'Measured' } as const

/** What a kind of work's figures are, and when they are as of: "Estimate, as of Sep 1, 2026". */
export function basisText(basis: 'estimate' | 'measured' | null, asOf: string | null): string {
  if (basis === null) {
    return asOf === null ? 'Not set' : `As of ${formatDate(asOf)}`
  }
  return asOf === null ? BASIS_TEXT[basis] : `${BASIS_TEXT[basis]}, as of ${formatDate(asOf)}`
}

const SOURCE_TEXT = { sortview_value: 'set for SortView Value', organization_efficiency: 'organization Efficiency default' } as const

/** The labor rate in effect and whose it is: "$20.00 per hour (organization Efficiency default)". Null when there is none. */
export function laborRateText(rate: SortViewValueSettings['labor_rate']): string | null {
  return rate.effective === null || rate.source === null ? null : `${perHour(rate.effective)} (${SOURCE_TEXT[rate.source]})`
}

export const NO_LABOR_RATE = 'A SortView Value labor rate can be entered, or the organization Efficiency default can supply it.'

// --- the fields ------------------------------------------------------------------------------------------------------

/** Every field of the form, by the name the API gives it: a kind of work's fields have it in front. */
export const VALUE_FIELDS = [
  'labor_rate',
  'subscription_annual_cost',
  'reporting.tasks_per_month',
  'reporting.baseline_minutes_per_task',
  'reporting.current_minutes_per_task',
  'reporting.basis',
  'reporting.as_of',
  'troubleshooting.incidents_per_month',
  'troubleshooting.staff_per_incident',
  'troubleshooting.baseline_minutes',
  'troubleshooting.current_minutes',
  'troubleshooting.basis',
  'troubleshooting.as_of',
] as const
export type ValueField = (typeof VALUE_FIELDS)[number]

export type FieldKind = 'decimal' | 'date' | 'basis'

export function fieldKind(field: ValueField): FieldKind {
  return field.endsWith('.basis') ? 'basis' : field.endsWith('.as_of') ? 'date' : 'decimal'
}

/** What a field is called: on its label, and wherever it is named as missing or at fault. */
export const FIELD_LABELS: Record<ValueField, string> = {
  labor_rate: 'SortView Value labor rate (USD per hour)',
  subscription_annual_cost: 'Annual subscription cost (USD per year)',
  'reporting.tasks_per_month': 'Reporting tasks per month',
  'reporting.baseline_minutes_per_task': 'Baseline minutes per reporting task',
  'reporting.current_minutes_per_task': 'Current minutes per reporting task',
  'reporting.basis': 'Basis of the reporting figures',
  'reporting.as_of': 'Reporting figures as of',
  'troubleshooting.incidents_per_month': 'Troubleshooting incidents per month',
  'troubleshooting.staff_per_incident': 'Staff per troubleshooting incident',
  'troubleshooting.baseline_minutes': 'Baseline minutes per person per incident',
  'troubleshooting.current_minutes': 'Current minutes per person per incident',
  'troubleshooting.basis': 'Basis of the troubleshooting figures',
  'troubleshooting.as_of': 'Troubleshooting figures as of',
}

/** What an input the estimate uses is called in the list of what is missing. */
export function missingText(input: ValueInput): string {
  return input === 'labor_rate' ? 'Labor rate' : FIELD_LABELS[input].replace(/ \(USD[^)]*\)$/, '')
}

type DecimalField = Exclude<ValueField, `${string}.basis` | `${string}.as_of`>

const PER_MONTH: DecimalRule = { places: 1, minimum: 0, maximum: 100_000 }
const MINUTES: DecimalRule = { places: 1, minimum: 0, maximum: 10_000 }

/** The API's own limits (services.sortview_value), mirrored so a mistake is caught before it is sent. */
const DECIMAL_RULES: Record<DecimalField, DecimalRule> = {
  labor_rate: { places: 2, minimum: 0, maximum: 10_000, aboveMinimum: true },
  subscription_annual_cost: { places: 2, minimum: 0, maximum: 10_000_000 },
  'reporting.tasks_per_month': PER_MONTH,
  'reporting.baseline_minutes_per_task': MINUTES,
  'reporting.current_minutes_per_task': MINUTES,
  'troubleshooting.incidents_per_month': PER_MONTH,
  'troubleshooting.staff_per_incident': { places: 1, minimum: 0, maximum: 1_000, aboveMinimum: true },
  'troubleshooting.baseline_minutes': MINUTES,
  'troubleshooting.current_minutes': MINUTES,
}

const RANGE_TEXT: Record<DecimalField, string> = {
  labor_rate: 'Enter an amount above 0, up to 10,000.',
  subscription_annual_cost: 'Enter an amount from 0 to 10,000,000.',
  'reporting.tasks_per_month': 'Enter a number from 0 to 100,000.',
  'reporting.baseline_minutes_per_task': 'Enter a number of minutes from 0 to 10,000.',
  'reporting.current_minutes_per_task': 'Enter a number of minutes from 0 to 10,000.',
  'troubleshooting.incidents_per_month': 'Enter a number from 0 to 100,000.',
  'troubleshooting.staff_per_incident': 'Enter a number above 0, up to 1,000.',
  'troubleshooting.baseline_minutes': 'Enter a number of minutes from 0 to 10,000.',
  'troubleshooting.current_minutes': 'Enter a number of minutes from 0 to 10,000.',
}

/** The codes the API gives for a refused field, which this module gives too -- all but the one only it can know. */
export type ValueProblemCode = 'not_a_decimal' | 'too_many_decimal_places' | 'out_of_range' | 'not_a_date' | 'not_a_basis'

/**
 * What is wrong with what was typed into a field, or null if it can be sent.
 * Nothing typed (after the space around it is dropped) is always fine: it
 * clears the field.
 *
 * A date is checked for being a date and no more. Whether it is after today
 * is the API's to say (`in_the_future`): today is the product's date, and
 * nothing on this page reads it.
 */
export function valueFieldProblem(field: ValueField, typed: string): ValueProblemCode | null {
  const text = typed.trim()
  if (text === '') {
    return null
  }
  switch (fieldKind(field)) {
    case 'basis':
      return text === 'estimate' || text === 'measured' ? null : 'not_a_basis'
    case 'date':
      return /^\d{4}-\d{2}-\d{2}$/.test(text) && isCalendarDate(text) ? null : 'not_a_date'
    case 'decimal':
      // A decimal is never refused for being a date: the two codes about dates are not among these.
      return decimalProblem(text, DECIMAL_RULES[field as DecimalField]) as ValueProblemCode | null
  }
}

/**
 * The sentence for a refused field, by the field and the code -- this
 * module's own code or the API's. A code this app does not know gets one
 * plain sentence. No sentence ever repeats what was typed.
 */
export function valueProblemText(field: ValueField, code: string): string {
  switch (fieldKind(field)) {
    case 'basis':
      return 'Choose Estimate or Measured, or leave it not set.'
    case 'date':
      return code === 'in_the_future' ? 'The date cannot be after today.' : 'Enter a date, or leave it blank.'
    case 'decimal': {
      const { places } = DECIMAL_RULES[field as DecimalField]
      switch (code) {
        case 'too_many_decimal_places':
          return places === 1 ? 'Use at most 1 decimal place. The value is not rounded for you.' : `Use at most ${places} decimal places. The value is not rounded for you.`
        case 'out_of_range':
          return RANGE_TEXT[field as DecimalField]
        case 'not_a_decimal':
        case 'not_a_string':
          return 'Enter digits only, with a decimal point if needed: no symbols, commas or spaces.'
        default:
          return 'This value was not accepted.'
      }
    }
  }
}

export function isValueField(name: string): name is ValueField {
  return (VALUE_FIELDS as readonly string[]).includes(name)
}

// --- what the form holds, and what it sends --------------------------------------------------------------------------

/** What is in each field of the form: text as typed, empty for nothing. */
export type ValueDrafts = Record<ValueField, string>

/** The form's fields for what is stored: each as the API wrote it, empty where nothing is set. */
export function draftsOf(settings: SortViewValueSettings | null): ValueDrafts {
  const reporting = settings?.reporting
  const troubleshooting = settings?.troubleshooting
  return {
    // The block's OWN rate: the Efficiency default is not this form's to edit, and is never put in the field.
    labor_rate: settings?.labor_rate.sortview_value ?? '',
    subscription_annual_cost: settings?.subscription_annual_cost ?? '',
    'reporting.tasks_per_month': reporting?.tasks_per_month ?? '',
    'reporting.baseline_minutes_per_task': reporting?.baseline_minutes_per_task ?? '',
    'reporting.current_minutes_per_task': reporting?.current_minutes_per_task ?? '',
    'reporting.basis': reporting?.basis ?? '',
    'reporting.as_of': reporting?.as_of ?? '',
    'troubleshooting.incidents_per_month': troubleshooting?.incidents_per_month ?? '',
    'troubleshooting.staff_per_incident': troubleshooting?.staff_per_incident ?? '',
    'troubleshooting.baseline_minutes': troubleshooting?.baseline_minutes ?? '',
    'troubleshooting.current_minutes': troubleshooting?.current_minutes ?? '',
    'troubleshooting.basis': troubleshooting?.basis ?? '',
    'troubleshooting.as_of': troubleshooting?.as_of ?? '',
  }
}

/** What a field sends: the text as typed without the space around it, or null for nothing. Never rewritten. */
const sent = (typed: string): string | null => (typed.trim() === '' ? null : typed.trim())

/** The whole block a save sends: every field, as typed, or null. */
export function inputOf(drafts: ValueDrafts): SortViewValueInput {
  return {
    labor_rate: sent(drafts.labor_rate),
    subscription_annual_cost: sent(drafts.subscription_annual_cost),
    reporting: {
      tasks_per_month: sent(drafts['reporting.tasks_per_month']),
      baseline_minutes_per_task: sent(drafts['reporting.baseline_minutes_per_task']),
      current_minutes_per_task: sent(drafts['reporting.current_minutes_per_task']),
      basis: sent(drafts['reporting.basis']),
      as_of: sent(drafts['reporting.as_of']),
    },
    troubleshooting: {
      incidents_per_month: sent(drafts['troubleshooting.incidents_per_month']),
      staff_per_incident: sent(drafts['troubleshooting.staff_per_incident']),
      baseline_minutes: sent(drafts['troubleshooting.baseline_minutes']),
      current_minutes: sent(drafts['troubleshooting.current_minutes']),
      basis: sent(drafts['troubleshooting.basis']),
      as_of: sent(drafts['troubleshooting.as_of']),
    },
  }
}
