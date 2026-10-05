// Data client for the ATS. Talks to the dashboard server's /api/db routes
// (db.mjs), which read and write Supabase with a server-side key. Records keep
// the { id, createdTime, fields } shape and field names the UI used with
// Airtable.

const API = '/api/db/jobs'
const CANDIDATES_API = '/api/db/candidates'
const EMPLOYEES_API = '/api/db/employees'

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
    throw new Error(`Database ${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export async function listJobs(): Promise<JobRecord[]> {
  // Newest posting first; deleted jobs are excluded server-side.
  const data = await handle<{ records: JobRecord[] }>(await fetch(API))
  return data.records
}

export async function createJob(fields: JobFields): Promise<JobRecord> {
  return handle<JobRecord>(
    await fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields }),
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
      body: JSON.stringify({ fields }),
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
  // Newest application first.
  const data = await handle<{ records: CandidateRecord[] }>(
    await fetch(CANDIDATES_API),
  )
  return data.records
}

export async function updateCandidate(
  id: string,
  fields: Partial<CandidateFields>,
): Promise<CandidateRecord> {
  return handle<CandidateRecord>(
    await fetch(`${CANDIDATES_API}/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields }),
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

// Records a hired candidate + offer details in the employees table.
export async function createEmployee(
  fields: EmployeeFields,
): Promise<EmployeeRecord> {
  return handle<EmployeeRecord>(
    await fetch(EMPLOYEES_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fields }),
    }),
  )
}
