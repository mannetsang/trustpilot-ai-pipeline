// Shared /api/send-email handler used by both the Vite dev server
// (vite.config.ts) and the production server (server.mjs).
//
// Creates a Gmail DRAFT (never sends) in the connected account via the Gmail
// REST API, using pure Node (no Python, no extra runtime) so it works inside
// the node:slim Cloud Run container. The user reviews the draft and sends it.
//
// Credentials resolution:
//   1. Env vars GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN
//      (production — injected from Secret Manager).
//   2. Fallback: ../token.json written by gmail_organizer.py (local dev only).
//
// SECURITY: like the /api/db routes, this endpoint has no auth layer of its
// own. Put it behind IAP / app-level login before any wide rollout so it
// cannot be used as an open relay to send mail as the connected account.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const TOKEN_URI = 'https://oauth2.googleapis.com/token'
const DRAFTS_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/drafts'
const SENDAS_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/settings/sendAs'

let cachedToken = null // { value, expiresAt }

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')))
    req.on('error', reject)
  })
}

function pickCredFields(raw, source) {
  if (!raw.client_id || !raw.client_secret || !raw.refresh_token) {
    throw new Error(`${source} is missing client_id, client_secret, or refresh_token`)
  }
  return {
    client_id: raw.client_id,
    client_secret: raw.client_secret,
    refresh_token: raw.refresh_token,
  }
}

// OAuth client id + secret + refresh token. Resolution order:
//   1. GMAIL_TOKEN_JSON_B64  — base64 of a full token.json (prod / Cloud Run).
//   2. GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN discrete vars.
//   3. ../token.json on disk (local dev).
function loadCredentials() {
  const { GMAIL_TOKEN_JSON_B64, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN } =
    process.env

  if (GMAIL_TOKEN_JSON_B64) {
    let raw
    try {
      raw = JSON.parse(Buffer.from(GMAIL_TOKEN_JSON_B64, 'base64').toString('utf-8'))
    } catch {
      throw new Error('GMAIL_TOKEN_JSON_B64 is not valid base64-encoded JSON')
    }
    return pickCredFields(raw, 'GMAIL_TOKEN_JSON_B64')
  }

  if (GMAIL_CLIENT_ID && GMAIL_CLIENT_SECRET && GMAIL_REFRESH_TOKEN) {
    return {
      client_id: GMAIL_CLIENT_ID,
      client_secret: GMAIL_CLIENT_SECRET,
      refresh_token: GMAIL_REFRESH_TOKEN,
    }
  }

  const tokenPath = path.resolve(__dirname, '..', 'token.json')
  let raw
  try {
    raw = JSON.parse(fs.readFileSync(tokenPath, 'utf-8'))
  } catch {
    throw new Error(
      'No Gmail credentials: set GMAIL_TOKEN_JSON_B64 (or GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN), or provide ../token.json',
    )
  }
  return pickCredFields(raw, 'token.json')
}

async function getAccessToken() {
  if (cachedToken && cachedToken.expiresAt > Date.now() + 60_000) {
    return cachedToken.value
  }
  const creds = loadCredentials()
  const res = await fetch(TOKEN_URI, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: creds.client_id,
      client_secret: creds.client_secret,
      refresh_token: creds.refresh_token,
      grant_type: 'refresh_token',
    }),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(
      `OAuth token refresh failed: ${data.error_description || data.error || res.status}`,
    )
  }
  cachedToken = {
    value: data.access_token,
    expiresAt: Date.now() + (data.expires_in ?? 3600) * 1000,
  }
  return cachedToken.value
}

