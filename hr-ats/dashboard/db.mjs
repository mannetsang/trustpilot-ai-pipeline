// Server-side data API for the jobs dashboard, backed by Supabase (project
// shp-ats). Replaces the old /api/airtable proxy.
//
// The browser never sees a Supabase key: it calls /api/db/* here, and this
// module talks to PostgREST with the secret key. Records go back to the
// browser in the same { id, createdTime, fields } shape, with the same field
// names, that the UI used with Airtable, so App.tsx only changed its import.
//
// Routes:
//   GET    /api/db/jobs                 live jobs, newest posting first
//   POST   /api/db/jobs        {fields} create
//   PATCH  /api/db/jobs/:id    {fields} update
//   DELETE /api/db/jobs/:id             soft delete (sets deleted_at)
//   GET    /api/db/candidates           all candidates, newest application first
//   PATCH  /api/db/candidates/:id {fields}
//   POST   /api/db/employees   {fields} create
//   GET    /api/resume/:candidateId     302 to a 5-minute signed résumé URL

const SUPABASE_URL = (
  process.env.SUPABASE_URL ?? 'https://qxmwygkwctyksfcmsqsf.supabase.co'
).replace(/\/$/, '')
// Read per request so the Vite dev server can set it from ../.env first.
const secretKey = () => process.env.SUPABASE_SECRET_KEY

// UI field name -> column, per table. Only these fields can be read or
// written through the API.
const MAPS = {
  jobs: {
    'Job Title': 'title',
    Status: 'status',
    'Date Posted': 'date_posted',
    'Employment Type': 'employment_type',
    'Work Arrangement': 'work_arrangement',
    Department: 'department',
    'Company / Brand': 'company_brand',
    Location: 'location',
    Schedule: 'schedule',
    'Pay Min': 'pay_min',
    'Pay Max': 'pay_max',
    'Pay Period': 'pay_period',
    'Pay Currency': 'pay_currency',
    'Education Requirement': 'education_requirement',
    'Experience Required': 'experience_required',
    Benefits: 'benefits',
    'Company Overview': 'company_overview',
    'Role Summary': 'role_summary',
    'Core Responsibilities': 'core_responsibilities',
    'Requirements & Qualifications': 'requirements_qualifications',
  },
  candidates: {
    'Candidate Name': 'candidate_name',
    Status: 'status',
    'Job Title Applied': 'job_title_applied',
    'Job Location': 'job_location',
    'Application Date': 'application_date',
    Source: 'source',
    Email: 'email',
    Phone: 'phone',
    'Candidate Location': 'candidate_location',
    'Years of Experience': 'years_of_experience',
    'Key Skills': 'key_skills',
    'Relevant Experience': 'relevant_experience',
    'Education & Qualifications': 'education_qualifications',
    'AI Summary': 'ai_summary',
    'Résumé Drive Link': 'resume_drive_link',
    'Indeed Profile Link': 'indeed_profile_link',
    'Match Score': 'match_score',
    'Match Notes': 'match_notes',
  },
  employees: {
    'Candidate Name': 'candidate_name',
    Email: 'email',
    'Job Title': 'job_title',
    'Compensation Type': 'compensation_type',
    'Compensation Amount': 'compensation_amount',
    Location: 'location',
    'Employment Type': 'employment_type',
    'Start Date': 'start_date',
    'Probation Period (Months)': 'probation_period_months',
    'Offer Emailed At': 'offer_emailed_at',
  },
}

const ORDER = {
  jobs: 'date_posted.desc.nullslast,created_at.desc',
  candidates: 'application_date.desc.nullslast,created_at.desc',
}

// Columns Postgres types as numeric come back from PostgREST as strings when
// they don't fit a JS number exactly; the UI expects numbers.
const NUMERIC = new Set([
  'pay_min',
  'pay_max',
  'years_of_experience',
  'compensation_amount',
])

function toRecord(table, row) {
  const fields = {}
  for (const [name, col] of Object.entries(MAPS[table])) {
    let v = row[col]
    if (v === null || v === undefined) continue
    if (NUMERIC.has(col) && typeof v === 'string') v = Number(v)
    fields[name] = v
  }
  if (table === 'candidates' && row.resume_path) {
    fields['Résumé'] = [
      {
        url: `/api/resume/${row.id}`,
        filename: row.resume_filename ?? 'résumé.pdf',
      },
    ]
  }
  return { id: row.id, createdTime: row.created_at, fields }
}

function toRow(table, fields) {
  const row = {}
  for (const [name, v] of Object.entries(fields ?? {})) {
    const col = MAPS[table][name]
    if (!col) throw httpError(400, `Unknown field "${name}"`)
    // Airtable treated '' as empty; keep that so cleared inputs store NULL.
    row[col] = v === '' ? null : v
  }
  return row
}

