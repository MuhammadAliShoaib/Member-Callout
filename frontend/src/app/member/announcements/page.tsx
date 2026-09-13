'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { apiGetMemberAnnouncements, type MemberAnnouncement } from '@/lib/api';
import { getToken, getMember, clearAuth } from '@/lib/auth';

export default function MemberAnnouncementsPage() {
  const router = useRouter();
  const [member] = useState<ReturnType<typeof getMember>>(() => getMember());
  const [announcements, setAnnouncements] = useState<MemberAnnouncement[]>([]);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (token: string) => {
    try {
      setAnnouncements(await apiGetMemberAnnouncements(token));
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Failed to load announcements.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const token = getToken();
    if (!token) { router.replace('/login'); return; }
    const m = getMember();
    if (m?.role === 'leader') { router.replace('/announcements'); return; }
    load(token);
  }, [router, load]);

  return (
    <>
      <header className="header">
        <div className="container header-inner">
          <span className="header-title">Member Callout</span>
          {member && (
            <div className="header-user">
              <span>{member.full_name}</span>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => { clearAuth(); router.replace('/login'); }}
              >
                Log out
              </button>
            </div>
          )}
        </div>
      </header>

      <main className="container page">
        <h1 className="page-title">Announcements</h1>

        {loadError && <p style={{ color: 'var(--danger)' }}>{loadError}</p>}
        {loading && !loadError && <p style={{ color: 'var(--muted)' }}>Loading…</p>}

        {!loading && !loadError && announcements.length === 0 && (
          <p style={{ color: 'var(--muted)' }}>No announcements yet.</p>
        )}

        {announcements.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {announcements.map(a => (
              <Link key={a.id} href={`/member/announcements/${a.id}`} style={{ textDecoration: 'none', color: 'inherit' }}>
              <div className="card" style={{ padding: '16px 20px' }}>
                <p style={{ fontWeight: 600, marginBottom: 4 }}>{a.title}</p>
                <p style={{ fontSize: '0.875rem', color: 'var(--muted)', marginBottom: 8 }}>
                  {new Date(a.sent_at).toLocaleDateString(undefined, {
                    year: 'numeric', month: 'short', day: 'numeric',
                  })}
                </p>
                <div style={{ display: 'flex', gap: 12, fontSize: '0.8125rem', color: 'var(--muted)' }}>
                  <span>{a.is_read ? 'Read' : 'Unread'}</span>
                  {a.is_acknowledged && <span>Acknowledged</span>}
                </div>
              </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </>
  );
}
