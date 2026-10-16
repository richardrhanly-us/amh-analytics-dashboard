import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'

import { isApiError } from '../../../src/api/client.ts'
import { NAME_LIMIT, SLUG_LIMIT, SLUG_PATTERN, getPlans, provisionLibrary, slugify, type ProvisionRequest } from './provisionApi.ts'

type Field = keyof ProvisionRequest
type Problems = Partial<Record<Field, string>>

const LABEL: Record<Field, string> = {
  name: 'Library name',
  slug: 'Library slug',
  branch_name: 'Primary branch name',
  branch_slug: 'Primary branch slug',
  plan_code: 'Plan',
}

const PROBLEM_TEXT: Record<string, (field: Field) => string> = {
  required: (field) => (field === 'plan_code' ? 'Choose a plan.' : 'Required.'),
  too_long: (field) => `At most ${field === 'name' || field === 'branch_name' ? NAME_LIMIT : SLUG_LIMIT} characters.`,
  invalid_slug: () => 'Use lowercase letters and digits, with single hyphens between them.',
}

/** The server's own rules, checked here first so a form is corrected in one pass. The server stays the authority. */
function check(request: ProvisionRequest): Problems {
  const problems: Problems = {}
  for (const field of ['name', 'branch_name'] as const) {
    const value = request[field].trim()
    if (value === '') problems[field] = PROBLEM_TEXT.required(field)
    else if (value.length > NAME_LIMIT) problems[field] = PROBLEM_TEXT.too_long(field)
  }
  for (const field of ['slug', 'branch_slug'] as const) {
    const value = request[field]
    if (value === '') problems[field] = PROBLEM_TEXT.required(field)
    else if (value.length > SLUG_LIMIT) problems[field] = PROBLEM_TEXT.too_long(field)
    else if (!SLUG_PATTERN.test(value)) problems[field] = PROBLEM_TEXT.invalid_slug(field)
  }
  if (request.plan_code === '') problems.plan_code = PROBLEM_TEXT.required('plan_code')
  return problems
}

/** A 422 `invalid_library` answer's problems, as this form's messages. Anything unrecognised is left out. */
function serverProblems(caught: unknown): Problems | null {
  if (!isApiError(caught) || caught.status !== 422 || caught.code !== 'invalid_library') {
    return null
  }
  const listed = (caught.body as { problems?: unknown } | undefined)?.problems
  const problems: Problems = {}
  for (const problem of Array.isArray(listed) ? listed : []) {
    const { field, code } = (problem ?? {}) as { field?: unknown; code?: unknown }
    if (typeof field === 'string' && field in LABEL && typeof code === 'string' && code in PROBLEM_TEXT) {
      problems[field as Field] = PROBLEM_TEXT[code](field as Field)
    }
  }
  return problems
}

function failureText(caught: unknown): string {
  if (isApiError(caught) && caught.status === 409) {
    return `Nothing was created. ${caught.message}`
  }
  // A failure with no clear answer may still have created the library: say so, rather than invite a blind second try.
  return 'The library may not have been created. Check the Libraries list before trying again.'
}

function FormField({ field, problems, children }: { field: Field; problems: Problems; children: ReactNode }) {
  return (
    <div className="field">
      <label htmlFor={field}>{LABEL[field]}</label>
      {children}
      {problems[field] !== undefined && (
        <p className="field-problem" id={`${field}-problem`}>
          {problems[field]}
        </p>
      )}
    </div>
  )
}

