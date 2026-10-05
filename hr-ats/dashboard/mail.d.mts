import type { IncomingMessage, ServerResponse } from 'node:http'

// Handles POST /api/send-email. Returns true once it has written a response.
export function handleSendEmail(
  req: IncomingMessage,
  res: ServerResponse,
): Promise<boolean>

export function handleChatSpaces(
  req: IncomingMessage,
  res: ServerResponse,
): Promise<boolean>

export function handleShareCandidate(
  req: IncomingMessage,
  res: ServerResponse,
): Promise<boolean>
