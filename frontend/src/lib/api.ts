const BASE = process.env.NEXT_PUBLIC_API_URL ?? '';

async function request<T>(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Token ${token}` } : {}),
  };
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { ...headers, ...(options.headers as Record<string, string> ?? {}) },
  });
  const data: unknown = await res.json();
  if (!res.ok) {
    const err = data as Record<string, unknown>;
    const message = typeof err?.detail === 'string' ? err.detail : JSON.stringify(data);
    throw new Error(message);
  }
  return data as T;
}

export interface Member {
  id: string;
  local_id: string;
  full_name: string;
  email: string;
  classification: string;
  status: string;
  role: string;
}

export interface Announcement {
  id: string;
  local: string;
  created_by: string;
  title: string;
  body: string;
  push_preview: string;
  target_classification: string | null;
  needs_ack: boolean;
  status: 'draft' | 'confirmed' | 'queued' | 'sent';
  confirmed_content_hash: string;
  created_at: string;
  confirmed_at: string | null;
  queued_at: string | null;
}

export interface AIDraft {
  title: string;
  body: string;
  push_preview: string;
}

export function apiLogin(email: string, password: string) {
  return request<{ token: string; token_type: string; member: Member }>(
    '/api/login/',
    { method: 'POST', body: JSON.stringify({ email, password }) },
  );
}

export function apiCreateAnnouncement(
  token: string,
  data: {
    title: string;
    body: string;
    push_preview: string;
    target_classification?: string | null;
    needs_ack?: boolean;
  },
) {
  return request<Announcement>(
    '/api/announcements/',
    { method: 'POST', body: JSON.stringify(data) },
    token,
  );
}

export function apiAIDraft(token: string, note: string) {
  return request<AIDraft>(
    '/api/announcements/ai-draft/',
    { method: 'POST', body: JSON.stringify({ note }) },
    token,
  );
}

export function apiGetAnnouncement(token: string, id: string) {
  return request<Announcement>(`/api/announcements/${id}/`, {}, token);
}

export function apiConfirmAnnouncement(token: string, id: string) {
  return request<Announcement>(
    `/api/announcements/${id}/confirm/`,
    { method: 'POST' },
    token,
  );
}

export function apiSendAnnouncement(token: string, id: string) {
  return request<Announcement>(
    `/api/announcements/${id}/send/`,
    { method: 'POST' },
    token,
  );
}