/** /provision -- provision one library with one primary branch, on a chosen plan. */
export function ProvisionPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const plans = useQuery({ queryKey: ['admin', 'plans'], queryFn: ({ signal }) => getPlans(signal) })
  const [values, setValues] = useState<ProvisionRequest>({
    name: '',
    slug: '',
    branch_name: 'Main Branch',
    branch_slug: slugify('Main Branch'),
    plan_code: '',
  })
  // A slug follows its name until it is edited by hand; after that it is never overwritten.
  const [edited, setEdited] = useState({ slug: false, branch_slug: false })
  const [problems, setProblems] = useState<Problems>({})
  const [failure, setFailure] = useState<string | null>(null)
  const [sending, setSending] = useState(false)

  function setName(field: 'name' | 'branch_name', value: string) {
    const slugField = field === 'name' ? 'slug' : 'branch_slug'
    setValues((old) => ({ ...old, [field]: value, ...(edited[slugField] ? {} : { [slugField]: slugify(value) }) }))
  }

  function setSlug(field: 'slug' | 'branch_slug', value: string) {
    setValues((old) => ({ ...old, [field]: value }))
    setEdited((old) => ({ ...old, [field]: true }))
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (sending) {
      return
    }
    const found = check(values)
    setProblems(found)
    setFailure(null)
    if (Object.keys(found).length > 0) {
      return
    }
    setSending(true)
    try {
      const created = await provisionLibrary(values)
      void queryClient.invalidateQueries({ queryKey: ['admin', 'libraries'] })
      await navigate(`/libraries/${encodeURIComponent(created.library.slug)}`, { state: { provisioned: true } })
    } catch (caught) {
      const fromServer = serverProblems(caught)
      if (fromServer !== null) {
        setProblems(fromServer)
      } else {
        setFailure(failureText(caught))
      }
      setSending(false)
    }
  }

  const aria = (field: Field) => ({
    id: field,
    'aria-invalid': problems[field] !== undefined,
    'aria-describedby': problems[field] === undefined ? undefined : `${field}-problem`,
  })
  const blocked = sending || plans.data === undefined

  return (
    <section aria-labelledby="provision-heading">
      <p>
        <Link to="/libraries">Libraries</Link>
      </p>
      <h2 id="provision-heading">Provision a library</h2>
      <p className="notice" role="note">
        This creates the library, its one primary branch, a trial subscription on the chosen plan, and its operational
        identity. Collector settings, installations and enrollment codes are set up afterwards, on the library&apos;s own
        pages.
      </p>
      {plans.isError && (
        <p className="error-message" role="alert">
          The plans could not be loaded, so a library cannot be provisioned right now. Please try again.
        </p>
      )}
      <form onSubmit={(event) => void submit(event)} noValidate aria-labelledby="provision-heading">
        <FormField field="name" problems={problems}>
          <input type="text" autoComplete="off" value={values.name} onChange={(event) => setName('name', event.target.value)} {...aria('name')} />
        </FormField>
        <FormField field="slug" problems={problems}>
          <input type="text" autoComplete="off" spellCheck={false} value={values.slug} onChange={(event) => setSlug('slug', event.target.value)} {...aria('slug')} />
        </FormField>
        <FormField field="branch_name" problems={problems}>
          <input
            type="text"
            autoComplete="off"
            value={values.branch_name}
            onChange={(event) => setName('branch_name', event.target.value)}
            {...aria('branch_name')}
          />
        </FormField>
        <FormField field="branch_slug" problems={problems}>
          <input
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={values.branch_slug}
            onChange={(event) => setSlug('branch_slug', event.target.value)}
            {...aria('branch_slug')}
          />
        </FormField>
        <FormField field="plan_code" problems={problems}>
          <select value={values.plan_code} onChange={(event) => setValues((old) => ({ ...old, plan_code: event.target.value }))} {...aria('plan_code')}>
            <option value="" disabled>
              Choose a plan
            </option>
            {(plans.data ?? []).map((plan) => (
              <option key={plan.code} value={plan.code}>
                {plan.name}
              </option>
            ))}
          </select>
        </FormField>

        {failure !== null && (
          <p className="error-message" role="alert">
            {failure}
          </p>
        )}
        <div className="form-actions">
          <button type="submit" disabled={blocked} aria-disabled={blocked}>
            {sending ? 'Provisioning…' : 'Provision library'}
          </button>
        </div>
      </form>
    </section>
  )
}
