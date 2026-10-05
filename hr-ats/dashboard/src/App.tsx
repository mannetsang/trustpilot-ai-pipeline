import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Briefcase,
  CalendarPlus,
  Check,
  ExternalLink,
  FileText,
  Loader2,
  Lock,
  LogOut,
  Mail,
  MapPin,
  Plus,
  RefreshCw,
  Share2,
  Trash2,
  Users,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'

import {
  createEmployee,
  createJob,
  deleteJob,
  listCandidates,
  listJobs,
  updateCandidate,
  updateJob,
} from '@/lib/db'
import type {
  CandidateFields,
  CandidateRecord,
  JobFields,
  JobRecord,
} from '@/lib/db'

const STATUSES = ['Draft', 'Open', 'On Hold', 'Filled', 'Closed']
// The pipeline creates a job with this status when an application arrives for
// a role that isn't in the ATS yet. Not offered in the Add-job form, but it
// must be filterable and settable, or those jobs (and their applicants) vanish.
const NEEDS_REVIEW = 'Needs Review'
const JOB_STATUSES = [NEEDS_REVIEW, ...STATUSES]
// Default view: open roles plus the new ones HR still has to review.
const ACTIVE_FILTER = 'active'
const DEPARTMENTS = [
  'Customer Service',
  'Sales',
  'Warehouse / Logistics',
  'Administration',
  'Marketing',
  'Manufacturing',
]
const EMPLOYMENT_TYPES = [
  'Full-time',
  'Part-time',
  'Contract',
  'Temporary',
  'Internship',
  'Seasonal',
]
const WORK_ARRANGEMENTS = ['In person', 'Hybrid', 'Remote']
const PAY_PERIODS = ['Per hour', 'Per week', 'Per month', 'Per year']
const PAY_CURRENCIES = ['CAD', 'USD', 'EUR']

// Offer-details popup (Send hire email). Recorded in the Employee table.
const COMP_TYPES = ['Hourly', 'Salary']
const OFFER_EMPLOYMENT_TYPES = ['Full-time', 'Part-time']
const OFFER_LOCATIONS = [
  'Ridgeway',
  'Eglinton',
  'Rapistan',
  'STC',
  'Dufferin',
  'Brampton',
  'Sunrise',
  'Pembroke Pines',
  'New York',
  'Bogota',
  'Qingdao',
  'Netherland',
  'France',
  'Madrid',
  'Germany',
]

const PROBATION_MONTHS = Array.from({ length: 12 }, (_, i) => String(i + 1))

interface OfferFormState {
  compType: string
  amount: string
  location: string
  employmentType: string
  startDate: string
  probationMonths: string
}

const EMPTY_OFFER: OfferFormState = {
  compType: 'Hourly',
  amount: '',
  location: '',
  employmentType: 'Full-time',
  startDate: '',
  probationMonths: '3',
}

const STATUS_STYLES: Record<string, string> = {
  Open: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  Draft: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300',
  'On Hold': 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  [NEEDS_REVIEW]: 'bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300',
  Filled: 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300',
  Closed: 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300',
}

const CANDIDATE_STATUS_STYLES: Record<string, string> = {
  New: 'bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300',
  Reviewing: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  Screening: 'bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300',
  Interview: 'bg-purple-100 text-purple-800 dark:bg-purple-950 dark:text-purple-300',
  Offer: 'bg-teal-100 text-teal-800 dark:bg-teal-950 dark:text-teal-300',
  Hired: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  Rejected: 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300',
}

function scoreColor(score: number): string {
  if (score >= 80) return 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300'
  if (score >= 60) return 'bg-lime-100 text-lime-800 dark:bg-lime-950 dark:text-lime-300'
  if (score >= 40) return 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300'
  return 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300'
}

function formatPay(f: JobFields): string {
  if (f['Pay Min'] == null && f['Pay Max'] == null) return '—'
  const cur = f['Pay Currency'] === 'CAD' ? 'CA$' : f['Pay Currency'] === 'EUR' ? '€' : '$'
  const fmt = (n: number) => n.toFixed(2)
  const min = f['Pay Min']
  const max = f['Pay Max']
  const range =
    min != null && max != null && min !== max
      ? `${cur}${fmt(min)}–${fmt(max)}`
      : `${cur}${fmt((min ?? max) as number)}`
  return f['Pay Period'] ? `${range} ${f['Pay Period'].toLowerCase()}` : range
}

