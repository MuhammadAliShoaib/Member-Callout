'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import {
  apiListAnnouncements,
  apiCreateAnnouncement,
  apiAIRegenerate,
  apiConfirmAnnouncement,
  apiGetAnnouncementStats,
  apiSendAnnouncement,
  apiGetAnnouncement,
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

function announcementDate(a: Announcement): string {
  const date = a.status === 'sent' && a.sent_at ? a.sent_at : a.created_at;
  const label = a.status === 'sent' && a.sent_at ? 'Sent' : 'Created';
  return `${label} ${new Date(date).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}`;
}

export default function AnnouncementsPage() {
  const router = useRouter();
  const [member] = useState<ReturnType<typeof getMember>>(() => getMember());

  const [list, setList] = useState<Announcement[]>([]);
  const [listError, setListError] = useState('');

  const [panel, setPanel] = useState<Announcement | null>(null);
  const [panelStats, setPanelStats] = useState<AnnouncementStats | null>(null);
  const [panelStatsLoading, setPanelStatsLoading] = useState(false);
  const [panelStatsError, setPanelStatsError] = useState('');
  const [panelSendLoading, setPanelSendLoading] = useState(false);
  const [panelSendError, setPanelSendError] = useState('');

  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [pushPreview, setPushPreview] = useState('');
  const [classification, setClassification] = useState('');
  const [needsAck, setNeedsAck] = useState(false);

  const [aiNote, setAiNote] = useState('');
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState('');
  const [aiSuggestion, setAiSuggestion] = useState('');
  const aiGeneration = useRef(0);
  const aiNoteRef = useRef<string>('');

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<Pick<Announcement, 'id' | 'status'> | null>(null);

  const [confirmLoading, setConfirmLoading] = useState(false);
  const [confirmError, setConfirmError] = useState('');
  const [confirmSuccess, setConfirmSuccess] = useState(false);

  const loadList = useCallback(async (token: string) => {
    try {
      setList(await apiListAnnouncements(token));
    } catch (err) {
      setListError(err instanceof Error ? err.message : 'Failed to load announcements.');
    }
  }, []);

  useEffect(() => {
    const token = getToken();
    if (!token) { router.replace('/login'); return; }
    loadList(token);
  }, [router, loadList]);

  async function loadPanelStats(id: string) {
    const token = getToken();
    if (!token) return;
    setPanelStatsLoading(true);
    setPanelStatsError('');
    try {
      setPanelStats(await apiGetAnnouncementStats(token, id));
    } catch (err) {
      setPanelStatsError(err instanceof Error ? err.message : 'Failed to load stats.');
    } finally {
      setPanelStatsLoading(false);
    }
  }

  function openPanel(a: Announcement) {
    setPanel(a);
    setPanelStats(null);
    setPanelStatsError('');
    if (a.status === 'queued' || a.status === 'sent') {
      loadPanelStats(a.id);
    }
  }

  function closePanel() {
    setPanel(null);
    setPanelStats(null);
    setPanelStatsError('');
    setPanelSendError('');
  }

  async function handlePanelSend() {
    if (!panel) return;
    const token = getToken();
    if (!token) return;
    setPanelSendError('');
    setPanelSendLoading(true);
    try {
      const updated = await apiSendAnnouncement(token, panel.id);
      setPanel(updated);
      setList(prev => prev.map(a => a.id === updated.id ? updated : a));
      loadPanelStats(updated.id);
    } catch (err) {
      setPanelSendError(err instanceof Error ? err.message : 'Failed to send.');
    } finally {
      setPanelSendLoading(false);
    }
  }

  // Auto-poll stats while delivery is in progress.
  // Stops when sent + failed reaches the target, then refreshes announcement to get final status.
  useEffect(() => {
    if (panel?.status !== 'queued') return;
    const announcementId = panel.id;

    const intervalId = setInterval(async () => {
      const token = getToken();
      if (!token) return;
      try {
        const stats = await apiGetAnnouncementStats(token, announcementId);
        setPanelStats(stats);
        if (stats.target_count > 0 && stats.sent_count + stats.failed_count >= stats.target_count) {
          clearInterval(intervalId);
          const updated = await apiGetAnnouncement(token, announcementId);
          setPanel(updated);
          setList(prev => prev.map(a => a.id === updated.id ? updated : a));
        }
      } catch { /* silently ignore poll errors */ }
    }, 3000);

    return () => clearInterval(intervalId);
  }, [panel?.id, panel?.status]);

  async function handleAIDraft() {
    const token = getToken();
    if (!token || !aiNote.trim()) return;
    setAiError('');
    setAiSuggestion('');
    setAiLoading(true);
    const id = ++aiGeneration.current;
    const clientRequestId = crypto.randomUUID();
    const noteAtRequestTime = aiNoteRef.current;
    try {
      const result = await apiAIRegenerate(token, aiNote.trim(), undefined, clientRequestId);
      if (id !== aiGeneration.current) return;
      if (result.client_request_id !== clientRequestId) return;
      if (aiNoteRef.current !== noteAtRequestTime) {
        setAiError('AI suggestion discarded because the note changed while it was being generated.');
        return;
      }
      setAiSuggestion(result.generated_text);
    } catch (err) {
      if (id !== aiGeneration.current) return;
      setAiError(err instanceof Error ? err.message : 'AI draft failed.');
    } finally {
      if (id === aiGeneration.current) setAiLoading(false);
    }
  }

  function applyAiSuggestion() {
    if (!aiSuggestion) return;
    setBody(aiSuggestion);
    setAiSuggestion('');
  }

  async function handleConfirm() {
    if (!result) return;
    const token = getToken();
    if (!token) return;
    setConfirmError('');
    setConfirmLoading(true);
    try {
      const a = await apiConfirmAnnouncement(token, result.id);
      setResult({ id: a.id, status: a.status });
      setConfirmSuccess(true);
    } catch (err) {
      setConfirmError(err instanceof Error ? err.message : 'Failed to confirm.');
    } finally {
      setConfirmLoading(false);
    }
  }

  async function handleSubmit() {
    const token = getToken();
    if (!token) return;
    setError('');
    setResult(null);
    setConfirmError('');
    setConfirmSuccess(false);
    setLoading(true);
    try {
      const a = await apiCreateAnnouncement(token, {
        title: title.trim(),
        body: body.trim(),
        push_preview: pushPreview.trim(),
        target_classification: classification.trim() || null,
        needs_ack: needsAck,
      });
      setResult({ id: a.id, status: a.status });
      loadList(token);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create announcement.');
    } finally {
      setLoading(false);
    }
  }

  const hasStats = panel?.status === 'queued' || panel?.status === 'sent';
  const deliveryCompleted = panelStats ? panelStats.sent_count + panelStats.failed_count : 0;
  const deliveryRemaining = panelStats ? Math.max(0, panelStats.target_count - deliveryCompleted) : 0;
  const deliveryPct = panelStats && panelStats.target_count > 0
    ? (deliveryCompleted / panelStats.target_count * 100)
    : 0;

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

        {/* List */}
        {listError && <p style={{ color: 'var(--danger)', marginBottom: 16 }}>{listError}</p>}
        {list.length > 0 && (
          <div className="card" style={{ marginBottom: 24, padding: 0 }}>
            {list.map((a, i) => (
              <div
                key={a.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 16,
                  padding: '14px 20px',
                  borderBottom: i < list.length - 1 ? '1px solid var(--border)' : undefined,
                  flexWrap: 'wrap',
                  background: panel?.id === a.id ? 'var(--bg)' : undefined,
                }}
              >
                <span style={{ flex: 1, fontWeight: 500, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {a.title}
                </span>
                <span style={{ fontSize: '0.875rem', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                  {STATUS_LABEL[a.status]}
                </span>
                <span style={{ fontSize: '0.875rem', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                  {announcementDate(a)}
                </span>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => panel?.id === a.id ? closePanel() : openPanel(a)}
                >
                  {panel?.id === a.id ? 'Close' : 'View Details'}
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Details panel */}
        {panel && (
          <div className="card" style={{ marginBottom: 32 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 20, gap: 16 }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 600 }}>{panel.title}</h2>
              <button type="button" className="btn btn-ghost btn-sm" onClick={closePanel} style={{ flexShrink: 0 }}>
                Close
              </button>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 14, fontSize: '0.9rem' }}>
              <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
                <span><strong>Status:</strong> {STATUS_LABEL[panel.status]}</span>
                <span><strong>{panel.status === 'sent' && panel.sent_at ? 'Sent' : 'Created'}:</strong>{' '}
                  {new Date(panel.status === 'sent' && panel.sent_at ? panel.sent_at : panel.created_at)
                    .toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                </span>
                <span><strong>Classification:</strong> {panel.target_classification || 'All members'}</span>
                <span><strong>Needs acknowledgement:</strong> {panel.needs_ack ? 'Yes' : 'No'}</span>
              </div>

              <div>
                <p style={{ fontWeight: 600, marginBottom: 4 }}>Push preview</p>
                <p style={{ color: 'var(--muted)' }}>{panel.push_preview}</p>
              </div>

              <div>
                <p style={{ fontWeight: 600, marginBottom: 4 }}>Body</p>
                <p style={{ whiteSpace: 'pre-wrap', lineHeight: 1.7 }}>{panel.body}</p>
              </div>

              {panel.status === 'confirmed' && (
                <div style={{ paddingTop: 14, borderTop: '1px solid var(--border)' }}>
                  <p style={{ marginBottom: 12 }}>Ready to send</p>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    onClick={handlePanelSend}
                    disabled={panelSendLoading}
                  >
                    {panelSendLoading ? 'Sending…' : 'Send Announcement'}
                  </button>
                  {panelSendError && (
                    <p style={{ color: 'var(--danger)', fontSize: '0.875rem', marginTop: 8 }}>{panelSendError}</p>
                  )}
                </div>
              )}

              {hasStats && (
                <div style={{ paddingTop: 14, borderTop: '1px solid var(--border)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                    <p style={{ fontWeight: 600 }}>
                      {panel.status === 'sent' ? 'Delivery Complete' : 'Delivery Progress'}
                    </p>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => loadPanelStats(panel.id)}
                      disabled={panelStatsLoading}
                    >
                      {panelStatsLoading ? 'Loading…' : 'Refresh Stats'}
                    </button>
                  </div>
                  {panelStatsError && <p style={{ color: 'var(--danger)', fontSize: '0.875rem' }}>{panelStatsError}</p>}
                  {panelStats && (
                    <div style={{ fontSize: '0.9rem' }}>
                      <progress
                        value={deliveryCompleted}
                        max={panelStats.target_count}
                        style={{ width: '100%', marginBottom: 12 }}
                      />
                      <div style={{ lineHeight: 2 }}>
                        <p>Target: {panelStats.target_count.toLocaleString()}</p>
                        <p>Sent: {panelStats.sent_count.toLocaleString()}</p>
                        <p>Failed: {panelStats.failed_count.toLocaleString()}</p>
                        <p>Remaining: {deliveryRemaining.toLocaleString()}</p>
                        <p>Progress: {deliveryPct.toFixed(1)}%</p>
                        <p>Read: {panelStats.read_count.toLocaleString()}</p>
                        <p>Acknowledged: {panelStats.acknowledged_count.toLocaleString()}</p>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}

        {/* New announcement */}
        <h2 style={{ fontSize: '1.1rem', fontWeight: 600, marginBottom: 16 }}>New Announcement</h2>
        <div className="card">
          <div style={{ marginBottom: 24, paddingBottom: 24, borderBottom: '1px solid var(--border)' }}>
            <p style={{ fontWeight: 600, marginBottom: 10 }}>Improve with AI</p>
            <div className="form-group" style={{ marginBottom: 10 }}>
              <label className="form-label" htmlFor="ai-note">Rough note</label>
              <textarea
                id="ai-note"
                className="form-textarea"
                value={aiNote}
                onChange={e => { setAiNote(e.target.value); aiNoteRef.current = e.target.value; }}
                rows={3}
                placeholder="Jot down what you want to say — the AI will clean it up into a proper announcement."
              />
            </div>
            {aiError && <p style={{ color: 'var(--danger)', fontSize: '0.875rem', marginBottom: 8 }}>{aiError}</p>}
            {aiSuggestion && (
              <div style={{ marginBottom: 8, padding: '10px 12px', background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', fontSize: '0.875rem' }}>
                <p style={{ fontWeight: 600, marginBottom: 6 }}>AI suggestion</p>
                <p style={{ whiteSpace: 'pre-wrap', marginBottom: 8 }}>{aiSuggestion}</p>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button type="button" className="btn btn-primary btn-sm" onClick={applyAiSuggestion}>Apply to body</button>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => setAiSuggestion('')}>Discard</button>
                </div>
              </div>
            )}
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={handleAIDraft}
              disabled={aiLoading || !aiNote.trim()}
            >
              {aiLoading ? 'Generating…' : 'Improve with AI'}
            </button>
          </div>

          <form onSubmit={e => { e.preventDefault(); handleSubmit(); }}>
            <div className="form-group">
              <label className="form-label" htmlFor="title">Title</label>
              <input
                id="title"
                type="text"
                className="form-input"
                value={title}
                onChange={e => setTitle(e.target.value)}
                required
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
                required
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
                required
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

            {/* busy: AI loading intentionally excluded — AI generation must not block Create */}
            <button
              type="submit"
              className="btn btn-primary"
              disabled={loading || confirmLoading}
              style={{ marginTop: 8 }}
            >
              {loading ? 'Creating…' : 'Create Draft'}
            </button>
          </form>

          {error && (
            <p style={{ color: 'var(--danger)', fontSize: '0.875rem', marginTop: 16 }}>{error}</p>
          )}

          {result && (
            <div style={{ marginTop: 16, fontSize: '0.9rem', display: 'flex', flexDirection: 'column', gap: 6 }}>
              <p>
                <strong>Status:</strong> {result.status} —{' '}
                <a href={`/announcements/${result.id}`} style={{ color: 'var(--primary)' }}>Edit</a>
              </p>
              {confirmSuccess && (
                <p style={{ color: '#16a34a' }}>Content confirmed</p>
              )}
              {!confirmSuccess && result.status === 'draft' && (
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={handleConfirm}
                  disabled={confirmLoading || loading}
                  style={{ alignSelf: 'flex-start', marginTop: 4 }}
                >
                  {confirmLoading ? 'Confirming…' : 'Confirm'}
                </button>
              )}
              {confirmError && (
                <p style={{ color: 'var(--danger)' }}>{confirmError}</p>
              )}
            </div>
          )}
        </div>
      </main>
    </>
  );
}
