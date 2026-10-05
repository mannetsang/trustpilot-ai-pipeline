// Production server for the jobs dashboard.
// - Serves the static Vite build from ./dist
// - Proxies /api/airtable/* to the Airtable API, injecting the token
//   server-side so it never reaches the browser
// - Password login: when DASHBOARD_PASSWORD is set, all /api/* routes (except
//   the auth endpoints) require a valid session cookie. The password itself is
//   only checked server-side and never ships to the browser.
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { fileURLToPath } from 'node:url'
import {
  handleSendEmail,
  handleChatSpaces,
  handleShareCandidate,
} from './mail.mjs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const DIST = path.join(__dirname, 'dist')
const PORT = Number(process.env.PORT ?? 8080)
const TOKEN = process.env.AIRTABLE_TOKEN
const PASSWORD = process.env.DASHBOARD_PASSWORD

if (!TOKEN) {
  console.error('AIRTABLE_TOKEN environment variable is required')
  process.exit(1)
}

// A session cookie is HMAC(password) — an attacker can't forge it without the
// password, and no extra secret needs to be managed.
function sessionToken() {
  return crypto
    .createHmac('sha256', PASSWORD || '')
    .update('shp-ats-session-v1')
    .digest('hex')
}

function parseCookies(req) {
  const out = {}
  for (const part of (req.headers.cookie || '').split(';')) {
    const i = part.indexOf('=')
    if (i > -1) out[part.slice(0, i).trim()] = part.slice(i + 1).trim()
  }
  return out
}

function isAuthed(req) {
  if (!PASSWORD) return true // no password configured => open
  return parseCookies(req)['shp_session'] === sessionToken()
}

function sendJson(res, code, obj, extraHeaders = {}) {
  res.writeHead(code, { 'Content-Type': 'application/json', ...extraHeaders })
  res.end(JSON.stringify(obj))
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.json': 'application/json',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ttf': 'font/ttf',
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

async function proxyAirtable(req, res, url) {
  const target =
    'https://api.airtable.com' +
    url.pathname.replace(/^\/api\/airtable/, '') +
    url.search
  const init = {
    method: req.method,
    headers: { Authorization: `Bearer ${TOKEN}` },
  }
  if (!['GET', 'HEAD', 'DELETE'].includes(req.method)) {
    init.headers['Content-Type'] =
      req.headers['content-type'] ?? 'application/json'
    init.body = await readBody(req)
  }
  const upstream = await fetch(target, init)
  const body = Buffer.from(await upstream.arrayBuffer())
  res.writeHead(upstream.status, {
    'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
  })
  res.end(body)
}

function serveStatic(res, pathname) {
  let filePath = path.normalize(path.join(DIST, pathname))
  if (!filePath.startsWith(DIST)) {
    res.writeHead(403)
    return res.end('Forbidden')
  }
  if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
    filePath = path.join(DIST, 'index.html') // SPA fallback
  }
  const ext = path.extname(filePath).toLowerCase()
  res.writeHead(200, { 'Content-Type': MIME[ext] ?? 'application/octet-stream' })
  fs.createReadStream(filePath).pipe(res)
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost')

    // --- auth endpoints (always reachable) ---
    if (url.pathname === '/api/session') {
      return sendJson(res, 200, { authed: isAuthed(req), required: !!PASSWORD })
    }
    if (url.pathname === '/api/login' && req.method === 'POST') {
      const body = JSON.parse((await readBody(req)).toString() || '{}')
      if (!PASSWORD || body.password === PASSWORD) {
        const secure =
          req.headers['x-forwarded-proto'] === 'https' ? '; Secure' : ''
        const cookie = `shp_session=${sessionToken()}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000${secure}`
        return sendJson(res, 200, { ok: true }, { 'Set-Cookie': cookie })
      }
      return sendJson(res, 401, { ok: false, error: 'Incorrect password' })
    }
    if (url.pathname === '/api/logout') {
      const cookie = `shp_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`
      return sendJson(res, 200, { ok: true }, { 'Set-Cookie': cookie })
    }

    // --- everything else under /api requires a session ---
    if (url.pathname.startsWith('/api/') && !isAuthed(req)) {
      return sendJson(res, 401, { error: 'Authentication required' })
    }

    if (url.pathname === '/api/send-email') {
      return void (await handleSendEmail(req, res))
    }
    if (url.pathname === '/api/chat/spaces') {
      return void (await handleChatSpaces(req, res))
    }
    if (url.pathname === '/api/share-candidate') {
      return void (await handleShareCandidate(req, res))
    }
    if (url.pathname.startsWith('/api/airtable/')) {
      return await proxyAirtable(req, res, url)
    }
    return serveStatic(res, url.pathname)
  } catch (err) {
    console.error(err)
    res.writeHead(500, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ error: 'Internal server error' }))
  }
})

server.listen(PORT, () => {
  console.log(`Jobs dashboard listening on port ${PORT}`)
})
