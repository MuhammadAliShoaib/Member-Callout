'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { useRouter, useParams } from 'next/navigation';
import {
  apiGetAnnouncement,
  apiUpdateAnnouncement,
  apiConfirmAnnouncement,
  apiSendAnnouncement,
  apiPollAnnouncementStats,
  type Announcement,
  type AnnouncementStats,
} from '@/lib/api';
import { getToken, getMember, clearAuth } from '@/lib/auth';

const STATUS_LABEL: Record<Announcement['status'], string> = {
  draft: 'Draft',
  confirmed: 'Confirmed',
  queued: 'Queued',
  sent: 'Sent',
};

export default function AnnouncementDetailPage() {
  const router = useRouter();
  const { id } = useParams<{ id: string }>();

  const [member, setMember] = useState<ReturnType<typeof getMember>>(null);
  const [announcement, setAnnouncement] = useState<Announcement | null>(null);
  const [loadError, setLoadError] = useState('');

  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [pushPreview, setPushPreview] = useState('');
  const [classification, setClassification] = useState('');
  const [needsAck, setNeedsAck] = useState(false);

  const [saveLoading, setSaveLoading] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [saveNote, setSaveNote] = useState('');
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState('');
  const [stats, setStats] = useState<AnnouncementStats | null>(null);
  const statsEtag = useRef<string | null>(null);

  function populate(a: Announcement) {
    setAnnouncement(a);
    setTitle(a.title);
    setBody(a.body);
    setPushPreview(a.push_preview);
    setClassification(a.target_classification ?? '');
    setNeedsAck(a.needs_ack);
  }

  const load = useCallback(async (token: string) => {
    try {
      populate(await apiGetAnnouncement(token, id));
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Failed to load announcement.');
    }
  }, [id]);

  const loadStats = useCallback(async (token: string) => {
    try {
      const result = await apiPollAnnouncementStats(token, id, statsEtag.current);
      if (result) { statsEtag.current = result.etag; setStats(result.data); }
    } catch { /* stats may not exist yet — silently ignore */ }
  }, [id]);

  useEffect(() => {
    const token = getToken();
    if (!token) { router.replace('/login'); return; }
    setMember(getMember());
    load(token);
  }, [router, load]);

  // Poll announcement + stats while queued; load once when sent.
  useEffect(() => {
    const currentStatus = announcement?.status;
    if (currentStatus !== 'queued' && currentStatus !== 'sent') return;
    const token = getToken();
    if (!token) return;

    loadStats(token);
    if (currentStatus !== 'queued') return;

    const timer = setInterval(async () => {
      const t = getToken();
      if (!t) return;
      try {
        const [a, result] = await Promise.all([
          apiGetAnnouncement(t, id),
          apiPollAnnouncementStats(t, id, statsEtag.current),
        ]);
        setAnnouncement(a);
        setTitle(a.title);
        setBody(a.body);
        setPushPreview(a.push_preview);
        setClassification(a.target_classification ?? '');
        setNeedsAck(a.needs_ack);
        if (result) {
          statsEtag.current = result.etag;
          setStats(result.data);
        }
      } catch { /* silently ignore poll errors */ }
    }, 5000);

    return () => clearInterval(timer);
  }, [announcement?.status, id, loadStats]);

  async function handleSave() {
    const token = getToken();
    if (!token) return;
    setSaveError('');
    setSaveNote('');
    setSaveLoading(true);
    try {
      const previousStatus = announcement?.status;
      const a = await apiUpdateAnnouncement(token, id, {
        title: title.trim(),
        body: body.trim(),
        push_preview: pushPreview.trim(),
        target_classification: classification.trim() || null,
        needs_ack: needsAck,
      });
      populate(a);
      // Backend resets to draft when content fields change on a non-draft announcement.
      // Derive this from the response — never infer it from local edits alone.
      if (previousStatus !== 'draft' && a.status === 'draft') {
        setSaveNote('Content changed — confirmation required before sending.');
      }
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save.');
    } finally {
      setSaveLoading(false);
    }
  }

  async function handleConfirm() {
    const token = getToken();
    if (!token) return;
    setSaveNote('');
    setActionError('');
    setActionLoading(true);
    try {
      populate(await apiConfirmAnnouncement(token, id));
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to confirm.');
    } finally {
      setActionLoading(false);
    }
  }

  async function handleSend() {
    const token = getToken();
    if (!token) return;
    setSaveNote('');
    setActionError('');
    setActionLoading(true);
    try {
      populate(await apiSendAnnouncement(token, id));
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to send.');
    } finally {
      setActionLoading(false);
    }
  }

  return (
    <>
      <header className="header">
        <div className="container header-inner">
          <a href="/announcements" className="header-title">Member Callout</a>
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
        <h1 className="page-title" style={{ marginBottom: 4 }}>Announcement</h1>
        {announcement && (
          <p style={{ marginBottom: 24, color: 'var(--muted)', fontSize: '0.9rem' }}>
            Status: <strong style={{ color: 'var(--text)' }}>{STATUS_LABEL[announcement.status]}</strong>
          </p>
        )}

        {loadError && <p style={{ color: 'var(--danger)' }}>{loadError}</p>}

        {!announcement && !loadError && (
          <p style={{ color: 'var(--muted)' }}>Loading…</p>
        )}

        {announcement && (
          <div className="card">
            <div className="form-group">
              <label className="form-label" htmlFor="title">Title</label>
              <input
                id="title"
                type="text"
                className="form-input"
                value={title}
                onChange={e => setTitle(e.target.value)}
                maxLength={255}
              />
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="body">Body</label>
              <textarea
                id="body"
                className="form-textarea"
                value={body}
                onChange={e => setBody(e.target.value)}
                rows={6}
              />
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="push-preview">Push preview</label>
              <input
                id="push-preview"
                type="text"
                className="form-input"
                value={pushPreview}
                onChange={e => setPushPreview(e.target.value)}
                maxLength={255}
              />
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="classification">
                Target classification
                <span style={{ fontWeight: 400, color: 'var(--muted)' }}> (optional)</span>
              </label>
              <input
                id="classification"
                type="text"
                className="form-input"
                value={classification}
                onChange={e => setClassification(e.target.value)}
                placeholder="Leave blank for all members"
              />
            </div>

            <div className="form-group">
              <label className="form-check">
                <input
                  type="checkbox"
                  checked={needsAck}
                  onChange={e => setNeedsAck(e.target.checked)}
                />
                Needs acknowledgement
              </label>
            </div>

            {(() => {
              const busy = saveLoading || actionLoading;
              return (
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 8, flexWrap: 'wrap' }}>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={handleSave}
                    disabled={busy}
                  >
                    {saveLoading ? 'Saving…' : 'Save changes'}
                  </button>

                  {announcement.status === 'draft' && (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={handleConfirm}
                      disabled={busy}
                    >
                      {actionLoading ? 'Confirming…' : 'Confirm'}
                    </button>
                  )}

                  {announcement.status === 'confirmed' && (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={handleSend}
                      disabled={busy}
                    >
                      {actionLoading ? 'Sending…' : 'Send to members'}
                    </button>
                  )}

                  {(announcement.status === 'queued' || announcement.status === 'sent') && (
                    <span style={{ fontSize: '0.875rem', color: 'var(--muted)' }}>
                      {announcement.status === 'queued' ? 'Delivery in progress' : 'Sent to members'}
                    </span>
                  )}
                </div>
              );
            })()}

            {saveNote && <p style={{ fontSize: '0.875rem', color: 'var(--muted)', marginTop: 12 }}>{saveNote}</p>}
            {saveError && <p style={{ color: 'var(--danger)', fontSize: '0.875rem', marginTop: 12 }}>{saveError}</p>}
            {actionError && <p style={{ color: 'var(--danger)', fontSize: '0.875rem', marginTop: 12 }}>{actionError}</p>}

            {stats && (() => {
              const processed = stats.sent_count + stats.failed_count;
              const pct = stats.target_count > 0
                ? Math.round((processed / stats.target_count) * 100)
                : 0;
              return (
                <div style={{ marginTop: 20, paddingTop: 20, borderTop: '1px solid var(--border)', fontSize: '0.9rem', lineHeight: 2 }}>
                  <p style={{ fontWeight: 600, marginBottom: 4 }}>Delivery</p>
                  <div style={{ marginBottom: 8 }}>
                    <progress value={processed} max={stats.target_count} style={{ width: '100%', height: 8 }} />
                    <p style={{ fontSize: '0.8rem', color: 'var(--muted)', marginTop: 2 }}>
                      {processed.toLocaleString()} / {stats.target_count.toLocaleString()} processed ({pct}%)
                    </p>
                  </div>
                  <p>Target: {stats.target_count.toLocaleString()}</p>
                  <p>Sent: {stats.sent_count.toLocaleString()}</p>
                  <p>Failed: {stats.failed_count.toLocaleString()}</p>
                  <p>Read: {stats.read_count.toLocaleString()}</p>
                  <p>Acknowledged: {stats.acknowledged_count.toLocaleString()}</p>
                </div>
              );
            })()}
          </div>
        )}
      </main>
    </>
  );
}
