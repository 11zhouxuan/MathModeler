// Server-side chat-history store for the MathModeler portal.
//
// Sessions and messages are persisted on the backend (jobs/{session_id}/ui_history.json)
// so the same user sees identical history across all browsers/devices.
//
// API endpoints (portal backend):
//   GET  /api/sessions                    -> {sessions: SessionMeta[]}
//   GET  /api/sessions/:id/messages       -> {frames: [...]} or {messages: [...]}
//   POST /api/sessions/:id/messages       -> save messages from frontend

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || '';

export type SessionMeta = {
  id: string;
  title: string;
  updatedAt: number;
};

/** AgentCore requires runtimeSessionId >= 33 chars; "mm-"+32 hex = 35. */
export function newSessionId(): string {
  let hex = '';
  try {
    const buf = new Uint8Array(16);
    crypto.getRandomValues(buf);
    hex = Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
  } catch {
    hex = (Math.random().toString(16) + Math.random().toString(16)).replace(/\./g, '').slice(0, 32);
  }
  return 'mm-' + hex.padEnd(32, '0').slice(0, 32);
}

function _token(): string {
  return typeof window !== 'undefined' ? sessionStorage.getItem('mm_token') || '' : '';
}

function _headers(): Record<string, string> {
  return { Authorization: `Bearer ${_token()}`, 'Content-Type': 'application/json' };
}

/** Fetch the session list from the backend (async). Falls back to empty on error. */
export async function listSessionsAsync(): Promise<SessionMeta[]> {
  try {
    const res = await fetch(`${API_BASE}/api/sessions`, { headers: _headers() });
    if (!res.ok) return [];
    const data = await res.json();
    return (data.sessions || []) as SessionMeta[];
  } catch {
    return [];
  }
}

/** Synchronous wrapper: returns cached list or empty (triggers async refresh). */
let _cachedSessions: SessionMeta[] = [];
export function listSessions(): SessionMeta[] {
  // Trigger async refresh in background
  listSessionsAsync().then((s) => { _cachedSessions = s; });
  return _cachedSessions;
}

/** Load messages for a session from the backend. */
export async function loadMessagesAsync(id: string): Promise<any[]> {
  try {
    const res = await fetch(`${API_BASE}/api/sessions/${id}/messages`, { headers: _headers() });
    if (!res.ok) return [];
    const data = await res.json();
    return data.messages || data.frames || [];
  } catch {
    return [];
  }
}

/** Synchronous compatibility: returns empty (use loadMessagesAsync for actual data). */
export function loadMessages(id: string): any[] {
  return [];
}

/** Derive a short, human-readable title from the first user message text. */
export function deriveTitle(messages: any[]): string {
  const firstUser = (messages || []).find((m) => m?.role === 'user');
  const text = (firstUser?.parts || [])
    .filter((p: any) => p?.type === 'text')
    .map((p: any) => p.text)
    .join('')
    .trim();
  if (!text) return '新会话';
  return text.length > 24 ? text.slice(0, 24) + '…' : text;
}

/** Persist (upsert) a session's messages to the backend. */
export async function saveSessionAsync(id: string, messages: any[]): Promise<void> {
  if (!id || !messages || messages.length === 0) return;
  const problem = deriveTitle(messages);
  try {
    await fetch(`${API_BASE}/api/sessions/${id}/messages`, {
      method: 'POST',
      headers: _headers(),
      body: JSON.stringify({ messages, problem }),
    });
  } catch {
    // Best-effort; silent failure.
  }
}

/** Synchronous compatibility wrapper (fires async save in background). */
export function saveSession(id: string, messages: any[]): SessionMeta[] {
  saveSessionAsync(id, messages);
  return _cachedSessions;
}

export function deleteSession(id: string): SessionMeta[] {
  // Remove from local cache immediately (optimistic update).
  _cachedSessions = _cachedSessions.filter((s) => s.id !== id);
  // Fire async DELETE to backend (best-effort).
  deleteSessionAsync(id);
  return _cachedSessions;
}

export async function deleteSessionAsync(id: string): Promise<void> {
  if (!id) return;
  try {
    await fetch(`${API_BASE}/api/sessions/${id}`, {
      method: 'DELETE',
      headers: _headers(),
    });
  } catch {
    // Best-effort; silent failure.
  }
}

export function getCollapsed(): boolean {
  if (typeof window === 'undefined') return false;
  return localStorage.getItem('mm_history_collapsed') === '1';
}

export function setCollapsed(v: boolean): void {
  if (typeof window !== 'undefined') {
    localStorage.setItem('mm_history_collapsed', v ? '1' : '0');
  }
}