function formatDate(iso?: string): string {
  if (!iso) return '—'
  return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

// Composes the hire / offer-letter notification email (subject + plain-text
// body) for a candidate. Sent server-side via POST /api/send-email, which
// relays it through the connected Gmail account.
function buildHireEmail(
  cf: CandidateFields,
  fallbackRole?: string,
): { subject: string; body: string } {
  const firstName = (cf['Candidate Name'] ?? '').trim().split(/\s+/)[0] || 'there'
  const role = (cf['Job Title Applied'] ?? fallbackRole ?? '').trim()
  const rolePhrase = role ? `the ${role} position` : 'the role you applied for'

  const subject = role
    ? `Congratulations! — ${role} offer (quick detail needed)`
    : 'Congratulations! — job offer (quick detail needed)'

  const body = [
    `Dear ${firstName},`,
    '',
    `Congratulations! We are delighted to let you know that you have successfully passed your interview for ${rolePhrase} at Superhairpieces. We have decided to move forward with hiring you, and we are excited to welcome you to the team.`,
    '',
    'We are now preparing your official offer letter, and to finalize it we just need your full residential mailing address. When you have a moment, could you please reply to this email with your complete address (street address, city, province, and postal code)?',
    '',
    'As soon as we receive it, we will send over your finalized offer letter with all the details.',
    '',
    'Congratulations again, and please do not hesitate to reach out if you have any questions.',
  ].join('\n')

  return { subject, body }
}

// Your interview scheduling / booking link, included in every interview invite.
const INTERVIEW_BOOKING_LINK = 'https://calendar.app.google/TZRevTq7qbmFTmFf9'

// Composes the interview-invitation email (drafted, never auto-sent).
function buildInterviewEmail(
  cf: CandidateFields,
  fallbackRole?: string,
): { subject: string; body: string } {
  const firstName = (cf['Candidate Name'] ?? '').trim().split(/\s+/)[0] || 'there'
  const role = (cf['Job Title Applied'] ?? fallbackRole ?? '').trim()
  const rolePhrase = role ? `the ${role} position` : 'the position you applied for'

  const subject = role
    ? `Interview Invitation — ${role} at Superhairpieces`
    : 'Interview Invitation — Superhairpieces'

  const body = [
    `Dear ${firstName},`,
    '',
    `Thank you for applying for ${rolePhrase} at Superhairpieces. We were impressed with your background and would like to invite you to an interview.`,
    '',
    'Please use the link below to pick an interview time that works for you:',
    INTERVIEW_BOOKING_LINK,
    '',
    'We look forward to speaking with you. If you have any questions, feel free to reply to this email.',
  ].join('\n')

  return { subject, body }
}

interface AddJobFormState {
  title: string
  status: string
  department: string
  employmentType: string
  workArrangement: string
  location: string
  schedule: string
  payMin: string
  payMax: string
  payPeriod: string
  payCurrency: string
  datePosted: string
  roleSummary: string
}

const EMPTY_FORM: AddJobFormState = {
  title: '',
  status: 'Open',
  department: '',
  employmentType: 'Full-time',
  workArrangement: 'In person',
  location: '',
  schedule: '',
  payMin: '',
  payMax: '',
  payPeriod: 'Per hour',
  payCurrency: 'CAD',
  datePosted: new Date().toISOString().slice(0, 10),
  roleSummary: '',
}

function LoginScreen({ onSuccess }: { onSuccess: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      })
      if (res.ok) onSuccess()
      else setError('Incorrect password.')
    } catch {
      setError('Login failed — please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Lock className="size-5" />
            Superhairpieces ATS
          </CardTitle>
          <CardDescription>Enter the password to continue.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="grid gap-3">
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
              autoFocus
              aria-label="Password"
            />
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Button type="submit" disabled={submitting || !password}>
              {submitting && <Loader2 className="size-4 animate-spin" />}
              Log in
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

export default function App() {
  const [jobs, setJobs] = useState<JobRecord[]>([])
  const [candidates, setCandidates] = useState<CandidateRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [savingStatusId, setSavingStatusId] = useState<string | null>(null)
  const [form, setForm] = useState<AddJobFormState>(EMPTY_FORM)
  const [selectedJob, setSelectedJob] = useState<JobRecord | null>(null)
  const [statusFilter, setStatusFilter] = useState(ACTIVE_FILTER)
  const [locationFilter, setLocationFilter] = useState('all')
  const [sentIds, setSentIds] = useState<Set<string>>(new Set())
  const [offerCandidate, setOfferCandidate] = useState<CandidateRecord | null>(null)
  const [offerForm, setOfferForm] = useState<OfferFormState>(EMPTY_OFFER)
  const [submittingOffer, setSubmittingOffer] = useState(false)
  const [movingId, setMovingId] = useState<string | null>(null)
  const [invitingId, setInvitingId] = useState<string | null>(null)
  const [invitedIds, setInvitedIds] = useState<Set<string>>(new Set())
  const [shareCandidate, setShareCandidate] = useState<CandidateRecord | null>(null)
  const [chatSpaces, setChatSpaces] = useState<{ name: string; label: string }[]>([])
  const [spacesStatus, setSpacesStatus] = useState<'loading' | 'error' | 'ready'>('loading')
  const [selectedSpace, setSelectedSpace] = useState('')
  const [sharing, setSharing] = useState(false)
  const [shareDone, setShareDone] = useState(false)
  const [authState, setAuthState] = useState<'checking' | 'needed' | 'ok'>(
    'checking',
  )

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [jobsData, candidatesData] = await Promise.all([
        listJobs(),
        listCandidates(),
      ])
      setJobs(jobsData)
      setCandidates(candidatesData)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      if (msg.includes('401')) setAuthState('needed') // session expired
      else setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  // Check whether a password login is required before loading any data.
  useEffect(() => {
    fetch('/api/session')
      .then((r) => r.json())
      .then((d) => setAuthState(d.required && !d.authed ? 'needed' : 'ok'))
      .catch(() => setAuthState('ok'))
  }, [])

  useEffect(() => {
    if (authState === 'ok') void refresh()
  }, [refresh, authState])

  async function handleLogout() {
    await fetch('/api/logout', { method: 'POST' }).catch(() => {})
    setAuthState('needed')
  }

  const openCount = useMemo(
    () => jobs.filter((j) => j.fields.Status === 'Open').length,
    [jobs],
  )
  const departmentCount = useMemo(
    () => new Set(jobs.map((j) => j.fields.Department).filter(Boolean)).size,
    [jobs],
  )
  // Candidates grouped by the exact job title they applied for.
  const candidatesByJob = useMemo(() => {
    const map = new Map<string, CandidateRecord[]>()
    for (const c of candidates) {
      const title = (c.fields['Job Title Applied'] ?? '').trim()
      if (!title) continue
      const list = map.get(title) ?? []
      list.push(c)
      map.set(title, list)
    }
    return map
  }, [candidates])

  const locations = useMemo(
    () =>
      Array.from(
        new Set(jobs.map((j) => j.fields.Location).filter(Boolean) as string[]),
      ).sort(),
    [jobs],
  )
  const jobTitles = useMemo(
    () =>
      Array.from(
        new Set(
          jobs.map((j) => (j.fields['Job Title'] ?? '').trim()).filter(Boolean),
        ),
      ).sort(),
    [jobs],
  )
  const filteredJobs = useMemo(
    () =>
      jobs.filter(
        (j) =>
          (statusFilter === 'all' ||
            (statusFilter === ACTIVE_FILTER
              ? j.fields.Status === 'Open' || j.fields.Status === NEEDS_REVIEW
              : j.fields.Status === statusFilter)) &&
          (locationFilter === 'all' || j.fields.Location === locationFilter),
      ),
    [jobs, statusFilter, locationFilter],
  )

  async function handleStatusChange(job: JobRecord, newStatus: string) {
    const previous = job.fields.Status
    setSavingStatusId(job.id)
    setError(null)
    // optimistic update
    setJobs((prev) =>
      prev.map((j) =>
        j.id === job.id
          ? { ...j, fields: { ...j.fields, Status: newStatus } }
          : j,
      ),
    )
    try {
      await updateJob(job.id, { Status: newStatus })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setJobs((prev) =>
        prev.map((j) =>
          j.id === job.id
            ? { ...j, fields: { ...j.fields, Status: previous } }
            : j,
        ),
      )
    } finally {
      setSavingStatusId(null)
    }
  }

  const selectedCandidates = (
    selectedJob
      ? candidatesByJob.get((selectedJob.fields['Job Title'] ?? '').trim()) ?? []
      : []
  )
    .slice()
    .sort(
      (a, b) =>
        (b.fields['Match Score'] ?? -1) - (a.fields['Match Score'] ?? -1),
    )

  const set = (key: keyof AddJobFormState) => (value: string) =>
    setForm((f) => ({ ...f, [key]: value }))

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!form.title.trim()) return
    setSaving(true)
    setError(null)
    try {
      const fields: JobFields = {
        'Job Title': form.title.trim(),
        Status: form.status,
        'Employment Type': form.employmentType,
        'Work Arrangement': form.workArrangement,
        'Pay Period': form.payPeriod,
        'Pay Currency': form.payCurrency,
        'Company / Brand': 'New Frontier Global / Superhairpieces',
      }
      if (form.department) fields.Department = form.department
      if (form.location.trim()) fields.Location = form.location.trim()
      if (form.schedule.trim()) fields.Schedule = form.schedule.trim()
      if (form.payMin) fields['Pay Min'] = Number(form.payMin)
      if (form.payMax) fields['Pay Max'] = Number(form.payMax)
      if (form.datePosted) fields['Date Posted'] = form.datePosted
      if (form.roleSummary.trim()) fields['Role Summary'] = form.roleSummary.trim()

      const created = await createJob(fields)
      setJobs((prev) => [created, ...prev])
      setForm(EMPTY_FORM)
      setDialogOpen(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(id: string) {
    setDeletingId(id)
    setError(null)
    try {
      await deleteJob(id)
      setJobs((prev) => prev.filter((j) => j.id !== id))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeletingId(null)
    }
  }

  // Opens the offer-details popup, pre-filling from the job where possible.
  function openOfferDialog(candidate: CandidateRecord) {
    const jf = selectedJob?.fields
    const amount = jf?.['Pay Max'] ?? jf?.['Pay Min']
    setOfferForm({
      compType: !jf?.['Pay Period'] || jf['Pay Period'] === 'Per hour' ? 'Hourly' : 'Salary',
      amount: amount != null ? String(amount) : '',
      location: '',
      employmentType: jf?.['Employment Type'] === 'Part-time' ? 'Part-time' : 'Full-time',
      startDate: '',
      probationMonths: '3',
    })
    setOfferCandidate(candidate)
  }

  const setOffer = (key: keyof OfferFormState) => (value: string) =>
    setOfferForm((f) => ({ ...f, [key]: value }))

  // Open the "Share to Google Chat" dialog and load the user's Chat spaces.
  async function openShareDialog(candidate: CandidateRecord) {
    setShareCandidate(candidate)
    setSelectedSpace('')
    setShareDone(false)
    setChatSpaces([])
    setSpacesStatus('loading')
    try {
      const res = await fetch('/api/chat/spaces')
      const data = (await res.json().catch(() => ({}))) as {
        ok?: boolean
        spaces?: { name: string; label: string }[]
        error?: string
      }
      if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`)
      setChatSpaces(data.spaces ?? [])
      setSpacesStatus('ready')
    } catch (e) {
      setSpacesStatus('error')
      setError(
        `Could not load Google Chat conversations: ${
          e instanceof Error ? e.message : String(e)
        }`,
      )
    }
  }

  async function handleShare() {
    const candidate = shareCandidate
    if (!candidate || !selectedSpace) return
    const cf = candidate.fields
    const resumeUrl = cf['Résumé Drive Link'] ?? cf['Résumé']?.[0]?.url ?? ''
    const score = cf['Match Score']
    const text = [
      `*${cf['Candidate Name'] ?? 'Candidate'}*${
        score != null ? ` — AI match score: ${score}/100` : ''
      }`,
      cf['Job Title Applied'] ? `Applied for: ${cf['Job Title Applied']}` : '',
      cf['Match Notes'] ? `Match: ${cf['Match Notes']}` : '',
      resumeUrl ? `Résumé: ${resumeUrl}` : '',
    ]
      .filter(Boolean)
      .join('\n')
    setSharing(true)
    setError(null)
    try {
      const res = await fetch('/api/share-candidate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ space: selectedSpace, text }),
      })
      const data = (await res.json().catch(() => ({}))) as { ok?: boolean; error?: string }
      if (!res.ok || !data.ok) throw new Error(data.error || `Share failed (HTTP ${res.status})`)
      setShareDone(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSharing(false)
    }
  }

  // Reassign a candidate to a different job by updating "Job Title Applied".
  // Optimistically moves the card out of the current job's list.
  async function handleMoveCandidate(candidate: CandidateRecord, newTitle: string) {
    const current = (candidate.fields['Job Title Applied'] ?? '').trim()
    if (!newTitle || newTitle === current) return
    setMovingId(candidate.id)
    setError(null)
    setCandidates((prev) =>
      prev.map((c) =>
        c.id === candidate.id
          ? { ...c, fields: { ...c.fields, 'Job Title Applied': newTitle } }
          : c,
      ),
    )
    try {
      await updateCandidate(candidate.id, { 'Job Title Applied': newTitle })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setCandidates((prev) =>
        prev.map((c) =>
          c.id === candidate.id
            ? { ...c, fields: { ...c.fields, 'Job Title Applied': current } }
            : c,
        ),
      )
    } finally {
      setMovingId(null)
    }
  }

  // Draft an interview-invitation email (with the booking link) to the candidate.
  async function handleInviteInterview(candidate: CandidateRecord) {
    const to = candidate.fields.Email
    if (!to) return
    setInvitingId(candidate.id)
    setError(null)
    try {
      const { subject, body } = buildInterviewEmail(
        candidate.fields,
        selectedJob?.fields['Job Title'],
      )
      const res = await fetch('/api/send-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to, subject, body }),
      })
      const data = (await res.json().catch(() => ({}))) as {
        ok?: boolean
        error?: string
      }
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `Draft failed (HTTP ${res.status})`)
      }
      setInvitedIds((prev) => new Set(prev).add(candidate.id))
    } catch (e) {
      setError(
        `Could not draft interview invite for ${
          candidate.fields['Candidate Name'] ?? to
        }: ${e instanceof Error ? e.message : String(e)}`,
      )
    } finally {
      setInvitingId(null)
    }
  }

  // Records the Employee row with the offer details, then emails the candidate.
  async function handleOfferSubmit(e: FormEvent) {
    e.preventDefault()
    const candidate = offerCandidate
    const to = candidate?.fields.Email
    if (!candidate || !to) return
    setSubmittingOffer(true)
    setError(null)
    try {
      await createEmployee({
        'Candidate Name': candidate.fields['Candidate Name'],
        Email: to,
        'Job Title':
          candidate.fields['Job Title Applied'] ?? selectedJob?.fields['Job Title'],
        'Compensation Type': offerForm.compType,
        'Compensation Amount': offerForm.amount ? Number(offerForm.amount) : undefined,
        Location: offerForm.location || undefined,
        'Employment Type': offerForm.employmentType,
        'Start Date': offerForm.startDate || undefined,
        'Probation Period (Months)': offerForm.probationMonths
          ? Number(offerForm.probationMonths)
          : undefined,
        'Offer Emailed At': new Date().toISOString(),
      })

      const { subject, body } = buildHireEmail(
        candidate.fields,
        selectedJob?.fields['Job Title'],
      )
      const res = await fetch('/api/send-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to, subject, body }),
      })
      const data = (await res.json().catch(() => ({}))) as {
        ok?: boolean
        error?: string
      }
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `Send failed (HTTP ${res.status})`)
      }
      setSentIds((prev) => new Set(prev).add(candidate.id))
      setOfferCandidate(null)
    } catch (e) {
      setError(
        `Could not complete offer for ${candidate.fields['Candidate Name'] ?? to}: ${
          e instanceof Error ? e.message : String(e)
        }`,
      )
    } finally {
      setSubmittingOffer(false)
    }
  }

  if (authState === 'checking') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    )
  }
  if (authState === 'needed') {
    return <LoginScreen onSuccess={() => setAuthState('ok')} />
  }

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-6xl px-6 py-10">
        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-muted-foreground">
              Superhairpieces ATS
            </p>
            <h1 className="text-3xl font-semibold tracking-tight">Jobs</h1>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading}>
              {loading ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <RefreshCw className="size-4" />
              )}
              Refresh
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void handleLogout()}
              aria-label="Log out"
            >
              <LogOut className="size-4" />
              Log out
            </Button>
            <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
              <DialogTrigger asChild>
                <Button size="sm">
                  <Plus className="size-4" />
                  Add job
                </Button>
              </DialogTrigger>
              <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
                <DialogHeader>
                  <DialogTitle>Add a job</DialogTitle>
                  <DialogDescription>
                    Creates a record in the Jobs table of the HR Manager base.
                  </DialogDescription>
                </DialogHeader>
                <form onSubmit={handleSubmit} className="grid gap-4">
                  <div className="grid gap-2">
                    <Label htmlFor="title">Job title *</Label>
                    <Input
                      id="title"
                      value={form.title}
                      onChange={(e) => set('title')(e.target.value)}
                      placeholder="e.g. Customer Service Representative"
                      required
                    />
                  </div>

                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <div className="grid gap-2">
                      <Label>Department</Label>
                      <Select value={form.department} onValueChange={set('department')}>
                        <SelectTrigger>
                          <SelectValue placeholder="Select department" />
                        </SelectTrigger>
                        <SelectContent>
                          {DEPARTMENTS.map((d) => (
                            <SelectItem key={d} value={d}>
                              {d}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="grid gap-2">
                      <Label>Status</Label>
                      <Select value={form.status} onValueChange={set('status')}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {STATUSES.map((s) => (
                            <SelectItem key={s} value={s}>
                              {s}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="grid gap-2">
                      <Label>Employment type</Label>
                      <Select value={form.employmentType} onValueChange={set('employmentType')}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {EMPLOYMENT_TYPES.map((t) => (
                            <SelectItem key={t} value={t}>
                              {t}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="grid gap-2">
                      <Label>Work arrangement</Label>
                      <Select value={form.workArrangement} onValueChange={set('workArrangement')}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {WORK_ARRANGEMENTS.map((w) => (
                            <SelectItem key={w} value={w}>
                              {w}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  </div>

                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <div className="grid gap-2">
                      <Label htmlFor="location">Location</Label>
                      <Input
                        id="location"
                        value={form.location}
                        onChange={(e) => set('location')(e.target.value)}
                        placeholder="e.g. 29-3075 Ridgeway Drive, Mississauga"
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="schedule">Schedule</Label>
                      <Input
                        id="schedule"
                        value={form.schedule}
                        onChange={(e) => set('schedule')(e.target.value)}
                        placeholder="e.g. Mon–Fri, 9:30 AM–5:30 PM"
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                    <div className="grid gap-2">
                      <Label htmlFor="payMin">Pay min</Label>
                      <Input
                        id="payMin"
                        type="number"
                        step="0.01"
                        min="0"
                        value={form.payMin}
                        onChange={(e) => set('payMin')(e.target.value)}
                        placeholder="20.00"
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label htmlFor="payMax">Pay max</Label>
                      <Input
                        id="payMax"
                        type="number"
                        step="0.01"
                        min="0"
                        value={form.payMax}
                        onChange={(e) => set('payMax')(e.target.value)}
                        placeholder="22.00"
                      />
                    </div>
                    <div className="grid gap-2">
                      <Label>Period</Label>
                      <Select value={form.payPeriod} onValueChange={set('payPeriod')}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {PAY_PERIODS.map((p) => (
                            <SelectItem key={p} value={p}>
                              {p}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="grid gap-2">
                      <Label>Currency</Label>
                      <Select value={form.payCurrency} onValueChange={set('payCurrency')}>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {PAY_CURRENCIES.map((c) => (
                            <SelectItem key={c} value={c}>
                              {c}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="datePosted">Date posted</Label>
                    <Input
                      id="datePosted"
                      type="date"
                      value={form.datePosted}
                      onChange={(e) => set('datePosted')(e.target.value)}
                    />
                  </div>

                  <div className="grid gap-2">
                    <Label htmlFor="roleSummary">Role summary</Label>
                    <Textarea
                      id="roleSummary"
                      rows={4}
                      value={form.roleSummary}
                      onChange={(e) => set('roleSummary')(e.target.value)}
                      placeholder="Short description of the role…"
                    />
                  </div>

                  <DialogFooter>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => setDialogOpen(false)}
                      disabled={saving}
                    >
                      Cancel
                    </Button>
                    <Button type="submit" disabled={saving || !form.title.trim()}>
                      {saving && <Loader2 className="size-4 animate-spin" />}
                      Create job
                    </Button>
                  </DialogFooter>
                </form>
              </DialogContent>
            </Dialog>
          </div>
        </header>

        <div className="mb-8 grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Card>
            <CardHeader>
              <CardDescription>Total jobs</CardDescription>
              <CardTitle className="text-3xl">{jobs.length}</CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Open positions</CardDescription>
              <CardTitle className="text-3xl">{openCount}</CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Departments hiring</CardDescription>
              <CardTitle className="text-3xl">{departmentCount}</CardTitle>
            </CardHeader>
          </Card>
        </div>

        {error && (
          <div className="mb-6 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}

        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <CardTitle className="flex items-center gap-2 text-lg">
                  <Briefcase className="size-5" />
                  All jobs
                </CardTitle>
                <CardDescription>
                  {statusFilter === 'all' && locationFilter === 'all'
                    ? 'Live from the HR ATS database.'
                    : `Showing ${filteredJobs.length} of ${jobs.length} jobs.`}
                </CardDescription>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Select value={statusFilter} onValueChange={setStatusFilter}>
                  <SelectTrigger className="h-8 w-48" aria-label="Filter by status">
                    <SelectValue placeholder="Status" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ACTIVE_FILTER}>Open &amp; needs review</SelectItem>
                    <SelectItem value="all">All statuses</SelectItem>
                    {JOB_STATUSES.map((s) => (
                      <SelectItem key={s} value={s}>
                        {s}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Select value={locationFilter} onValueChange={setLocationFilter}>
                  <SelectTrigger className="h-8 w-48" aria-label="Filter by location">
                    <SelectValue placeholder="Location" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All locations</SelectItem>
                    {locations.map((loc) => (
                      <SelectItem key={loc} value={loc}>
                        {loc}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {(statusFilter !== 'all' || locationFilter !== 'all') && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setStatusFilter('all')
                      setLocationFilter('all')
                    }}
                  >
                    Clear
                  </Button>
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {loading ? (
              <div className="flex items-center justify-center gap-2 py-12 text-muted-foreground">
                <Loader2 className="size-5 animate-spin" />
                Loading jobs…
              </div>
            ) : jobs.length === 0 ? (
              <div className="py-12 text-center text-muted-foreground">
                No jobs yet. Click “Add job” to create the first one.
              </div>
            ) : filteredJobs.length === 0 ? (
              <div className="py-12 text-center text-muted-foreground">
                No jobs match the current filters.
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Job title</TableHead>
                    <TableHead>Department</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Location</TableHead>
                    <TableHead>Pay</TableHead>
                    <TableHead>Posted</TableHead>
                    <TableHead className="text-center">Candidates</TableHead>
                    <TableHead className="w-12 text-right" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredJobs.map((job) => {
                    const f = job.fields
                    const count =
                      candidatesByJob.get((f['Job Title'] ?? '').trim())?.length ?? 0
                    return (
                      <TableRow key={job.id}>
                        <TableCell className="font-medium">
                          <button
                            type="button"
                            onClick={() => setSelectedJob(job)}
                            className="text-left font-medium text-primary underline-offset-4 hover:underline"
                          >
                            {f['Job Title'] ?? 'Untitled'}
                          </button>
                        </TableCell>
                        <TableCell>{f.Department ?? '—'}</TableCell>
                        <TableCell>
                          <Select
                            value={f.Status ?? ''}
                            onValueChange={(v) => void handleStatusChange(job, v)}
                            disabled={savingStatusId === job.id}
                          >
                            <SelectTrigger
                              className={`h-8 w-36 border-transparent font-medium ${
                                f.Status ? STATUS_STYLES[f.Status] ?? '' : ''
                              }`}
                              aria-label={`Status for ${f['Job Title'] ?? 'job'}`}
                            >
                              {savingStatusId === job.id ? (
                                <Loader2 className="size-4 animate-spin" />
                              ) : (
                                <SelectValue placeholder="Set status" />
                              )}
                            </SelectTrigger>
                            <SelectContent>
                              {JOB_STATUSES.map((s) => (
                                <SelectItem key={s} value={s}>
                                  {s}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </TableCell>
                        <TableCell>{f['Employment Type'] ?? '—'}</TableCell>
                        <TableCell className="max-w-52">
                          {f.Location ? (
                            <span className="flex items-center gap-1 truncate">
                              <MapPin className="size-3.5 shrink-0 text-muted-foreground" />
                              <span className="truncate">{f.Location}</span>
                            </span>
                          ) : (
                            '—'
                          )}
                        </TableCell>
                        <TableCell className="whitespace-nowrap">{formatPay(f)}</TableCell>
                        <TableCell className="whitespace-nowrap">
                          {formatDate(f['Date Posted'])}
                        </TableCell>
                        <TableCell className="text-center">
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-7 gap-1.5 px-2"
                            onClick={() => setSelectedJob(job)}
                          >
                            <Users className="size-3.5" />
                            {count}
                          </Button>
                        </TableCell>
                        <TableCell className="text-right">
                          <AlertDialog>
                            <AlertDialogTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="text-muted-foreground hover:text-destructive"
                                disabled={deletingId === job.id}
                                aria-label={`Delete ${f['Job Title'] ?? 'job'}`}
                              >
                                {deletingId === job.id ? (
                                  <Loader2 className="size-4 animate-spin" />
                                ) : (
                                  <Trash2 className="size-4" />
                                )}
                              </Button>
                            </AlertDialogTrigger>
                            <AlertDialogContent>
                              <AlertDialogHeader>
                                <AlertDialogTitle>
                                  Delete “{f['Job Title'] ?? 'this job'}”?
                                </AlertDialogTitle>
                                <AlertDialogDescription>
                                  This hides the job from the dashboard. It stays
                                  in the database (marked deleted), so it can be
                                  restored if needed.
                                </AlertDialogDescription>
                              </AlertDialogHeader>
                              <AlertDialogFooter>
                                <AlertDialogCancel>Cancel</AlertDialogCancel>
                                <AlertDialogAction
                                  onClick={() => void handleDelete(job.id)}
                                  className="bg-destructive text-white hover:bg-destructive/90"
                                >
                                  Delete
                                </AlertDialogAction>
                              </AlertDialogFooter>
                            </AlertDialogContent>
                          </AlertDialog>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>

      <Dialog
        open={!!selectedJob}
        onOpenChange={(open) => {
          if (!open) setSelectedJob(null)
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Users className="size-5" />
              Candidates — {selectedJob?.fields['Job Title']}
            </DialogTitle>
            <DialogDescription>
              {selectedCandidates.length === 0
                ? 'No candidates have applied for this position yet.'
                : `${selectedCandidates.length} applicant${
                    selectedCandidates.length === 1 ? '' : 's'
                  } from the Candidates table.`}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3">
            {selectedCandidates.map((c) => {
              const cf = c.fields
              const resume = cf['Résumé']?.[0]
              return (
                <div
                  key={c.id}
                  className="rounded-lg border bg-card p-4 text-sm"
                >
                  <div className="mb-2 flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2">
                      {cf['Match Score'] != null && (
                        <span
                          className={`inline-flex h-9 min-w-9 items-center justify-center rounded-full px-2 text-sm font-bold ${scoreColor(
                            cf['Match Score'],
                          )}`}
                          title="AI match score (0–100)"
                        >
                          {cf['Match Score']}
                        </span>
                      )}
                      <div className="font-semibold">
                        {cf['Candidate Name'] ?? 'Unknown'}
                      </div>
                    </div>
                    {cf.Status && (
                      <Badge
                        variant="outline"
                        className={`border-transparent ${
                          CANDIDATE_STATUS_STYLES[cf.Status] ?? ''
                        }`}
                      >
                        {cf.Status}
                      </Badge>
                    )}
                  </div>

                  {cf['Match Notes'] && (
                    <p className="mb-2 rounded-md bg-muted/50 px-2.5 py-1.5 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">
                        Match:{' '}
                      </span>
                      {cf['Match Notes']}
                    </p>
                  )}

                  <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
                    {cf['Years of Experience'] != null && (
                      <span>{cf['Years of Experience']} yrs experience</span>
                    )}
                    {cf['Candidate Location'] && (
                      <span className="flex items-center gap-1">
                        <MapPin className="size-3.5" />
                        {cf['Candidate Location']}
                      </span>
                    )}
                    {cf.Email && <span>{cf.Email}</span>}
                    {cf.Phone && <span>{cf.Phone}</span>}
                  </div>

                  {cf['Key Skills'] && (
                    <p className="mb-2 text-muted-foreground">
                      <span className="font-medium text-foreground">Skills: </span>
                      {cf['Key Skills']}
                    </p>
                  )}
                  {cf['AI Summary'] && (
                    <p className="mb-2 text-muted-foreground">{cf['AI Summary']}</p>
                  )}

                  <div className="flex flex-wrap items-center gap-3 pt-1">
                    {resume && (
                      <a
                        href={resume.url}
                        target="_blank"
                        rel="noreferrer"
                        title="Résumé"
                        aria-label="Résumé"
                        className="flex items-center text-primary hover:opacity-70"
                      >
                        <FileText className="size-4" />
                      </a>
                    )}
                    {cf['Résumé Drive Link'] && (
                      <a
                        href={cf['Résumé Drive Link']}
                        target="_blank"
                        rel="noreferrer"
                        className="flex items-center gap-1 text-primary underline-offset-4 hover:underline"
                      >
                        <ExternalLink className="size-3.5" />
                        Drive
                      </a>
                    )}
                    {cf['Indeed Profile Link'] && (
                      <a
                        href={cf['Indeed Profile Link']}
                        target="_blank"
                        rel="noreferrer"
                        className="flex items-center gap-1 text-primary underline-offset-4 hover:underline"
                      >
                        <ExternalLink className="size-3.5" />
                        Indeed profile
                      </a>
                    )}
                    <button
                      type="button"
                      onClick={() => void openShareDialog(c)}
                      title="Share résumé + match score to Google Chat"
                      className="flex items-center gap-1 text-primary underline-offset-4 hover:underline"
                    >
                      <Share2 className="size-3.5" />
                      Share
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleInviteInterview(c)}
                      disabled={invitingId === c.id || !cf.Email}
                      title={
                        cf.Email
                          ? 'Draft an interview invitation email with your booking link'
                          : 'No email address on file'
                      }
                      className="flex items-center gap-1 text-primary underline-offset-4 hover:underline disabled:opacity-50 disabled:no-underline"
                    >
                      {invitingId === c.id ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : invitedIds.has(c.id) ? (
                        <Check className="size-3.5" />
                      ) : (
                        <CalendarPlus className="size-3.5" />
                      )}
                      {invitedIds.has(c.id) ? 'Invite drafted' : 'Invite to interview'}
                    </button>
                    <Select
                      value=""
                      onValueChange={(v) => void handleMoveCandidate(c, v)}
                      disabled={movingId === c.id}
                    >
                      <SelectTrigger
                        className="h-8 w-44"
                        aria-label={`Move ${cf['Candidate Name'] ?? 'candidate'} to another job`}
                      >
                        {movingId === c.id ? (
                          <Loader2 className="size-4 animate-spin" />
                        ) : (
                          <SelectValue placeholder="Move to job…" />
                        )}
                      </SelectTrigger>
                      <SelectContent>
                        {jobTitles
                          .filter(
                            (t) => t !== (selectedJob?.fields['Job Title'] ?? '').trim(),
                          )
                          .map((t) => (
                            <SelectItem key={t} value={t}>
                              {t}
                            </SelectItem>
                          ))}
                      </SelectContent>
                    </Select>
                    {!cf.Email ? (
                      <Button
                        size="sm"
                        variant="outline"
                        className="ml-auto"
                        disabled
                        title="No email address on file for this candidate"
                      >
                        <Mail className="size-3.5" />
                        Draft hire email
                      </Button>
                    ) : sentIds.has(c.id) ? (
                      <span className="ml-auto flex items-center gap-1 text-sm font-medium text-emerald-600 dark:text-emerald-400">
                        <Check className="size-4" />
                        Draft created
                      </span>
                    ) : (
                      <Button
                        size="sm"
                        className="ml-auto"
                        onClick={() => openOfferDialog(c)}
                      >
                        <Mail className="size-3.5" />
                        Draft hire email
                      </Button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!offerCandidate}
        onOpenChange={(open) => {
          if (!open && !submittingOffer) setOfferCandidate(null)
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              Prepare offer — {offerCandidate?.fields['Candidate Name'] ?? 'candidate'}
            </DialogTitle>
            <DialogDescription>
              Saves these details to the Employee table and creates a draft
              email to {offerCandidate?.fields.Email} in your Gmail — you review
              and send it. Nothing is sent automatically.
            </DialogDescription>
          </DialogHeader>

          <form onSubmit={handleOfferSubmit} className="grid gap-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label>Compensation</Label>
                <Select value={offerForm.compType} onValueChange={setOffer('compType')}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {COMP_TYPES.map((t) => (
                      <SelectItem key={t} value={t}>
                        {t}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="offer-amount">
                  Amount ({offerForm.compType === 'Hourly' ? 'per hour' : 'per year'})
                </Label>
                <Input
                  id="offer-amount"
                  type="number"
                  step="0.01"
                  min="0"
                  value={offerForm.amount}
                  onChange={(e) => setOffer('amount')(e.target.value)}
                  placeholder={offerForm.compType === 'Hourly' ? '22.00' : '55000'}
                  required
                />
              </div>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label>Location</Label>
                <Select value={offerForm.location} onValueChange={setOffer('location')}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select location" />
                  </SelectTrigger>
                  <SelectContent>
                    {OFFER_LOCATIONS.map((l) => (
                      <SelectItem key={l} value={l}>
                        {l}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-2">
                <Label>Employment</Label>
                <Select
                  value={offerForm.employmentType}
                  onValueChange={setOffer('employmentType')}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {OFFER_EMPLOYMENT_TYPES.map((t) => (
                      <SelectItem key={t} value={t}>
                        {t}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor="offer-start">Start date</Label>
                <Input
                  id="offer-start"
                  type="date"
                  value={offerForm.startDate}
                  onChange={(e) => setOffer('startDate')(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label>Probation period</Label>
                <Select
                  value={offerForm.probationMonths}
                  onValueChange={setOffer('probationMonths')}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PROBATION_MONTHS.map((m) => (
                      <SelectItem key={m} value={m}>
                        {m} {m === '1' ? 'month' : 'months'}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setOfferCandidate(null)}
                disabled={submittingOffer}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  submittingOffer ||
                  !offerForm.amount ||
                  !offerForm.location ||
                  !offerForm.startDate
                }
              >
                {submittingOffer && <Loader2 className="size-4 animate-spin" />}
                Save &amp; create draft
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!shareCandidate}
        onOpenChange={(open) => {
          if (!open && !sharing) setShareCandidate(null)
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Share2 className="size-5" />
              Share to Google Chat
            </DialogTitle>
            <DialogDescription>
              Posts {shareCandidate?.fields['Candidate Name'] ?? 'this candidate'}&apos;s
              AI match score and résumé link to the conversation you pick.
            </DialogDescription>
          </DialogHeader>

          {shareDone ? (
            <div className="flex items-center gap-2 py-2 text-sm font-medium text-emerald-600 dark:text-emerald-400">
              <Check className="size-4" />
              Shared to Google Chat
            </div>
          ) : (
            <div className="grid gap-2">
              <Label>Conversation</Label>
              {spacesStatus === 'loading' ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  Loading your Chat conversations…
                </div>
              ) : spacesStatus === 'error' ? (
                <p className="text-sm text-destructive">
                  Couldn&apos;t load your Chat conversations.
                </p>
              ) : chatSpaces.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No Chat conversations found for your account.
                </p>
              ) : (
                <Select value={selectedSpace} onValueChange={setSelectedSpace}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select a conversation" />
                  </SelectTrigger>
                  <SelectContent>
                    {chatSpaces.map((s) => (
                      <SelectItem key={s.name} value={s.name}>
                        {s.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          )}

          <DialogFooter>
            {shareDone ? (
              <Button onClick={() => setShareCandidate(null)}>Done</Button>
            ) : (
              <>
                <Button
                  variant="outline"
                  onClick={() => setShareCandidate(null)}
                  disabled={sharing}
                >
                  Cancel
                </Button>
                <Button
                  onClick={() => void handleShare()}
                  disabled={sharing || !selectedSpace}
                >
                  {sharing && <Loader2 className="size-4 animate-spin" />}
                  Share
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