function httpError(status, message) {
  return Object.assign(new Error(message), { status })
}

async function rest(path, init = {}) {
  const key = secretKey()
  if (!key) throw httpError(500, 'SUPABASE_SECRET_KEY is not configured')
  const res = await fetch(`${SUPABASE_URL}${path}`, {
    ...init,
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      'Content-Type': 'application/json',
      ...init.headers,
    },
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.message ?? body.error ?? detail
    } catch {
      /* non-JSON error body */
    }
    throw httpError(res.status >= 500 ? 502 : res.status, `Supabase: ${detail}`)
  }
  return res
}

async function listAll(table, filter = '') {
  const rows = []
  const page = 1000
  for (let start = 0; ; start += page) {
    const res = await rest(
      `/rest/v1/${table}?select=*&order=${ORDER[table]}${filter}`,
      { headers: { 'Range-Unit': 'items', Range: `${start}-${start + page - 1}` } },
    )
    const batch = await res.json()
    rows.push(...batch)
    if (batch.length < page) return rows
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

async function insertRow(table, fields) {
  const res = await rest(`/rest/v1/${table}`, {
    method: 'POST',
    headers: { Prefer: 'return=representation' },
    body: JSON.stringify(toRow(table, fields)),
  })
  return toRecord(table, (await res.json())[0])
}

async function updateRow(table, id, patch, extraFilter = '') {
  const res = await rest(`/rest/v1/${table}?id=eq.${id}${extraFilter}`, {
    method: 'PATCH',
    headers: { Prefer: 'return=representation' },
    body: JSON.stringify(patch),
  })
  const rows = await res.json()
  if (!rows.length) throw httpError(404, 'Record not found')
  return toRecord(table, rows[0])
}

async function resumeRedirect(id) {
  const res = await rest(
    `/rest/v1/candidates?select=resume_path&id=eq.${id}`,
  )
  const path = (await res.json())[0]?.resume_path
  if (!path) throw httpError(404, 'No résumé stored for this candidate')
  const signed = await rest(
    `/storage/v1/object/sign/resumes/${path.split('/').map(encodeURIComponent).join('/')}`,
    { method: 'POST', body: JSON.stringify({ expiresIn: 300 }) },
  )
  const { signedURL } = await signed.json()
  // Older objects can have spaces etc. in their names; encode each segment so
  // the Location header is a valid URL.
  const [objPath, query] = signedURL.split('?')
  const encoded = objPath
    .split('/')
    .map((seg) => encodeURIComponent(decodeURIComponent(seg)))
    .join('/')
  return `${SUPABASE_URL}/storage/v1${encoded}?${query}`
}

function sendJson(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(obj))
}

// Handles /api/db/* and /api/resume/*. Returns false for any other path.
// `readBody` resolves to the raw request body as a Buffer.
export async function handleDb(req, res, pathname, readBody) {
  const resume = pathname.match(/^\/api\/resume\/([^/]+)$/)
  const m = pathname.match(/^\/api\/db\/(jobs|candidates|employees)(?:\/([^/]+))?$/)
  if (!resume && !m) return false

  try {
    if (resume) {
      if (!UUID.test(resume[1])) throw httpError(404, 'Not found')
      res.writeHead(302, { Location: await resumeRedirect(resume[1]) })
      res.end()
      return true
    }

    const [, table, id] = m
    if (id && !UUID.test(id)) throw httpError(404, 'Not found')
    const body = async () => {
      const raw = (await readBody(req)).toString() || '{}'
      try {
        return JSON.parse(raw)
      } catch {
        throw httpError(400, 'Invalid JSON body')
      }
    }

    const route = `${req.method} ${table}${id ? '/:id' : ''}`
    let result
    switch (route) {
      case 'GET jobs':
        result = { records: (await listAll('jobs', '&deleted_at=is.null')).map((r) => toRecord('jobs', r)) }
        break
      case 'GET candidates':
        result = { records: (await listAll('candidates')).map((r) => toRecord('candidates', r)) }
        break
      case 'POST jobs':
      case 'POST employees':
        result = await insertRow(table, (await body()).fields)
        break
      case 'PATCH jobs/:id':
        result = await updateRow('jobs', id, toRow('jobs', (await body()).fields), '&deleted_at=is.null')
        break
      case 'PATCH candidates/:id':
        result = await updateRow('candidates', id, toRow('candidates', (await body()).fields))
        break
      case 'DELETE jobs/:id':
        await updateRow('jobs', id, { deleted_at: new Date().toISOString() }, '&deleted_at=is.null')
        result = { deleted: true, id }
        break
      default:
        throw httpError(405, 'Method not allowed')
    }
    sendJson(res, 200, result)
    return true
  } catch (err) {
    const status = err.status ?? 500
    if (status >= 500) console.error(err)
    sendJson(res, status, { error: { message: err.message } })
    return true
  }
}
