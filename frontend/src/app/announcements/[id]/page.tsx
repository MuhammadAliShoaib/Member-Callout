'use client';

import { useState, useEffect, useCallback } from 'react';
import { useRouter, useParams } from 'next/navigation';
import type { Announcement } from '@/lib/api';
import { apiGetAnnouncement, apiConfirmAnnouncement, apiSendAnnouncement } from '@/lib/api';
import { getToken, getMember, clearAuth } from '@/lib/auth';

const STATUS_LABELS: Record<Announcement['status'], string> = {
  draft: 'Draft',
  confirmed: 'Confirmed',
  queued: 'Queued',
  sent: 'Sent',
};

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

export default function AnnouncementDetailPage() {
  const router = useRouter();
  const params = useParams();
  const id = params.id as string;

  const [member, setMember] = useState<ReturnType<typeof getMember>>(null);
  const [announcement, setAnnouncement] = useState<Announcement | null>(null);
  const [loadError, setLoadError] = useState('');
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState('');

  const loadAnnouncement = useCallback(async (token: string) => {
    try {
      const data = await apiGetAnnouncement(token, id);
      setAnnouncement(data);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Failed to load announcement.');
    }
  }, [id]);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace('/login');
      return;
    }
    setMember(getMember());
    loadAnnouncement(token);
  }, [router, loadAnnouncement]);

  function handleLogout() {
    clearAuth();
    router.replace('/login');
  }

  async function handleConfirm() {
    const token = getToken();
    if (!token || !announcement) return;
    setActionError('');
    setActionLoading(true);
    try {
      const updated = await apiConfirmAnnouncement(token, announcement.id);
      setAnnouncement(updated);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to confirm announcement.');
    } finally {
      setActionLoading(false);
    }
  }

  async function handleSend() {
    const token = getToken();
    if (!token || !announcement) return;
    setActionError('');
    setActionLoading(true);
    try {
      const updated = await apiSendAnnouncement(token, announcement.id);
      setAnnouncement(updated);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to send announcement.');
    } finally {
      setActionLoading(false);
    }
  }

  return (
    <>
      <header className="header">
        <div className="container header-inner">
          <a href="/announcements/new" className="header-title">Member Callout</a>
          {member && (
            <div className="header-user">
              <span>{member.full_name}</span>
              <button className="btn btn-ghost btn-sm" onClick={handleLogout}>
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>

      <main className="container page">
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '24px', flexWrap: 'wrap' }}>
          <h1 className="page-title" style={{ marginBottom: 0 }}>Announcement</h1>
          {announcement && (
            <span className={`badge badge-${announcement.status}`}>
              {STATUS_LABELS[announcement.status]}
            </span>
          )}
        </div>

        {loadError && (
          <div className="alert alert-error">{loadError}</div>
        )}

        {!announcement && !loadError && (
          <div style={{ color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span className="spinner" /> Loading…
          </div>
        )}

        {announcement && (
          <div className="card">
            <div className="detail-grid">
              <div className="detail-field">
                <p className="detail-label">Title</p>
                <p className="detail-value" style={{ fontWeight: 600, fontSize: '1.125rem' }}>
                  {announcement.title}
                </p>
              </div>

              <div className="detail-field">
                <p className="detail-label">Body</p>
                <p className="detail-value">{announcement.body}</p>
              </div>

              <div className="detail-field">
                <p className="detail-label">Push preview</p>
                <p className="detail-value detail-meta">{announcement.push_preview}</p>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px' }}>
                <div className="detail-field">
                  <p className="detail-label">Target</p>
                  <p className="detail-value">
                    {announcement.target_classification ?? 'All members'}
                  </p>
                </div>
                <div className="detail-field">
                  <p className="detail-label">Requires acknowledgement</p>
                  <p className="detail-value">{announcement.needs_ack ? 'Yes' : 'No'}</p>
                </div>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '20px' }}>
                <div className="detail-field">
                  <p className="detail-label">Created</p>
                  <p className="detail-value detail-meta">{formatDate(announcement.created_at)}</p>
                </div>
                <div className="detail-field">
                  <p className="detail-label">Confirmed</p>
                  <p className="detail-value detail-meta">{formatDate(announcement.confirmed_at)}</p>
                </div>
                <div className="detail-field">
                  <p className="detail-label">Queued</p>
                  <p className="detail-value detail-meta">{formatDate(announcement.queued_at)}</p>
                </div>
              </div>
            </div>

            <div className="action-bar">
              {announcement.status === 'draft' && (
                <button
                  className="btn btn-primary"
                  onClick={handleConfirm}
                  disabled={actionLoading}
                >
                  {actionLoading ? <span className="spinner" /> : null}
                  {actionLoading ? 'Confirming…' : 'Confirm announcement'}
                </button>
              )}

              {announcement.status === 'confirmed' && (
                <button
                  className="btn btn-primary"
                  onClick={handleSend}
                  disabled={actionLoading}
                >
                  {actionLoading ? <span className="spinner" /> : null}
                  {actionLoading ? 'Sending…' : 'Send to members'}
                </button>
              )}

              {announcement.status === 'queued' && (
                <p className="detail-meta">
                  Announcement is queued for delivery. Members will receive it shortly.
                </p>
              )}

              {announcement.status === 'sent' && (
                <p className="detail-meta">
                  Announcement has been sent to all eligible members.
                </p>
              )}

              <a href="/announcements/new" className="btn btn-secondary">
                New announcement
              </a>
            </div>

            {actionError && <p className="error-msg" style={{ marginTop: '12px' }}>{actionError}</p>}
          </div>
        )}
      </main>
    </>
  );
}
