import type { BinVolumeBin } from '../api/reports.ts'
import { formatCount } from './derive.ts'

/** What a bin is called: its number, and nothing else. No bin has a name or a meaning of its own. */
export const binLabel = (bin: BinVolumeBin) => `Bin ${bin.key}`

export const checkIns = (value: number) => `${formatCount(value)} ${value === 1 ? 'check-in' : 'check-ins'}`
