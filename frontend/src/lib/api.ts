const BASE = process.env.NEXT_PUBLIC_API_URL ?? '';

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Token ${token}`;

  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  const data = await res.json().catch(() => null) as { detail?: string } | null;

  if (!res.ok) {
    throw new Error(data?.detail ?? res.statusText);
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
  content_editable: boolean;
  confirmed_content_hash: string;
  created_at: string;
  confirmed_at: string | null;
  queued_at: string | null;
  sent_at: string | null;
}

export interface AIDraft {
  title: string;
  body: string;
  push_preview: string;
}

export interface AIRegeneration {
  generated_text: string;
}

export interface AnnouncementStats {
  target_count: number;
  sent_count: number;
  failed_count: number;
  read_count: number;
  acknowledged_count: number;
  coming_count: number;
  cant_come_count: number;
  updated_at: string;
}

export function apiLogin(email: string, password: string) {
  return request<{ token: string; token_type: string; member: Member }>(
    '/api/login/',
    { method: 'POST', body: JSON.stringify({ email, password }) },
  );
}

export function apiListAnnouncements(token: string) {
  return request<Announcement[]>('/api/announcements/', {}, token);
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

export function apiAIRegenerate(token: string, text: string, instruction?: string, clientRequestId?: string) {
  return request<AIRegeneration>(
    '/api/announcements/ai/regenerate/',
    {
      method: 'POST',
      body: JSON.stringify({
        text,
        instruction: instruction?.trim() || undefined,
        client_request_id: clientRequestId,
      }),
    },
    token,
  );
}

export function apiGetAnnouncement(token: string, id: string) {
  return request<Announcement>(`/api/announcements/${id}/`, {}, token);
}

export function apiUpdateAnnouncement(
  token: string,
  id: string,
  data: {
    title?: string;
    body?: string;
    push_preview?: string;
    target_classification?: string | null;
    needs_ack?: boolean;
  },
) {
  return request<Announcement>(
    `/api/announcements/${id}/`,
    { method: 'PATCH', body: JSON.stringify(data) },
    token,
  );
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

export interface MemberAnnouncement {
  id: string;
  title: string;
  sent_at: string;
  is_read: boolean;
  is_acknowledged: boolean;
}

export function apiGetMemberAnnouncements(token: string) {
  return request<MemberAnnouncement[]>('/api/member/announcements/', {}, token);
}

export interface MemberAnnouncementDetail {
  id: string;
  title: string;
  body: string;
  needs_ack: boolean;
  sent_at: string;
  is_acknowledged: boolean;
}

export function apiGetMemberAnnouncement(token: string, id: string) {
  return request<MemberAnnouncementDetail>(`/api/member/announcements/${id}/`, {}, token);
}

export function apiAcknowledgeMemberAnnouncement(token: string, id: string) {
  return request<{ is_acknowledged: boolean }>(`/api/member/announcements/${id}/acknowledge/`, { method: 'POST' }, token);
}

export function apiGetAnnouncementStats(token: string, id: string) {
  return request<AnnouncementStats>(`/api/announcements/${id}/stats/`, {}, token);
}

export async function apiPollAnnouncementStats(
  token: string,
  id: string,
  ifNoneMatch: string | null,
): Promise<{ data: AnnouncementStats; etag: string } | null> {
  const headers: Record<string, string> = { Authorization: `Token ${token}` };
  if (ifNoneMatch) headers['If-None-Match'] = ifNoneMatch;
  const res = await fetch(`${BASE}/api/announcements/${id}/stats/`, { headers });
  if (res.status === 304) return null;
  const data = await res.json().catch(() => null) as AnnouncementStats | { detail?: string } | null;
  if (!res.ok) throw new Error((data as { detail?: string } | null)?.detail ?? res.statusText);
  return { data: data as AnnouncementStats, etag: res.headers.get('ETag') ?? '' };
}
