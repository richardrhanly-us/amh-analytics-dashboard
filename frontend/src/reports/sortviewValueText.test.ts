import { describe, expect, it } from 'vitest'

import { VALUE_INPUTS } from '../api/sortviewValue.ts'
import { emptyValueStore, valueBody, valueStore } from '../test/sortviewValue.ts'
import {
  basisText,
  DISCLOSURE,
  draftsOf,
  FIELD_LABELS,
  fieldKind,
  formatMonths,
  formatPercentText,
  formatSignedHours,
  inputOf,
  isNegative,
  isValueField,
  isZeroOrLess,
  laborRateText,
  missingText,
  perHour,
  perYear,
  VALUE_FIELDS,
  valueFieldProblem,
  valueProblemText,
  type ValueField,
} from './sortviewValueText.ts'

const settingsOf = (...changes: Parameters<typeof valueStore>) => valueBody.settings(valueStore(...changes)).sortview_value as Parameters<typeof draftsOf>[0] & object

describe('the words of SortView Value', () => {
  it('says what its figures are, exactly, and claims nothing was measured or saved', () => {
    expect(DISCLOSURE).toBe(
      'SortView Value figures are estimates based on workflow assumptions entered by your organization. They are intended for planning and evaluation and do not represent guaranteed cost savings.',
    )
    expect(DISCLOSURE).not.toMatch(/operational data|measured|hours saved/i)
  })

  it('writes a figure from its characters, with its sign in front', () => {
    expect(perHour('32.50')).toBe('$32.50 per hour')
    expect(perYear('4800.00')).toBe('$4,800.00 per year')
    expect(perYear('0.00')).toBe('$0.00 per year')
    expect(formatPercentText('49.0')).toBe('49.0%')
    expect(formatPercentText('-121.7')).toBe('-121.7%')
    expect(formatMonths('8.1')).toBe('8.1 months')
    expect(formatMonths('1.0')).toBe('1.0 month')
    expect(formatMonths('0.0')).toBe('0.0 months')
    expect(formatMonths('1200.5')).toBe('1,200.5 months')
    expect(formatSignedHours('18.33')).toBe('18.33 hours')
    expect(formatSignedHours('-6.00')).toBe('-6.00 hours')
    expect(formatSignedHours('-1250.50')).toBe('-1,250.50 hours')
    expect(formatSignedHours('0.00')).toBe('0.00 hours')
  })

  it('reads below zero, and zero or below, from how a figure is written', () => {
    expect(['-0.01', '-6.00'].map(isNegative)).toEqual([true, true])
    expect(['0.00', '0.01', '6.00', null].map(isNegative)).toEqual([false, false, false, false])
    expect(['-0.01', '0.00', '0.0'].map(isZeroOrLess)).toEqual([true, true, true])
    expect(['0.01', '10.00', '0.10', null].map(isZeroOrLess)).toEqual([false, false, false, false])
  })

  it('says what a kind of work’s figures are and when they are as of', () => {
    expect(basisText('estimate', '2026-09-01')).toBe('Estimate, as of Sep 1, 2026')
    expect(basisText('measured', '2026-09-15')).toBe('Measured, as of Sep 15, 2026')
    expect(basisText('estimate', null)).toBe('Estimate')
    expect(basisText(null, '2026-09-01')).toBe('As of Sep 1, 2026')
    expect(basisText(null, null)).toBe('Not set')
  })

  it('says whose the labor rate is, and nothing when there is none', () => {
    expect(laborRateText(settingsOf().labor_rate)).toBe('$32.50 per hour (set for SortView Value)')
    expect(laborRateText(settingsOf({ labor_rate: null, efficiency_labor_rate: '20.00' }).labor_rate)).toBe('$20.00 per hour (organization Efficiency default)')
    // Its own rate is the one in effect, whatever the default is.
    expect(laborRateText(settingsOf({ efficiency_labor_rate: '20.00' }).labor_rate)).toBe('$32.50 per hour (set for SortView Value)')
    expect(laborRateText(settingsOf({ labor_rate: null }).labor_rate)).toBeNull()
  })

  it('names every input the estimate uses, the labor rate as either rate', () => {
    expect(VALUE_INPUTS.map(missingText)).toEqual([
      'Labor rate',
      'Annual subscription cost',
      'Reporting tasks per month',
      'Baseline minutes per reporting task',
      'Current minutes per reporting task',
      'Troubleshooting incidents per month',
      'Staff per troubleshooting incident',
      'Baseline minutes per person per incident',
      'Current minutes per person per incident',
    ])
  })
})

