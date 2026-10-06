import { describe, expect, it } from 'vitest'

import source from './efficiencyText.ts?raw'
import { fieldProblem, fieldValue, formatDecimal, formatHours, formatMoney, problemText, type EfficiencyField } from './efficiencyText.ts'

const TODAY = '2026-10-05'
const problem = (field: EfficiencyField, typed: string) => fieldProblem(field, typed, TODAY)

describe('writing a figure', () => {
  it('writes money as dollars with separators and every digit it was given', () => {
    expect(formatMoney('0.00')).toBe('$0.00')
    expect(formatMoney('17.50')).toBe('$17.50')
    expect(formatMoney('1225.30')).toBe('$1,225.30')
    expect(formatMoney('118003.92')).toBe('$118,003.92')
    expect(formatMoney('100000000.00')).toBe('$100,000,000.00')
    expect(formatMoney('0.2199')).toBe('$0.2199')
    expect(formatMoney('1234.5678')).toBe('$1,234.5678')
    expect(formatMoney('-690.41')).toBe('-$690.41')
  })

  it('never rounds, and holds values no JavaScript number could', () => {
    // 0.1 + 0.2, and a sum of money past 2^53 cents.
    expect(formatMoney('0.30')).toBe('$0.30')
    expect(formatMoney('91999997479452.05')).toBe('$91,999,997,479,452.05')
    expect(formatMoney('9007199254740993.01')).toBe('$9,007,199,254,740,993.01')
  })

  it('writes hours and rates the same way', () => {
    expect(formatHours('69.78')).toBe('69.78 hours')
    expect(formatHours('0.00')).toBe('0.00 hours')
    expect(formatHours('1.00')).toBe('1.00 hour')
    expect(formatHours('92000000000.00')).toBe('92,000,000,000.00 hours')
    expect(formatDecimal('40.0')).toBe('40.0')
    expect(formatDecimal('1000.0')).toBe('1,000.0')
  })
})

