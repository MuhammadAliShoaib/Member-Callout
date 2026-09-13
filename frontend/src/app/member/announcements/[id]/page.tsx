'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter, useParams } from 'next/navigation';
import Link from 'next/link';
import {
  apiGetMemberAnnouncement,
  apiAcknowledgeMemberAnnouncement,
  type MemberAnnouncementDetail,
} from '@/lib/api';
import { getToken, getMember, clearAuth } from '@/lib/auth';

export default function MemberAnnouncementDetailPage() {
  const router = useRouter();
  const { id } = useParams<{ id: string }>();

  const [member] = useState<ReturnType<typeof getMember>>(() => getMember());
  const [announcement, setAnnouncement] = useState<MemberAnnouncementDetail | null>(null);
  const [loadError, setLoadError] = useState('');
  const [ackLoading, setAckLoading] = useState(false);
  const [ackError, setAckError] = useState('');

  const load = useCallback(async (token: string) => {
    try {
      setAnnouncement(await apiGetMemberAnnouncement(token, id));
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Failed to load announcement.');
    }
  }, [id]);

  useEffect(() => {
    const token = getToken();
    if (!token) { router.replace('/login'); return; }
    const m = getMember();
    if (m?.role === 'leader') { router.replace('/announcements'); return; }
    const timer = window.setTimeout(() => { void load(token); }, 0);
    return () => window.clearTimeout(timer);
  }, [router, load]);

  async function handleAcknowledge() {
    const token = getToken();
    if (!token) return;
    setAckError('');
    setAckLoading(true);
    try {
      await apiAcknowledgeMemberAnnouncement(token, id);
      setAnnouncement(prev => prev ? { ...prev, is_acknowledged: true } : prev);
    } catch (err) {
      setAckError(err instanceof Error ? err.message : 'Failed to acknowledge.');
    } finally {
      setAckLoading(false);
    }
  }

  return (
    <>
      <header className="header">
        <div className="container header-inner">
          <Link href="/member/announcements" className="header-title">Member Callout</Link>
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
        {loadError && (
          <p style={{ color: 'var(--danger)' }}>{loadError}</p>
        )}

        {!announcement && !loadError && (
          <p style={{ color: 'var(--muted)' }}>Loading…</p>
        )}

        {announcement && (
          <>
            <h1 className="page-title">{announcement.title}</h1>

            <p style={{ fontSize: '0.875rem', color: 'var(--muted)', marginBottom: 24 }}>
              {new Date(announcement.sent_at).toLocaleString(undefined, {
                year: 'numeric', month: 'short', day: 'numeric',
                hour: '2-digit', minute: '2-digit',
              })}
            </p>

            <div className="card" style={{ marginBottom: 20 }}>
              <p style={{ whiteSpace: 'pre-wrap', lineHeight: 1.7 }}>{announcement.body}</p>
            </div>

            {announcement.needs_ack && (
              <div>
                {announcement.is_acknowledged ? (
                  <p style={{ fontSize: '0.9rem', color: 'var(--muted)' }}>Acknowledged</p>
                ) : (
                  <>
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={handleAcknowledge}
                      disabled={ackLoading}
                    >
                      {ackLoading ? 'Acknowledging…' : 'Acknowledge'}
                    </button>
                    {ackError && (
                      <p style={{ color: 'var(--danger)', fontSize: '0.875rem', marginTop: 8 }}>
                        {ackError}
                      </p>
                    )}
                  </>
                )}
              </div>
            )}
          </>
        )}
      </main>
    </>
  );
}
