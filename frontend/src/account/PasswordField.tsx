/**
 * One password box: its label, the box, an optional line of help and -- when
 * the value was refused -- what is wrong with it, tied to the box so it is
 * read out with it. What is typed is never shown, and there is no control to
 * reveal it.
 *
 * `autoComplete` says which password this is, so a password manager offers
 * the saved one for "current-password" and offers to save for "new-password".
 */
export function PasswordField({
  id,
  name,
  label,
  autoComplete,
  value,
  onChange,
  help,
  problem,
}: {
  id: string
  name: string
  label: string
  autoComplete: 'current-password' | 'new-password'
  value: string
  onChange: (value: string) => void
  help?: string
  problem?: string
}) {
  const described = [help === undefined ? null : `${id}-help`, problem === undefined ? null : `${id}-problem`].filter(
    (part) => part !== null,
  )

  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        name={name}
        type="password"
        autoComplete={autoComplete}
        required
        value={value}
        aria-invalid={problem !== undefined}
        aria-describedby={described.length === 0 ? undefined : described.join(' ')}
        onChange={(event) => onChange(event.target.value)}
      />
      {help !== undefined && (
        <div className="field-help" id={`${id}-help`}>
          {help}
        </div>
      )}
      {problem !== undefined && (
        <p className="field-problem" id={`${id}-problem`}>
          {problem}
        </p>
      )}
    </div>
  )
}
