import type { OperationalIdentity, OnboardingStatus, Pipeline, PipelineState, PrimarySorter } from './librariesApi.ts'

/** The words the admin app uses for the API's values. A value it does not know is shown as "Unknown", never raw. */

const LIBRARY_STATUS: Record<string, string> = {
  active: 'Active',
  trial: 'Trial',
  suspended: 'Suspended',
  cancelled: 'Cancelled',
}

const SUBSCRIPTION_STATUS: Record<string, string> = {
  active: 'Active',
  trial: 'Trial',
  past_due: 'Past due',
  cancelled: 'Cancelled',
}

const BRANCH_STATUS: Record<string, string> = {
  active: 'Active',
  inactive: 'Inactive',
}

const INSTALLATION_STATUS: Record<string, string> = {
  active: 'Active',
  provisioning: 'Provisioning',
  inactive: 'Inactive',
}

const PIPELINE_STATE: Record<PipelineState, string> = {
  ok: 'OK',
  degraded: 'Degraded',
  failed: 'Failed',
  unknown: 'Nothing reported',
}

const ONBOARDING: Record<OnboardingStatus, string> = {
  ready: 'Ready',
  pending: 'Waiting for first data',
}

const IDENTITY: Record<OperationalIdentity, string> = {
  assigned: 'Assigned',
  partial: 'Partially assigned',
  missing: 'Not assigned',
}

const known = (labels: Record<string, string>, value: string): string => labels[value] ?? 'Unknown'

export const libraryStatusLabel = (status: string): string => known(LIBRARY_STATUS, status)
export const subscriptionStatusLabel = (status: string): string => known(SUBSCRIPTION_STATUS, status)
export const branchStatusLabel = (status: string): string => known(BRANCH_STATUS, status)
export const installationStatusLabel = (status: string): string => known(INSTALLATION_STATUS, status)
export const onboardingLabel = (status: OnboardingStatus): string => ONBOARDING[status]
export const identityLabel = (identity: OperationalIdentity): string => IDENTITY[identity]
export const pipelineStateLabel = (pipeline: Pipeline): string => PIPELINE_STATE[pipeline.state]

/** The list's one word for a library's primary sorter: whether there is one, and what it last reported. */
export function primarySorterLabel(sorter: PrimarySorter | null): string {
  if (sorter === null) {
    return 'No sorter'
  }
  if (sorter.pipeline.state === 'unknown' && sorter.onboarding_status === 'pending') {
    return ONBOARDING.pending
  }
  return PIPELINE_STATE[sorter.pipeline.state]
}
