// Airtable client for the HR Manager base — all requests go through the Vite
// dev-server proxy (/api/airtable), which injects the API token server-side.

const BASE_ID = 'appar5DLoak36lfyj'
const JOBS_TABLE = 'tblEPFbViaY4EpjF8'
const CANDIDATES_TABLE = 'tbl4I3BpES6LDla89'
const EMPLOYEES_TABLE = 'tblZ38T0qi31dW4jD'
const API = `/api/airtable/v0/${BASE_ID}/${JOBS_TABLE}`
const CANDIDATES_API = `/api/airtable/v0/${BASE_ID}/${CANDIDATES_TABLE}`
const EMPLOYEES_API = `/api/airtable/v0/${BASE_ID}/${EMPLOYEES_TABLE}`

export interface JobFields {
  'Job Title'?: string
  Status?: string
  'Date Posted'?: string
  'Employment Type'?: string
  'Work Arrangement'?: string
  Department?: string
  'Company / Brand'?: string
  Location?: string
  Schedule?: string
  'Pay Min'?: number
  'Pay Max'?: number
  'Pay Period'?: string
  'Pay Currency'?: string
  'Education Requirement'?: string
  'Experience Required'?: string
  Benefits?: string[]
  'Company Overview'?: string
  'Role Summary'?: string
  'Core Responsibilities'?: string
  'Requirements & Qualifications'?: string
}

export interface JobRecord {
  id: string
  createdTime: string
  fields: JobFields
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body?.error?.message ?? detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`Airtable ${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export async function listJobs(): Promise<JobRecord[]> {
  const records: JobRecord[] = []
  let offset: string | undefined
  do {
    const url = new URL(API, window.location.origin)
    url.searchParams.set('pageSize', '100')
    url.searchParams.set('sort[0][field]', 'Date Posted')
    url.searchParams.set('sort[0][direction]', 'desc')
    if (offset) url.searchParams.set('offset', offset)
    const data = await handle<{ records: JobRecord[]; offset?: string }>(
      await fetch(url),
    )
    records.push(...data.records)
    offset = data.offset
  } while (offset)
  return records
}

export async function createJob(fields: JobFields): Promise<JobRecord> {
  return handle<JobRecord>(
    await fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // typecast lets Airtable create new single-select options on the fly
      body: JSON.stringify({ fields, typecast: true }),
    }),
  )
}

export async function updateJob(
  id: string,
  fields: Partial<JobFields>,
): Promise<JobRecord> {
  return handle<JobRecord>(
    await fetch(`${API}/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields, typecast: true }),
    }),
  )
}

export async function deleteJob(id: string): Promise<void> {
  await handle<{ deleted: boolean; id: string }>(
    await fetch(`${API}/${id}`, { method: 'DELETE' }),
  )
}

export interface Attachment {
  url: string
  filename: string
}

export interface CandidateFields {
  'Candidate Name'?: string
  Status?: string
  'Job Title Applied'?: string
  'Job Location'?: string
  'Application Date'?: string
  Source?: string
  Email?: string
  Phone?: string
  'Candidate Location'?: string
  'Years of Experience'?: number
  'Key Skills'?: string
  'Relevant Experience'?: string
  'Education & Qualifications'?: string
  'AI Summary'?: string
  Résumé?: Attachment[]
  'Résumé Drive Link'?: string
  'Indeed Profile Link'?: string
  'Match Score'?: number
  'Match Notes'?: string
}

export interface CandidateRecord {
  id: string
  createdTime: string
  fields: CandidateFields
}

export async function listCandidates(): Promise<CandidateRecord[]> {
  const records: CandidateRecord[] = []
  let offset: string | undefined
  do {
    const url = new URL(CANDIDATES_API, window.location.origin)
    url.searchParams.set('pageSize', '100')
    url.searchParams.set('sort[0][field]', 'Application Date')
    url.searchParams.set('sort[0][direction]', 'desc')
    if (offset) url.searchParams.set('offset', offset)
    const data = await handle<{ records: CandidateRecord[]; offset?: string }>(
      await fetch(url),
    )
    records.push(...data.records)
    offset = data.offset
  } while (offset)
  return records
}

export async function updateCandidate(
  id: string,
  fields: Partial<CandidateFields>,
): Promise<CandidateRecord> {
  return handle<CandidateRecord>(
    await fetch(`${CANDIDATES_API}/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields, typecast: true }),
    }),
  )
}

export interface EmployeeFields {
  'Candidate Name'?: string
  Email?: string
  'Job Title'?: string
  'Compensation Type'?: string
  'Compensation Amount'?: number
  Location?: string
  'Employment Type'?: string
  'Start Date'?: string
  'Probation Period (Months)'?: number
  'Offer Emailed At'?: string
}

export interface EmployeeRecord {
  id: string
  createdTime: string
  fields: EmployeeFields
}

// Records a hired candidate + offer details in the Employee table. typecast
// lets Airtable accept the single-select values without exact option ids.
export async function createEmployee(
  fields: EmployeeFields,
): Promise<EmployeeRecord> {
  return handle<EmployeeRecord>(
    await fetch(EMPLOYEES_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields, typecast: true }),
    }),
  )
}