describe('the fields of the form', () => {
  it('are the block’s thirteen, each with a label of its own, and each input the estimate uses is one of them', () => {
    expect(VALUE_FIELDS).toHaveLength(13)
    expect(new Set(Object.values(FIELD_LABELS)).size).toBe(13)
    expect(Object.keys(FIELD_LABELS)).toEqual([...VALUE_FIELDS])
    expect(VALUE_INPUTS.every(isValueField)).toBe(true)
    expect(VALUE_FIELDS.filter((field) => fieldKind(field) === 'basis')).toEqual(['reporting.basis', 'troubleshooting.basis'])
    expect(VALUE_FIELDS.filter((field) => fieldKind(field) === 'date')).toEqual(['reporting.as_of', 'troubleshooting.as_of'])
    expect(VALUE_FIELDS.filter((field) => fieldKind(field) === 'decimal')).toEqual([...VALUE_INPUTS])
    expect(['reporting', 'results.monthly_value', 'labor_rate.effective', ''].map(isValueField)).toEqual([false, false, false, false])
  })

  it('accepts nothing typed in any field: it clears it', () => {
    for (const field of VALUE_FIELDS) {
      expect(valueFieldProblem(field, '')).toBeNull()
      expect(valueFieldProblem(field, '   ')).toBeNull()
    }
  })

  // The API's own limits (services.sortview_value): each field's largest value, one step past it, and its smallest.
  it.each<[ValueField, string, string, string, string]>([
    ['labor_rate', '0.01', '10000', '10000.01', '0.001'],
    ['subscription_annual_cost', '0', '10000000.00', '10000000.01', '0.001'],
    ['reporting.tasks_per_month', '0', '100000', '100000.1', '0.01'],
    ['reporting.baseline_minutes_per_task', '0', '10000.0', '10000.1', '0.01'],
    ['reporting.current_minutes_per_task', '0.0', '10000', '10000.1', '0.01'],
    ['troubleshooting.incidents_per_month', '0', '100000', '100000.1', '0.01'],
    ['troubleshooting.staff_per_incident', '0.1', '1000', '1000.1', '0.01'],
    ['troubleshooting.baseline_minutes', '0', '10000', '10000.1', '0.01'],
    ['troubleshooting.current_minutes', '0', '10000', '10000.1', '0.01'],
  ])('%s takes %s to %s, and refuses %s and %s', (field, smallest, largest, tooLarge, tooFine) => {
    expect(valueFieldProblem(field, smallest)).toBeNull()
    expect(valueFieldProblem(field, largest)).toBeNull()
    expect(valueFieldProblem(field, ` ${largest} `)).toBeNull()
    expect(valueFieldProblem(field, tooLarge)).toBe('out_of_range')
    expect(valueFieldProblem(field, tooFine)).toBe('too_many_decimal_places')
    for (const typed of ['-1', '$5', '1,000', '1e3', 'abc', '1.', '.5', '1 0', '+1']) {
      expect(valueFieldProblem(field, typed)).toBe('not_a_decimal')
    }
  })

  it('refuses zero for the two that must be above it, and takes it for every other', () => {
    for (const field of VALUE_INPUTS) {
      const mustBeAbove = field === 'labor_rate' || field === 'troubleshooting.staff_per_incident'
      expect(valueFieldProblem(field, '0')).toBe(mustBeAbove ? 'out_of_range' : null)
      expect(valueFieldProblem(field, '0.0')).toBe(mustBeAbove ? 'out_of_range' : null)
    }
  })

  it('takes a basis that is one of the two, and a date that is a date -- whenever it is', () => {
    for (const field of ['reporting.basis', 'troubleshooting.basis'] as const) {
      expect(['estimate', 'measured'].map((typed) => valueFieldProblem(field, typed))).toEqual([null, null])
      expect(['Estimate', 'guess', '1'].map((typed) => valueFieldProblem(field, typed))).toEqual(['not_a_basis', 'not_a_basis', 'not_a_basis'])
    }
    for (const field of ['reporting.as_of', 'troubleshooting.as_of'] as const) {
      expect(valueFieldProblem(field, '2026-09-01')).toBeNull()
      expect(valueFieldProblem(field, '2024-02-29')).toBeNull()
      // Whether a date is after today is the API's to say: nothing here reads a clock.
      expect(valueFieldProblem(field, '2999-12-31')).toBeNull()
      for (const typed of ['2026-02-30', '2025-02-29', '09/01/2026', '2026-9-1', 'today', '2026-09-01T00:00']) {
        expect(valueFieldProblem(field, typed)).toBe('not_a_date')
      }
    }
  })

  it('has a sentence for every code, by the field, that never repeats what was typed', () => {
    expect(valueProblemText('labor_rate', 'out_of_range')).toBe('Enter an amount above 0, up to 10,000.')
    expect(valueProblemText('subscription_annual_cost', 'out_of_range')).toBe('Enter an amount from 0 to 10,000,000.')
    expect(valueProblemText('reporting.tasks_per_month', 'out_of_range')).toBe('Enter a number from 0 to 100,000.')
    expect(valueProblemText('troubleshooting.incidents_per_month', 'out_of_range')).toBe('Enter a number from 0 to 100,000.')
    expect(valueProblemText('troubleshooting.staff_per_incident', 'out_of_range')).toBe('Enter a number above 0, up to 1,000.')
    expect(valueProblemText('reporting.baseline_minutes_per_task', 'out_of_range')).toBe('Enter a number of minutes from 0 to 10,000.')
    expect(valueProblemText('troubleshooting.current_minutes', 'out_of_range')).toBe('Enter a number of minutes from 0 to 10,000.')
    expect(valueProblemText('labor_rate', 'too_many_decimal_places')).toBe('Use at most 2 decimal places. The value is not rounded for you.')
    expect(valueProblemText('reporting.tasks_per_month', 'too_many_decimal_places')).toBe('Use at most 1 decimal place. The value is not rounded for you.')
    expect(valueProblemText('labor_rate', 'not_a_decimal')).toBe('Enter digits only, with a decimal point if needed: no symbols, commas or spaces.')
    expect(valueProblemText('labor_rate', 'not_a_string')).toBe(valueProblemText('labor_rate', 'not_a_decimal'))
    expect(valueProblemText('labor_rate', 'something_new')).toBe('This value was not accepted.')
    expect(valueProblemText('reporting.as_of', 'in_the_future')).toBe('The date cannot be after today.')
    expect(valueProblemText('reporting.as_of', 'not_a_date')).toBe('Enter a date, or leave it blank.')
    expect(valueProblemText('troubleshooting.as_of', 'something_new')).toBe('Enter a date, or leave it blank.')
    expect(valueProblemText('reporting.basis', 'not_a_basis')).toBe('Choose Estimate or Measured, or leave it not set.')
  })
})

