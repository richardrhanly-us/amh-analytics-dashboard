import type { FieldProblem } from '../api/account.ts'

/**
 * What to say beside a field the API refused. The API sends the field and a
 * stable word for why; the sentence is written here, in the API's own terms.
 * A word this app does not know gets one plain sentence rather than a guess.
 */
const SENTENCES: Record<string, Record<string, string>> = {
  full_name: {
    required: 'Enter your name.',
    too_long: 'Your name must be 120 characters or fewer.',
    invalid_characters: 'Your name contains characters that cannot be used.',
  },
  current_password: {
    incorrect: 'Your current password is incorrect.',
  },
  new_password: {
    too_short: 'Your new password must be at least 8 characters long.',
    same_as_current: 'Your new password must be different from your current password.',
  },
  confirm_password: {
    mismatch: 'New password and confirmation do not match.',
  },
}

const UNKNOWN = 'This is not valid. Check it and try again.'

/** The sentence for each refused field among `fields`, by field name. Fields the form does not have are left out. */
export function problemSentences<F extends string>(problems: readonly FieldProblem[], fields: readonly F[]): Partial<Record<F, string>> {
  const sentences: Partial<Record<F, string>> = {}
  for (const problem of problems) {
    const field = fields.find((candidate) => candidate === problem.field)
    if (field !== undefined && sentences[field] === undefined) {
      const known = SENTENCES[field]
      sentences[field] = known !== undefined && Object.hasOwn(known, problem.code) ? known[problem.code] : UNKNOWN
    }
  }
  return sentences
}