// Fetches the HTML signature of the account's primary send-as address, so sent
// mail carries the same signature Gmail's compose window would add.
async function fetchPrimarySignature(accessToken) {
  const res = await fetch(SENDAS_URL, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const err = new Error(data.error?.message || `HTTP ${res.status}`)
    err.status = res.status
    throw err
  }
  const list = data.sendAs || []
  const primary = list.find((s) => s.isPrimary) || list[0]
  return primary?.signature || ''
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// Plain-text body (\n line breaks) + optional HTML signature -> HTML document.
function composeHtml(body, signature) {
  const bodyHtml = escapeHtml(body).replace(/\n/g, '<br>\n')
  const sig = signature ? `<br><br>${signature}` : ''
  return `<div dir="ltr">${bodyHtml}${sig}</div>`
}

// Builds an RFC 2822 text/html message and base64url-encodes it for Gmail.
function buildRawMessage({ to, subject, html }) {
  const asciiSubject = [...subject].every((ch) => ch.charCodeAt(0) <= 0x7f)
  const encodedSubject = asciiSubject
    ? subject
    : `=?UTF-8?B?${Buffer.from(subject, 'utf-8').toString('base64')}?=`
  const encodedBody = Buffer.from(html, 'utf-8')
    .toString('base64')
    .replace(/(.{76})/g, '$1\r\n')
  const message =
    [
      `To: ${to}`,
      `Subject: ${encodedSubject}`,
      'MIME-Version: 1.0',
      'Content-Type: text/html; charset="UTF-8"',
      'Content-Transfer-Encoding: base64',
      '',
    ].join('\r\n') +
    '\r\n' +
    encodedBody
  return Buffer.from(message, 'utf-8').toString('base64url')
}

async function draftEmail({ to, subject, body }) {
  const accessToken = await getAccessToken()

  // Best-effort: attach the account's Gmail signature. Reports status so the
  // caller can tell whether it was applied (e.g. if the scope is insufficient).
  let signature = ''
  let signatureStatus = 'applied'
  try {
    signature = await fetchPrimarySignature(accessToken)
    if (!signature) signatureStatus = 'no signature configured in Gmail'
  } catch (e) {
    signatureStatus = `not applied: ${e instanceof Error ? e.message : String(e)}`
  }

  // Create a Gmail DRAFT (never send). The user reviews it and clicks send.
  const html = composeHtml(body, signature)
  const res = await fetch(DRAFTS_URL, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ message: { raw: buildRawMessage({ to, subject, html }) } }),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(`Gmail draft failed: ${data.error?.message || res.status}`)
  }
  return { draftId: data.id, signatureStatus }
}

// Handles a POST /api/send-email request. Works with both raw Node http and
// Connect-style (req, res) objects. Returns true once a response is written.
export async function handleSendEmail(req, res) {
  if (req.method !== 'POST') {
    res.writeHead(405, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: 'Method not allowed' }))
    return true
  }
  let payload
  try {
    payload = JSON.parse((await readBody(req)) || '{}')
  } catch {
    res.writeHead(400, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: 'Invalid JSON body' }))
    return true
  }
  const to = (payload.to || '').trim()
  const subject = payload.subject || ''
  if (!to || !subject) {
    res.writeHead(400, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: 'Fields "to" and "subject" are required' }))
    return true
  }
  if (!to.includes('@')) {
    res.writeHead(400, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: `Recipient "${to}" is not a valid email` }))
    return true
  }
  try {
    const result = await draftEmail({ to, subject, body: payload.body ?? '' })
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: true, drafted: true, ...result }))
  } catch (e) {
    res.writeHead(502, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: e instanceof Error ? e.message : String(e) }))
  }
  return true
}

// ---- Google Chat: list the user's spaces/DMs and post a message as them ----
const CHAT_BASE = 'https://chat.googleapis.com/v1'

async function chatGet(token, path) {
  const res = await fetch(`${CHAT_BASE}/${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.error?.message || `HTTP ${res.status}`)
  return data
}

// Chat's members API returns only opaque user ids (not names) under user auth,
// so DMs can't be labeled by person without the Directory/People API. List the
// named conversations (spaces + named group chats) the user can post to.
async function listChatSpaces() {
  const token = await getAccessToken()
  const data = await chatGet(token, 'spaces?pageSize=100')
  const out = (data.spaces || [])
    .filter((s) => s.name && s.displayName)
    .map((s) => ({ name: s.name, label: s.displayName, type: s.spaceType }))
  out.sort((a, b) => a.label.localeCompare(b.label))
  return out
}

async function postChatMessage(space, text) {
  const token = await getAccessToken()
  const res = await fetch(`${CHAT_BASE}/${space}/messages`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(`Chat post failed: ${data.error?.message || res.status}`)
  return { name: data.name }
}

// GET /api/chat/spaces — the spaces/DMs the signed-in account can post to.
export async function handleChatSpaces(_req, res) {
  try {
    const spaces = await listChatSpaces()
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: true, spaces }))
  } catch (e) {
    res.writeHead(502, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: e instanceof Error ? e.message : String(e) }))
  }
  return true
}

// POST /api/share-candidate {space, text} — post the message to the chosen space.
export async function handleShareCandidate(req, res) {
  if (req.method !== 'POST') {
    res.writeHead(405, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: 'Method not allowed' }))
    return true
  }
  let payload
  try {
    payload = JSON.parse((await readBody(req)) || '{}')
  } catch {
    res.writeHead(400, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: 'Invalid JSON body' }))
    return true
  }
  if (!payload.space || !payload.text) {
    res.writeHead(400, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: 'Fields "space" and "text" are required' }))
    return true
  }
  try {
    const result = await postChatMessage(payload.space, payload.text)
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: true, ...result }))
  } catch (e) {
    res.writeHead(502, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: e instanceof Error ? e.message : String(e) }))
  }
  return true
}
