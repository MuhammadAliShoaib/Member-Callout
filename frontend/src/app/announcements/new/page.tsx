'use client';

import { useState, useEffect, useRef, FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { apiCreateAnnouncement, apiAIRegenerate } from '@/lib/api';
import { getToken, getMember, clearAuth } from '@/lib/auth';

export default function NewAnnouncementPage() {
  const router = useRouter();
  const [member] = useState<ReturnType<typeof getMember>>(() => getMember());

  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [pushPreview, setPushPreview] = useState('');
  const [targetClassification, setTargetClassification] = useState('');
  const [needsAck, setNeedsAck] = useState(false);

  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState('');
  const [aiSuggestion, setAiSuggestion] = useState('');
  const [aiNotice, setAiNotice] = useState('');
  const latestAIRequestId = useRef('');
  const bodyRef = useRef('');

  const [submitLoading, setSubmitLoading] = useState(false);
  const [submitError, setSubmitError] = useState('');

  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace('/login');
      return;
    }
  }, [router]);

  function handleLogout() {
    clearAuth();
    router.replace('/login');
  }

  async function handleAIRegenerate() {
    if (!body.trim()) return;
    const token = getToken();
    if (!token) return;
    const requestSourceText = body;
    const clientRequestId = crypto.randomUUID();
    latestAIRequestId.current = clientRequestId;
    setAiError('');
    setAiSuggestion('');
    setAiNotice('');
    setAiLoading(true);
    try {
      const result = await apiAIRegenerate(token, requestSourceText, undefined, clientRequestId);
      if (clientRequestId !== latestAIRequestId.current) return;
      if (bodyRef.current !== requestSourceText) {
        setAiNotice('The announcement changed while AI was generating this suggestion.');
      }
      setAiSuggestion(result.generated_text);
    } catch (err) {
      if (clientRequestId !== latestAIRequestId.current) return;
      setAiError(err instanceof Error ? err.message : 'AI regeneration failed.');
    } finally {
      if (clientRequestId === latestAIRequestId.current) setAiLoading(false);
    }
  }

  function applyAiSuggestion() {
    if (!aiSuggestion) return;
    setBody(aiSuggestion);
    bodyRef.current = aiSuggestion;
    setAiSuggestion('');
    setAiNotice('');
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
                onChange={e => { setBody(e.target.value); bodyRef.current = e.target.value; }}
                required
                placeholder="Full announcement text"
                rows={6}
              />
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={handleAIRegenerate}
                disabled={aiLoading || !body.trim()}
                style={{ marginTop: 8 }}
              >
                {aiLoading ? 'Generating...' : 'Regenerate with AI'}
              </button>
              {aiError && <p className="error-msg">{aiError}</p>}
              {aiSuggestion && (
                <div style={{ marginTop: 10, padding: 12, border: '1px solid var(--border)', borderRadius: 'var(--radius)' }}>
                  <p style={{ fontWeight: 600, marginBottom: 6 }}>AI suggestion</p>
                  {aiNotice && <p style={{ color: 'var(--muted)', fontSize: '0.875rem', marginBottom: 8 }}>{aiNotice}</p>}
                  <p style={{ whiteSpace: 'pre-wrap', marginBottom: 10 }}>{aiSuggestion}</p>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <button type="button" className="btn btn-primary btn-sm" onClick={applyAiSuggestion}>
                      Use this version
                    </button>
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setAiSuggestion(''); setAiNotice(''); }}>
                      Discard
                    </button>
                  </div>
                </div>
              )}
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
