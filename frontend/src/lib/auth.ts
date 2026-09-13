const TOKEN_KEY = 'callout_token';
const MEMBER_KEY = 'callout_member';

export interface Member {
  id: string;
  local_id: string;
  full_name: string;
  email: string;
  classification: string;
  status: string;
  role: string;
}

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function getMember(): Member | null {
  if (typeof window === 'undefined') return null;
  const raw = localStorage.getItem(MEMBER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Member;
  } catch {
    return null;
  }
}

export function setAuth(token: string, member: Member): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(MEMBER_KEY, JSON.stringify(member));
}

export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(MEMBER_KEY);
}
