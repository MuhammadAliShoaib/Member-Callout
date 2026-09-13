'use client';

import { useState, useEffect, FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { apiCreateAnnouncement, apiAIDraft } from '@/lib/api';
import { getToken, getMember, clearAuth } from '@/lib/auth';

export default function NewAnnouncementPage() {
  const router = useRouter();
  const [member, setMember] = useState<ReturnType<typeof getMember>>(null);

  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [pushPreview, setPushPreview] = useState('');
  const [targetClassification, setTargetClassification] = useState('');
  const [needsAck, setNeedsAck] = useState(false);

  const [aiNote, setAiNote] = useState('');
  const [showAi, setShowAi] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState('');

  const [submitLoading, setSubmitLoading] = useState(false);
  const [submitError, setSubmitError] = useState('');

  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace('/login');
      return;
    }
    setMember(getMember());
  }, [router]);

  function handleLogout() {
    clearAuth();
    router.replace('/login');
  }

  async function handleAIDraft() {
    if (!aiNote.trim()) return;
    const token = getToken();
    if (!token) return;
    setAiError('');
    setAiLoading(true);
    try {
      const draft = await apiAIDraft(token, aiNote.trim());
      setTitle(draft.title);
      setBody(draft.body);
      setPushPreview(draft.push_preview);
      setShowAi(false);
      setAiNote('');
    } catch (err) {
      setAiError(err instanceof Error ? err.message : 'AI draft failed.');
    } finally {
      setAiLoading(false);
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const token = getToken();
    if (!token) return;
    setSubmitError('');
    setSubmitLoading(true);
    try {
      const announcement = await apiCreateAnnouncement(token, {
        title: title.trim(),
        body: body.trim(),
        push_preview: pushPreview.trim(),
        target_classification: targetClassification.trim() || null,
        needs_ack: needsAck,
      });
      router.push(`/announcements/${announcement.id}`);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : 'Failed to create announcement.');
    } finally {
      setSubmitLoading(false);
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
              <button className="btn btn-ghost btn-sm" onClick={handleLogout}>
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>

      <main className="container page">
        <h1 className="page-title">New Announcement</h1>

        {showAi ? (
          <div className="ai-section">
            <p className="ai-section-title">Draft with AI</p>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label" htmlFor="ai-note">
                Describe what you want to announce
              </label>
              <textarea
                id="ai-note"
                className="form-textarea"
                value={aiNote}
                onChange={e => setAiNote(e.target.value)}
                placeholder="e.g. Remind members about the general meeting this Thursday at 7pm in the union hall."
                rows={3}
              />
            </div>
            {aiError && <p className="error-msg">{aiError}</p>}
            <div className="ai-actions">
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={handleAIDraft}
                disabled={aiLoading || !aiNote.trim()}
              >
                {aiLoading ? <span className="spinner" /> : null}
                {aiLoading ? 'Generating…' : 'Generate draft'}
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => { setShowAi(false); setAiNote(''); setAiError(''); }}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div style={{ marginBottom: '24px' }}>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => setShowAi(true)}
            >
              ✦ Draft with AI
            </button>
          </div>
        )}

        <div className="card">
          <form onSubmit={handleSubmit}>
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
                placeholder="Announcement title"
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
                placeholder="Full announcement text"
                rows={6}
              />
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="push-preview">Push notification preview</label>
              <input
                id="push-preview"
                type="text"
                className="form-input"
                value={pushPreview}
                onChange={e => setPushPreview(e.target.value)}
                required
                maxLength={255}
                placeholder="Short preview text for push notifications"
              />
              <p className="form-help">Max 255 characters. Keep it concise.</p>
            </div>

            <div className="form-group">
              <label className="form-label" htmlFor="classification">
                Target classification <span style={{ fontWeight: 400, color: 'var(--muted)' }}>(optional)</span>
              </label>
              <input
                id="classification"
                type="text"
                className="form-input"
                value={targetClassification}
                onChange={e => setTargetClassification(e.target.value)}
                placeholder="Leave blank to send to all members"
              />
              <p className="form-help">Enter a specific job classification, or leave blank for all active members.</p>
            </div>

            <div className="form-group">
              <label className="form-check">
                <input
                  type="checkbox"
                  checked={needsAck}
                  onChange={e => setNeedsAck(e.target.checked)}
                />
                Require member acknowledgement
              </label>
            </div>

            {submitError && <p className="error-msg">{submitError}</p>}

            <div style={{ marginTop: '8px' }}>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={submitLoading}
              >
                {submitLoading ? <span className="spinner" /> : null}
                {submitLoading ? 'Creating…' : 'Create announcement'}
              </button>
            </div>
          </form>
        </div>
      </main>
    </>
  );
}
