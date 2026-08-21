const BASE = ''  // same origin

interface RequestOptions {
  signal?: AbortSignal
}

async function request<T>(method: string, path: string, body?: unknown, options?: RequestOptions): Promise<T> {
  const url = `${BASE}${path}`
  console.log(`[API] ${method} ${url}`)
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
    signal: options?.signal,
  })
  if (!res.ok) {
    const text = await res.text()
    console.error(`[API ERROR] ${method} ${url} - ${res.status}: ${text}`)
    throw new Error(`${res.status} ${text}`)
  }
  return res.json()
}

/** Upload a file as multipart/form-data. Content-Type is left to the browser so
 *  it can set the multipart boundary itself. */
async function upload<T>(path: string, form: FormData): Promise<T> {
  const url = `${BASE}${path}`
  console.log(`[API] POST ${url} (upload)`)
  const res = await fetch(url, { method: 'POST', body: form })
  if (!res.ok) {
    const text = await res.text()
    console.error(`[API ERROR] POST ${url} - ${res.status}: ${text}`)
    // Import failures carry a parser message worth showing the user verbatim.
    try {
      throw new Error(JSON.parse(text).detail ?? text)
    } catch (e) {
      throw e instanceof Error ? e : new Error(text)
    }
  }
  return res.json()
}

export const api = {
  upload,
  get: <T>(path: string, options?: RequestOptions) => request<T>('GET', path, undefined, options),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  delete: <T>(path: string) => request<T>('DELETE', path),
}
