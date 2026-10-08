import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { isApiError } from '../api/client.ts'
import {
  addMember,
  changeMemberRole,
  getMemberActivity,
  getMembers,
  removeMember,
  type Member,
  type MemberActivity,
  type NewMember,
} from '../api/members.ts'
import { organizationKey } from '../api/organizations.ts'
import { useAuth } from '../auth/useAuth.ts'
import { messageFor } from '../components/errorText.ts'

/**
 * What a read of the organization's members has to show:
 *
 *   loading     the first answer has not arrived
 *   ready       the API's answer
 *   forbidden   403: this person may not manage members (any longer). Nothing of the list is shown.
 *   not_found   404: the organization is not this person's to see (any longer)
 *   error       anything else, with no earlier answer to keep showing; `message` is safe to show
 *
 * A 403 or 404 always wins over an earlier answer: once the API has said no,
 * what it said before is not shown.
 */
export type Loaded<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'forbidden' }
  | { status: 'not_found' }
  | { status: 'error'; message: string }

const membersKey = (orgSlug: string) => ['members', orgSlug] as const
const activityKey = (orgSlug: string) => ['member-activity', orgSlug] as const

function loaded<T>(query: UseQueryResult<T>): Loaded<T> {
  if (isApiError(query.error) && query.error.status === 403) {
    return { status: 'forbidden' }
  }
  if (isApiError(query.error) && query.error.status === 404) {
    return { status: 'not_found' }
  }
  if (query.data !== undefined) {
    return { status: 'ready', data: query.data }
  }
  return query.isError ? { status: 'error', message: messageFor(query.error) } : { status: 'loading' }
}

/** The organization's members. Asked only when `wanted`: nobody is asked on behalf of someone it would refuse. */
export function useMembers(orgSlug: string, wanted: boolean): { members: Loaded<Member[]>; retry: () => void } {
  const query = useQuery({
    queryKey: membersKey(orgSlug),
    queryFn: ({ signal }) => getMembers(orgSlug, signal),
    enabled: wanted,
    // Nothing of a list is kept once the page showing it is gone: the next visit asks again.
    gcTime: 0,
  })
  return { members: loaded(query), retry: () => void query.refetch({ cancelRefetch: false }) }
}

/** The recent changes to the organization's members. */
export function useMemberActivity(orgSlug: string, wanted: boolean): { activity: Loaded<MemberActivity[]>; retry: () => void } {
  const query = useQuery({
    queryKey: activityKey(orgSlug),
    queryFn: ({ signal }) => getMemberActivity(orgSlug, signal),
    enabled: wanted,
    gcTime: 0,
  })
  return { activity: loaded(query), retry: () => void query.refetch({ cancelRefetch: false }) }
}

/**
 * The three changes. Each resolves once the API has made the change AND the
 * list and the recent changes have been asked for again, so what the page
 * then shows is what the API then says -- nothing is updated by hand.
 *
 * A change to the signed-in person's OWN role also changes what the
 * organization says their role is, which is what this page offers its
 * controls by. So that change waits for the organization to be asked for
 * again as well: the role the page then goes by is the one the API confirms,
 * never one worked out here. Nobody else's change asks for it.
 *
 * Each rejects with the failure, for the form or row that asked to show. A
 * 401 means the session is gone, exactly as for a read. `refresh` asks for
 * the list again when a failure says it is out of date.
 */
export function useMemberChanges(orgSlug: string) {
  const queryClient = useQueryClient()
  const { sessionExpired } = useAuth()

  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: membersKey(orgSlug) }),
      queryClient.invalidateQueries({ queryKey: activityKey(orgSlug) }),
    ])
  const sessionEnd = (error: unknown) => {
    if (isApiError(error) && error.status === 401) {
      sessionExpired()
    }
  }

  const add = useMutation({ mutationFn: (member: NewMember) => addMember(orgSlug, member), onSuccess: refresh, onError: sessionEnd })
  const changeRole = useMutation({
    mutationFn: ({ email, role }: { email: string; role: string; own: boolean }) => changeMemberRole(orgSlug, email, role),
    onSuccess: (_done, { own }) =>
      own ? Promise.all([refresh(), queryClient.invalidateQueries({ queryKey: organizationKey(orgSlug) })]) : refresh(),
    onError: sessionEnd,
  })
  const remove = useMutation({ mutationFn: (email: string) => removeMember(orgSlug, email), onSuccess: refresh, onError: sessionEnd })

  return {
    add: (member: NewMember) => add.mutateAsync(member),
    /** `own` is true when the member is the signed-in person: their role in the organization is then read again. */
    changeRole: (email: string, role: string, own: boolean) => changeRole.mutateAsync({ email, role, own }),
    remove: (email: string) => remove.mutateAsync(email),
    refresh: () => void refresh(),
  }
}

export type MemberChanges = ReturnType<typeof useMemberChanges>