describe('what may be typed', () => {
  it('lets every field be left blank, which clears it', () => {
    for (const field of ['labor_rate', 'manual_items_per_hour', 'one_time_cost', 'recurring_annual_cost', 'in_service_date'] as const) {
      expect(problem(field, '')).toBeNull()
      expect(problem(field, '   ')).toBeNull()
    }
    expect(fieldValue('')).toBeNull()
    expect(fieldValue('  \t ')).toBeNull()
  })

  it('sends what was typed, less the space around it, and never rewrites it', () => {
    expect(fieldValue(' 17.5 ')).toBe('17.5')
    expect(fieldValue('17.50')).toBe('17.50')
    expect(fieldValue('0')).toBe('0')
    expect(fieldValue('0.00')).toBe('0.00')
    expect(fieldValue('40')).toBe('40')
  })

  it.each<[EfficiencyField, string[], Array<[string, string]>]>([
    [
      'labor_rate',
      ['0.01', '17.5', '17.56', '18', '1000', '1000.00', ' 25.00 '],
      [['0', 'out_of_range'], ['0.00', 'out_of_range'], ['1000.01', 'out_of_range'], ['1001', 'out_of_range'], ['17.567', 'too_many_decimal_places'], ['17.560', 'too_many_decimal_places']],
    ],
    [
      'manual_items_per_hour',
      ['1', '1.0', '40', '47.1', '1000', '1000.0'],
      [['0', 'out_of_range'], ['0.9', 'out_of_range'], ['1000.1', 'out_of_range'], ['45.25', 'too_many_decimal_places'], ['45.00', 'too_many_decimal_places']],
    ],
    [
      'one_time_cost',
      ['0', '0.00', '118003.92', '100000000', '100000000.00'],
      [['100000000.01', 'out_of_range'], ['100000001', 'out_of_range'], ['1.005', 'too_many_decimal_places']],
    ],
    [
      'recurring_annual_cost',
      ['0', '0.0', '8400', '10000000.00'],
      [['10000000.01', 'out_of_range'], ['8400.000', 'too_many_decimal_places']],
    ],
  ])('holds %s to the API’s own limits', (field, accepted, refused) => {
    for (const typed of accepted) {
      expect([typed, problem(field, typed)]).toEqual([typed, null])
    }
    for (const [typed, code] of refused) {
      expect([typed, problem(field, typed)]).toEqual([typed, code])
    }
  })

  it.each(['$17.56', '17,56', '1,000.00', '17.56 USD', '-17.56', '+17.56', '017.56', '00017.56', '1e2', 'NaN', 'Infinity', '17.', '.56', '1_000', '0x10', 'seventeen', '١٧', '1 7', '9'.repeat(33)])(
    'refuses %j as not a plain decimal, for every amount and rate',
    (typed) => {
      for (const field of ['labor_rate', 'manual_items_per_hour', 'one_time_cost', 'recurring_annual_cost'] as const) {
        expect(problem(field, typed)).toBe('not_a_decimal')
      }
    },
  )

  it('compares exactly: a value a floating-point number would get wrong is still judged right', () => {
    // 1000.00 is allowed and 1000.01 is not; 0.1 + 0.2 worth of cost is in range; 2^53 + 1 is far out of it.
    expect(problem('labor_rate', '1000.00')).toBeNull()
    expect(problem('labor_rate', '1000.01')).toBe('out_of_range')
    expect(problem('one_time_cost', '0.30')).toBeNull()
    expect(problem('one_time_cost', '9007199254740993')).toBe('out_of_range')
    expect(problem('one_time_cost', '99999999.99')).toBeNull()
  })

  it('takes an in-service date up to and including today, as a plain date', () => {
    for (const typed of ['2020-11-20', '2024-02-29', TODAY, '2026-10-04']) {
      expect(problem('in_service_date', typed)).toBeNull()
    }
    expect(problem('in_service_date', '2026-10-06')).toBe('in_the_future')
    expect(problem('in_service_date', '2999-01-01')).toBe('in_the_future')
    for (const typed of ['2026-02-30', '2025-02-29', '2026-13-01', '11/20/2020', '2020-11-20T00:00:00', '2020-1-2', 'today', '20201120']) {
      expect(problem('in_service_date', typed)).toBe('not_a_date')
    }
  })

  it('judges "today" by the date it is given, never by a clock', () => {
    expect(fieldProblem('in_service_date', '2026-10-06', '2026-10-06')).toBeNull()
    expect(fieldProblem('in_service_date', '2026-10-06', '2026-10-05')).toBe('in_the_future')
    expect(source).not.toMatch(/new Date\(|Date\.now|toLocale/)
  })
})

describe('saying what is wrong', () => {
  it('has a sentence for every code, the app’s own and the API’s', () => {
    expect(problemText('labor_rate', 'too_many_decimal_places')).toBe('Use at most 2 decimal places. The value is not rounded for you.')
    expect(problemText('manual_items_per_hour', 'too_many_decimal_places')).toBe('Use at most 1 decimal place. The value is not rounded for you.')
    expect(problemText('labor_rate', 'out_of_range')).toBe('Enter an amount above 0, up to 1,000.')
    expect(problemText('manual_items_per_hour', 'out_of_range')).toBe('Enter a rate from 1 to 1,000.')
    expect(problemText('one_time_cost', 'out_of_range')).toBe('Enter an amount from 0 to 100,000,000.')
    expect(problemText('recurring_annual_cost', 'out_of_range')).toBe('Enter an amount from 0 to 10,000,000.')
    expect(problemText('labor_rate', 'not_a_decimal')).toBe('Enter digits only, with a decimal point if needed: no symbols, commas or spaces.')
    expect(problemText('labor_rate', 'not_a_string')).toBe(problemText('labor_rate', 'not_a_decimal'))
    expect(problemText('in_service_date', 'in_the_future')).toBe('The in-service date cannot be after today.')
    expect(problemText('in_service_date', 'not_a_date')).toBe('Enter a date, or leave it blank.')
    expect(problemText('one_time_cost', 'something_new')).toBe('This value was not accepted.')
  })
})

describe('the module', () => {
  it('turns no figure into a floating-point number', () => {
    expect(source).not.toMatch(/Number\(|parseFloat|parseInt|toFixed|Math\.|\bNumberFormat\b/)
    expect(source).toMatch(/BigInt/)
  })
})
