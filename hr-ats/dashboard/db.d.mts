import type { IncomingMessage, ServerResponse } from 'node:http'

export function handleDb(
  req: IncomingMessage,
  res: ServerResponse,
  pathname: string,
  readBody: (req: IncomingMessage) => Promise<Buffer>,
): Promise<boolean>
