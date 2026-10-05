import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
import fs from 'node:fs'
import crypto from 'node:crypto'
import { handleSendEmail, handleChatSpaces, handleShareCandidate } from './mail.mjs'

// Read a var from the shared ../.env for the dev server.
function readEnv(name: string): string | null {
  try {
    const content = fs.readFileSync(path.resolve(__dirname, '../.env'), 'utf-8')
    return content.match(new RegExp(`^${name}=(.+)$`, 'm'))?.[1].trim() ?? null
  } catch {
    return null
  }
}

const DASHBOARD_PASSWORD = readEnv('DASHBOARD_PASSWORD')

function devSessionToken(): string {
  return crypto
    .createHmac('sha256', DASHBOARD_PASSWORD || '')
    .update('shp-ats-session-v1')
    .digest('hex')
}

function devIsAuthed(req: any): boolean {
  if (!DASHBOARD_PASSWORD) return true
  const cookie = (req.headers.cookie || '') as string
  return cookie.split(';').some((p: string) => {
    const [k, v] = p.split('=')
    return k?.trim() === 'shp_session' && v?.trim() === devSessionToken()
  })
}

// Dev-server middleware exposing the app's /api endpoints, mirroring what
// server.mjs serves in production, so the features work in `vite dev` too.
const API_HANDLERS: Record<string, unknown> = {
  '/api/send-email': handleSendEmail,
  '/api/chat/spaces': handleChatSpaces,
  '/api/share-candidate': handleShareCandidate,
}

function apiEndpointsPlugin() {
  return {
    name: 'api-endpoints',
    configureServer(server: { middlewares: { use: (fn: unknown) => void } }) {
      server.middlewares.use((req: any, res: any, next: () => void) => {
        const path = req.url?.split('?')[0]
        const json = (code: number, obj: unknown, headers: object = {}) => {
          res.writeHead(code, { 'Content-Type': 'application/json', ...headers })
          res.end(JSON.stringify(obj))
        }

        // --- auth endpoints (mirror server.mjs) ---
        if (path === '/api/session') {
          return json(200, {
            authed: devIsAuthed(req),
            required: !!DASHBOARD_PASSWORD,
          })
        }
        if (path === '/api/login' && req.method === 'POST') {
          const chunks: Buffer[] = []
          req.on('data', (c: Buffer) => chunks.push(c))
          req.on('end', () => {
            const body = JSON.parse(Buffer.concat(chunks).toString() || '{}')
            if (!DASHBOARD_PASSWORD || body.password === DASHBOARD_PASSWORD) {
              json(200, { ok: true }, {
                'Set-Cookie': `shp_session=${devSessionToken()}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000`,
              })
            } else {
              json(401, { ok: false, error: 'Incorrect password' })
            }
          })
          return
        }
        if (path === '/api/logout') {
          return json(200, { ok: true }, {
            'Set-Cookie': 'shp_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0',
          })
        }
        if (path?.startsWith('/api/') && !devIsAuthed(req)) {
          return json(401, { error: 'Authentication required' })
        }

        const handler = API_HANDLERS[path] as
          | ((r: unknown, s: unknown) => void)
          | undefined
        if (handler) {
          void handler(req, res)
          return
        }
        next()
      })
    },
  }
}

// Read the Airtable token from the shared .env one folder up so the secret
// stays server-side; the browser only ever talks to the /api/airtable proxy.
// Absent in CI/Docker builds, where only `vite build` runs and no proxy is needed.
function readAirtableToken(): string | null {
  try {
    const envPath = path.resolve(__dirname, '../.env')
    const content = fs.readFileSync(envPath, 'utf-8')
    return content.match(/^AIRTABLE_TOKEN=(.+)$/m)?.[1].trim() ?? null
  } catch {
    return null
  }
}

const token = readAirtableToken()

export default defineConfig({
  plugins: [react(), tailwindcss(), apiEndpointsPlugin()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: token
      ? {
          '/api/airtable': {
            target: 'https://api.airtable.com',
            changeOrigin: true,
            rewrite: (p) => p.replace(/^\/api\/airtable/, ''),
            headers: {
              Authorization: `Bearer ${token}`,
            },
          },
        }
      : undefined,
  },
})
