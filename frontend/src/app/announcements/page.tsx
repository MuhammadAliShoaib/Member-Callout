'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { apiCreateAnnouncement, apiAIDraft, apiConfirmAnnouncement, type Announcement, type AIDraft } from '@/lib/api';
import { getToken, getMember, clearAuth } from '@/lib/auth';

export default function AnnouncementsPage() {
  const router = useRouter();
  const [member, setMember] = useState<ReturnType<typeof getMember>>(null);

  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [pushPreview, setPushPreview] = useState('');
  const [classification, setClassification] = useState('');
  const [needsAck, setNeedsAck] = useState(false);

  const [aiNote, setAiNote] = useState('');
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState('');
  const [aiPending, setAiPending] = useState<AIDraft | null>(null);
  const aiGeneration = useRef(0);
  const editVersion = useRef(0);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<Pick<Announcement, 'id' | 'status'> | null>(null);

  const [confirmLoading, setConfirmLoading] = useState(false);
  const [confirmError, setConfirmError] = useState('');
  const [confirmSuccess, setConfirmSuccess] = useState(false);

  async function handleAIDraft() {
    const token = getToken();
    if (!token || !aiNote.trim()) return;
    setAiError('');
    setAiPending(null);
    setAiLoading(true);
    const id = ++aiGeneration.current;
    const versionAtStart = editVersion.current;
    try {
      const draft = await apiAIDraft(token, aiNote.trim());
      if (id !== aiGeneration.current) return;
      if (editVersion.current !== versionAtStart) {
        // User edited the fields while AI was generating — require explicit apply
        setAiPending(draft);
      } else {
        setTitle(draft.title);
        setBody(draft.body);
        setPushPreview(draft.push_preview);
      }
    } catch (err) {
      if (id !== aiGeneration.current) return;
      setAiError(err instanceof Error ? err.message : 'AI draft failed.');
    } finally {
      if (id === aiGeneration.current) setAiLoading(false);
    }
  }

  function applyAiPending() {
    if (!aiPending) return;
    setTitle(aiPending.title);
    setBody(aiPending.body);
    setPushPreview(aiPending.push_preview);
    setAiPending(null);
  }

  useEffect(() => {
    const token = getToken();
    if (!token) { router.replace('/login'); return; }
    setMember(getMember());
  }, [router]);

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
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create announcement.');
    } finally {
      setLoading(false);
    }
  }

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
        <h1 className="page-title">New Announcement</h1>

        <div className="card">
          <div style={{ marginBottom: 24, paddingBottom: 24, borderBottom: '1px solid var(--border)' }}>
            <p style={{ fontWeight: 600, marginBottom: 10 }}>Improve with AI</p>
            <div className="form-group" style={{ marginBottom: 10 }}>
              <label className="form-label" htmlFor="ai-note">Rough note</label>
              <textarea
                id="ai-note"
                className="form-textarea"
                value={aiNote}
                onChange={e => setAiNote(e.target.value)}
                rows={3}
                placeholder="Jot down what you want to say — the AI will clean it up into a proper announcement."
              />
            </div>
            {aiError && <p style={{ color: 'var(--danger)', fontSize: '0.875rem', marginBottom: 8 }}>{aiError}</p>}
            {aiPending && (
              <div style={{ marginBottom: 8, padding: '10px 12px', background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', fontSize: '0.875rem' }}>
                <p style={{ marginBottom: 8 }}>AI result is ready, but you edited the form. Apply it now or discard.</p>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button type="button" className="btn btn-primary btn-sm" onClick={applyAiPending}>Apply</button>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => setAiPending(null)}>Discard</button>
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
                onChange={e => { editVersion.current++; setTitle(e.target.value); }}
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
                onChange={e => { editVersion.current++; setBody(e.target.value); }}
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
                onChange={e => { editVersion.current++; setPushPreview(e.target.value); }}
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
              <p><strong>ID:</strong> <code>{result.id}</code></p>
              <p><strong>Status:</strong> {result.status}</p>
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