describe('what the form holds and what it sends', () => {
  it('holds each value as the API wrote it, and empty where nothing is set', () => {
    expect(draftsOf(settingsOf())).toEqual({
      labor_rate: '32.50',
      subscription_annual_cost: '4800.00',
      'reporting.tasks_per_month': '12.0',
      'reporting.baseline_minutes_per_task': '90.0',
      'reporting.current_minutes_per_task': '15.0',
      'reporting.basis': 'estimate',
      'reporting.as_of': '2026-09-01',
      'troubleshooting.incidents_per_month': '4.0',
      'troubleshooting.staff_per_incident': '2.0',
      'troubleshooting.baseline_minutes': '45.0',
      'troubleshooting.current_minutes': '20.0',
      'troubleshooting.basis': 'measured',
      'troubleshooting.as_of': '2026-09-15',
    })
    const empty = valueBody.settings(emptyValueStore()).sortview_value as Parameters<typeof draftsOf>[0]
    expect(Object.values(draftsOf(empty))).toEqual(VALUE_FIELDS.map(() => ''))
    expect(Object.values(draftsOf(null))).toEqual(VALUE_FIELDS.map(() => ''))
    expect(Object.keys(draftsOf(null))).toEqual([...VALUE_FIELDS])
  })

  it('never puts the Efficiency default in the labor-rate field: it is not this form’s to edit', () => {
    expect(draftsOf(settingsOf({ labor_rate: null, efficiency_labor_rate: '20.00' })).labor_rate).toBe('')
    expect(draftsOf(settingsOf({ efficiency_labor_rate: '20.00' })).labor_rate).toBe('32.50')
  })

  it('sends every field as typed, less the space around it, and null for nothing', () => {
    const drafts = { ...draftsOf(null), labor_rate: ' 32.5 ', subscription_annual_cost: '0', 'reporting.tasks_per_month': '12', 'reporting.basis': 'estimate', 'troubleshooting.as_of': '2026-09-15', 'troubleshooting.current_minutes': '   ' }

    expect(inputOf(drafts)).toStrictEqual({
      labor_rate: '32.5',
      subscription_annual_cost: '0',
      reporting: { tasks_per_month: '12', baseline_minutes_per_task: null, current_minutes_per_task: null, basis: 'estimate', as_of: null },
      troubleshooting: { incidents_per_month: null, staff_per_incident: null, baseline_minutes: null, current_minutes: null, basis: null, as_of: '2026-09-15' },
    })
  })

  it('sends back what is stored unchanged when nothing is typed', () => {
    const stored = settingsOf()

    expect(inputOf(draftsOf(stored))).toStrictEqual({
      labor_rate: '32.50',
      subscription_annual_cost: '4800.00',
      reporting: stored.reporting,
      troubleshooting: stored.troubleshooting,
    })
  })
})
