import React, { useState, useRef, useEffect, useCallback } from 'react';
import Header from './components/Header';
import PreviewPane from './components/PreviewPane';
import TerminalPane from './components/TerminalPane';
import LandingPage from './components/LandingPage';
import PasswordGate from './components/PasswordGate';
import {
  Message, Sender, ChatSession, AgentEvent, FileMap,
  CloneRecord, PipelineStep,
} from './types';
import { supabase } from './lib/supabase';
import type { User } from '@supabase/supabase-js';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

function ts(): string {
  return new Date().toLocaleTimeString('en-US', {
    hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

const App: React.FC = () => {
  // ── Auth ──────────────────────────────────────────────────────────────
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [isAuthenticated, setIsAuthenticated] = useState(
    () => sessionStorage.getItem('aq_authenticated') === 'true'
  );

  // ── View ──────────────────────────────────────────────────────────────
  const [showLanding, setShowLanding] = useState(true);
  const [viewMode, setViewMode] = useState<'split' | 'editor' | 'preview'>('split');

  // ── Clone state ───────────────────────────────────────────────────────
  const [url, setUrl] = useState('');
  const outputFormat = 'react' as const;
  const [isCloning, setIsCloning] = useState(false);
  const [cloneId, setCloneId] = useState('');
  const [previewUrl, setPreviewUrl] = useState('');
  const [files, setFiles] = useState<FileMap>({});
  const [activeFile, setActiveFile] = useState('');
  const [elapsedTime, setElapsedTime] = useState(0);
  const [pipelineSteps, setPipelineSteps] = useState<PipelineStep[]>([]);

  // ── Sessions ──────────────────────────────────────────────────────────
  const [sessions, setSessions] = useState<ChatSession[]>([{
    id: 'default',
    title: 'New Session',
    lastModified: Date.now(),
    messages: [{
      id: '0',
      timestamp: ts(),
      sender: Sender.SYSTEM,
      text: 'Session initialized. Use the URL bar above to start cloning.',
    }],
  }]);
  const [currentSessionId, setCurrentSessionId] = useState('default');

  // ── History ───────────────────────────────────────────────────────────
  const [history, setHistory] = useState<CloneRecord[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [reactivatingId, setReactivatingId] = useState<string | null>(null);

  // ── Refs ──────────────────────────────────────────────────────────────
  const timerRef = useRef<ReturnType<typeof setInterval>>(undefined);
  const abortRef = useRef<AbortController | null>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  // keep a mutable ref for the current session id so SSE callbacks see latest
  const sessionIdRef = useRef(currentSessionId);
  useEffect(() => { sessionIdRef.current = currentSessionId; }, [currentSessionId]);
  // keep a mutable ref for cloneId so deactivation callbacks see latest
  const cloneIdRef = useRef(cloneId);
  useEffect(() => { cloneIdRef.current = cloneId; }, [cloneId]);

  const currentSession = sessions.find(s => s.id === currentSessionId) || sessions[0];

  // ═══════════════════════════════════════════════════════════════════════
  // Auth
  // ═══════════════════════════════════════════════════════════════════════

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      setUser(session?.user ?? null);
      setAuthLoading(false);
    });
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_ev, session) => {
      setUser(session?.user ?? null);
    });
    return () => subscription.unsubscribe();
  }, []);

  const handleLogout = async () => {
    await supabase.auth.signOut();
    setUser(null);
    setShowLanding(true);
  };

  // ═══════════════════════════════════════════════════════════════════════
  // Timer
  // ═══════════════════════════════════════════════════════════════════════

  useEffect(() => {
    if (isCloning) {
      setElapsedTime(0);
      timerRef.current = setInterval(() => setElapsedTime(t => t + 1), 1000);
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [isCloning]);

  // ═══════════════════════════════════════════════════════════════════════
  // Fetch history
  // ═══════════════════════════════════════════════════════════════════════

  useEffect(() => {
    fetchHistory();
  }, []);

  useEffect(() => {
    if (!showLanding) fetchHistory();
  }, [showLanding]);

  const fetchHistory = async () => {
    try {
      const res = await fetch(`${API_URL}/clones`);
      if (res.ok) {
        const data = await res.json();
        setHistory(data.clones || []);
      }
    } catch { /* silent */ }
  };

  // ═══════════════════════════════════════════════════════════════════════
  // Sandbox Deactivation — destroy sandbox on navigate away, keep in history
  // ═══════════════════════════════════════════════════════════════════════

  const deactivateCurrentClone = useCallback((idToDeactivate?: string) => {
    const targetId = idToDeactivate || cloneIdRef.current;
    if (!targetId) return;

    // Fire-and-forget: don't await, don't block navigation
    fetch(`${API_URL}/clone/${targetId}/deactivate`, { method: 'POST' })
      .catch(() => { /* silent */ });

    // Optimistically update local history
    setHistory(prev => prev.map(h =>
      h.id === targetId ? { ...h, is_active: false } : h
    ));
  }, []);

  // Deactivate sandbox on tab close / refresh
  useEffect(() => {
    const handleBeforeUnload = () => {
      const id = cloneIdRef.current;
      if (id) {
        navigator.sendBeacon(`${API_URL}/clone/${id}/deactivate`);
      }
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, []);

  // ═══════════════════════════════════════════════════════════════════════
  // Helpers
  // ═══════════════════════════════════════════════════════════════════════

  const addMsg = useCallback((msg: Omit<Message, 'id' | 'timestamp'>) => {
    const full: Message = {
      ...msg,
      id: Date.now().toString() + Math.random().toString(36).slice(2),
      timestamp: ts(),
    };
    setSessions(prev => prev.map(s =>
      s.id === sessionIdRef.current
        ? { ...s, lastModified: Date.now(), messages: [...s.messages, full] }
        : s
    ));
  }, []);

  // ═══════════════════════════════════════════════════════════════════════
  // SSE Stream Processing
  // ═══════════════════════════════════════════════════════════════════════

  const processStream = useCallback(async (response: Response) => {
    const reader = response.body?.getReader();
    const decoder = new TextDecoder();
    if (!reader) return;

    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const event: AgentEvent = JSON.parse(line.slice(6));
          handleEvent(event);
        } catch { /* skip */ }
      }
    }
    setIsCloning(false);
  }, []);

  const handleEvent = useCallback((event: AgentEvent) => {
    switch (event.type) {
      case 'clone_created':
        setCloneId(event.clone_id);
        setSessions(prev => prev.map(s =>
          s.id === sessionIdRef.current ? { ...s, cloneId: event.clone_id } : s
        ));
        break;

      case 'step':
        setPipelineSteps(prev => {
          const updated = prev.map(s => ({ ...s, status: 'completed' as const }));
          return [...updated, { step: event.step, message: event.message, status: 'active' as const }];
        });
        addMsg({ sender: Sender.SYSTEM, text: event.message, metadata: { eventType: 'step' } });
        break;

      case 'scrape_done': {
        const lines = [`Scraped: ${event.title || 'page'}`];
        lines.push(`  ${event.sections || 0} sections  ·  ${event.images || 0} images  ·  ${event.screenshots || 0} screenshots`);
        if (event.page_height) lines.push(`  Page height: ${event.page_height.toLocaleString()}px`);
        if (event.section_types?.length) lines.push(`  Sections: ${event.section_types.join(', ')}`);
        if (event.font_families?.length) lines.push(`  Fonts: ${event.font_families.join(', ')}`);
        if (event.nav_links || event.cta_buttons) lines.push(`  ${event.nav_links || 0} nav links  ·  ${event.cta_buttons || 0} buttons  ·  ${event.svgs || 0} SVGs`);
        if (event.colors?.length) lines.push(`  Colors: ${event.colors.join('  ')}`);
        addMsg({
          sender: Sender.BOT,
          text: lines.join('\n'),
          metadata: { domUpdateStatus: 'Scrape complete', eventType: 'scrape_done' },
        });
        break;
      }

      case 'file':
        setFiles(prev => ({ ...prev, [event.path]: event.content }));
        setActiveFile(event.path);
        // Don't spam activity — generation_complete shows the summary
        break;

      case 'file_updated':
        setFiles(prev => ({ ...prev, [event.path]: event.content }));
        addMsg({
          sender: Sender.BOT,
          text: `Updated: ${event.path}`,
          metadata: { files: [event.path], eventType: 'file_updated' },
        });
        break;

      case 'deployed':
        setPreviewUrl(event.preview_url);
        setSessions(prev => prev.map(s =>
          s.id === sessionIdRef.current ? { ...s, previewUrl: event.preview_url } : s
        ));
        addMsg({
          sender: Sender.BOT,
          text: `Deployed to sandbox`,
          metadata: { domUpdateStatus: 'Live preview ready', previewUrl: event.preview_url, eventType: 'deployed' },
        });
        setTimeout(() => {
          if (iframeRef.current) iframeRef.current.src = event.preview_url;
        }, 3000);
        break;

      case 'generation_complete':
        setPipelineSteps(prev => {
          const updated = prev.map(s => ({ ...s, status: 'completed' as const }));
          return [...updated, { step: 'generation_complete', message: `Generated ${event.file_count || 0} files in ${event.time || '?'}s`, status: 'completed' as const }];
        });
        addMsg({
          sender: Sender.BOT,
          text: `Code generation complete — ${event.file_count || 0} files in ${event.time || '?'}s`,
          metadata: {
            domUpdateStatus: 'Generation complete',
            files: event.files || [],
            eventType: 'generation_complete',
          },
        });
        break;

      case 'compiled':
        addMsg({
          sender: Sender.BOT,
          text: event.message || 'Compiled successfully',
          metadata: { domUpdateStatus: 'Compiled', eventType: 'compiled' },
        });
        break;

      case 'compile_errors':
        addMsg({
          sender: Sender.BOT,
          text: `Compilation errors (attempt ${event.attempt || '?'}/3): ${event.error_count || 0} error${(event.error_count || 0) !== 1 ? 's' : ''}`,
          metadata: {
            errors: event.report ? [event.report] : [],
            eventType: 'compile_errors',
          },
        });
        break;

      case 'warning':
        addMsg({
          sender: Sender.SYSTEM,
          text: `Warning: ${event.message}`,
          metadata: { eventType: 'warning' },
        });
        break;

      case 'agent_message':
        addMsg({ sender: Sender.BOT, text: event.text, metadata: { eventType: 'agent_message' } });
        break;

      case 'fix_step': {
        const isErr = event.phase?.includes('error') || event.phase === 'final_errors';
        const isOk = event.phase?.includes('clean') || event.phase?.includes('fixed');
        addMsg({
          sender: Sender.BOT,
          text: `Fix Agent: ${event.message}`,
          metadata: {
            phase: event.phase,
            errors: event.errors,
            issues: event.issues,
            files: event.files,
            eventType: 'fix_step',
            domUpdateStatus: isOk ? 'Fixed' : isErr ? 'Errors found' : undefined,
          },
        });
        break;
      }

      case 'logs':
        addMsg({
          sender: Sender.SYSTEM,
          text: event.has_errors ? 'Build errors found' : 'Build logs OK',
          metadata: { eventType: 'logs' },
        });
        break;

      case 'error':
        setIsCloning(false);
        setPipelineSteps([]);
        addMsg({
          sender: Sender.SYSTEM,
          text: `Error: ${event.message}`,
          metadata: { eventType: 'error', cloneIncomplete: true, retryUrl: url },
        });
        break;

      case 'done': {
        setIsCloning(false);
        setPipelineSteps([]);
        if (event.preview_url) setPreviewUrl(event.preview_url);
        if (event.clone_id) setCloneId(event.clone_id);
        const hasSandbox = !!(event.preview_url || event.sandbox_id);
        if (hasSandbox) {
          addMsg({
            sender: Sender.SYSTEM,
            text: `Clone complete — ${event.iterations || 0} iteration${(event.iterations || 0) !== 1 ? 's' : ''}`,
            metadata: { domUpdateStatus: 'Clone finished', eventType: 'done' },
          });
          setTimeout(() => {
            if (iframeRef.current && event.preview_url) iframeRef.current.src = event.preview_url;
          }, 2000);
        } else {
          addMsg({
            sender: Sender.SYSTEM,
            text: `Clone incomplete — sandbox was not created. You can retry the clone.`,
            metadata: { domUpdateStatus: 'Clone incomplete', eventType: 'done', cloneIncomplete: true, retryUrl: url },
          });
        }
        fetchHistory();
        break;
      }

      // skip step/thinking/iteration/clone_created from feed (handled above)
      default:
        break;
    }
  }, [addMsg]);

  // ═══════════════════════════════════════════════════════════════════════
  // Clone Actions
  // ═══════════════════════════════════════════════════════════════════════

  const handleStartClone = async (cloneUrl: string) => {
    const fmt = 'react';
    const trimmedUrl = cloneUrl.trim();
    if (!trimmedUrl) return;

    setUrl(trimmedUrl);
    setShowLanding(false);
    setIsCloning(true);
    setFiles({});
    setActiveFile('');
    setPreviewUrl('');
    setCloneId('');
    setPipelineSteps([]);

    // New session for this clone
    const sid = Date.now().toString();
    const newSession: ChatSession = {
      id: sid,
      title: trimmedUrl.replace(/^https?:\/\//, '').slice(0, 30),
      lastModified: Date.now(),
      url: trimmedUrl,
      outputFormat: fmt,
      messages: [{
        id: sid + '-init',
        timestamp: ts(),
        sender: Sender.SYSTEM,
        text: `Cloning ${trimmedUrl} (${fmt.toUpperCase()})...`,
      }],
    };
    setSessions(prev => [newSession, ...prev.filter(s => s.id !== 'default' || s.messages.length > 1)]);
    setCurrentSessionId(sid);
    sessionIdRef.current = sid;

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const response = await fetch(`${API_URL}/clone/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: trimmedUrl, output_format: fmt }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await processStream(response);
    } catch (error: any) {
      if (error.name === 'AbortError') {
        addMsg({ sender: Sender.SYSTEM, text: 'Clone stopped by user' });
      } else {
        addMsg({ sender: Sender.SYSTEM, text: `Error: ${error.message || 'Failed to connect'}` });
      }
      setIsCloning(false);
    } finally {
      abortRef.current = null;
    }
  };

  const handleStop = async () => {
    if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
    setIsCloning(false);
    setPipelineSteps([]);
    clearInterval(timerRef.current);

    try {
      await fetch(`${API_URL}/clone/stop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clone_id: cloneId || undefined }),
      });
    } catch { /* silent */ }

    addMsg({ sender: Sender.SYSTEM, text: 'Clone stopped' });
  };

  const handleNewClone = () => {
    setUrl('');
    setIsCloning(false);
    setFiles({});
    setActiveFile('');
    setPreviewUrl('');
    setCloneId('');
    setElapsedTime(0);
    setPipelineSteps([]);
    clearInterval(timerRef.current);

    const sid = Date.now().toString();
    const newSession: ChatSession = {
      id: sid,
      title: 'New Session',
      lastModified: Date.now(),
      messages: [{
        id: sid + '-init',
        timestamp: ts(),
        sender: Sender.SYSTEM,
        text: 'Session initialized. Use the URL bar above to start cloning.',
      }],
    };
    setSessions(prev => [newSession, ...prev]);
    setCurrentSessionId(sid);
    sessionIdRef.current = sid;
  };

  // ═══════════════════════════════════════════════════════════════════════
  // History Actions
  // ═══════════════════════════════════════════════════════════════════════

  const handleHistoryClick = async (record: CloneRecord) => {
    if (record.is_active === false) {
      const hasSaved = record.metadata?.files && Object.keys(record.metadata.files).length > 0;
      if (hasSaved) handleReactivate(record);
      return;
    }

    if (record.preview_url) setPreviewUrl(record.preview_url);
    setCloneId(record.id);
    setUrl(record.url || '');
    // output format is always react
    setFiles({});
    setActiveFile('');
    setShowHistory(false);
    setShowLanding(false);

    // Create session for this history item
    const existing = sessions.find(s => s.cloneId === record.id);
    if (existing) {
      setCurrentSessionId(existing.id);
      sessionIdRef.current = existing.id;
    } else {
      const sid = record.id;
      const s: ChatSession = {
        id: sid,
        title: record.url?.replace(/^https?:\/\//, '').slice(0, 30) || 'Clone',
        lastModified: Date.now(),
        cloneId: record.id,
        previewUrl: record.preview_url,
        url: record.url,
        outputFormat: 'react',
        messages: [{
          id: sid + '-load',
          timestamp: ts(),
          sender: Sender.SYSTEM,
          text: `Loaded clone: ${record.url}`,
        }],
      };
      setSessions(prev => [s, ...prev]);
      setCurrentSessionId(sid);
      sessionIdRef.current = sid;
    }
  };

  const handleDeleteClone = async (record: CloneRecord, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const res = await fetch(`${API_URL}/clone/${record.id}`, { method: 'DELETE' });
      if (res.ok) {
        setHistory(prev => prev.filter(h => h.id !== record.id));
        if (cloneId === record.id) handleNewClone();
      }
    } catch { /* silent */ }
  };

  const handleToggleActive = async (record: CloneRecord, e: React.MouseEvent) => {
    e.stopPropagation();
    const newActive = !record.is_active;
    try {
      const res = await fetch(`${API_URL}/clone/${record.id}/active`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: newActive }),
      });
      if (res.ok) {
        setHistory(prev => prev.map(h => h.id === record.id ? { ...h, is_active: newActive } : h));
        if (!newActive && cloneId === record.id) setPreviewUrl('');
      }
    } catch { /* silent */ }
  };

  const handleReactivate = async (record: CloneRecord) => {
    setReactivatingId(record.id);
    try {
      const res = await fetch(`${API_URL}/clone/${record.id}/rebuild`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        setHistory(prev => prev.map(h =>
          h.id === record.id ? { ...h, is_active: true, preview_url: data.preview_url } : h
        ));
        setPreviewUrl(data.preview_url);
        setCloneId(record.id);
        setUrl(record.url || '');
        setShowHistory(false);
      }
    } catch { /* silent */ }
    finally { setReactivatingId(null); }
  };

  // ═══════════════════════════════════════════════════════════════════════
  // File editing
  // ═══════════════════════════════════════════════════════════════════════

  const handleFileEdit = useCallback((filepath: string, content: string) => {
    setFiles(prev => ({ ...prev, [filepath]: content }));
    if (!cloneId) return;

    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      try {
        const res = await fetch(
          `${API_URL}/clone/${cloneId}/files/${encodeURIComponent(filepath)}`,
          { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }) }
        );
        if (res.ok) {
          const data = await res.json();
          // React hot-reloads automatically
        }
      } catch { /* silent */ }
    }, 500);
  }, [cloneId]);

  const handleFilesTabClick = async () => {
    if (cloneId && Object.keys(files).length === 0) {
      try {
        const res = await fetch(`${API_URL}/clone/${cloneId}/files`);
        if (res.ok) {
          const data = await res.json();
          const loaded = data.files || {};
          setFiles(loaded);
          const paths = Object.keys(loaded);
          if (paths.length > 0) setActiveFile(paths[0]);
        }
      } catch { /* silent */ }
    }
  };

  // ═══════════════════════════════════════════════════════════════════════
  // Session management
  // ═══════════════════════════════════════════════════════════════════════

  const switchSession = (id: string) => {
    setCurrentSessionId(id);
    sessionIdRef.current = id;
    const session = sessions.find(s => s.id === id);
    if (session) {
      setCloneId(session.cloneId || '');
      setPreviewUrl(session.previewUrl || '');
      setUrl(session.url || '');
      setFiles({});
      setActiveFile('');
    }
  };

  const deleteSession = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const rest = sessions.filter(s => s.id !== id);
    if (rest.length === 0) {
      handleNewClone();
    } else {
      setSessions(rest);
      if (currentSessionId === id) {
        setCurrentSessionId(rest[0].id);
        sessionIdRef.current = rest[0].id;
      }
    }
  };

  const deleteMessage = (messageId: string) => {
    setSessions(prev => prev.map(s =>
      s.id === currentSessionId
        ? { ...s, messages: s.messages.filter(m => m.id !== messageId) }
        : s
    ));
  };

  // ═══════════════════════════════════════════════════════════════════════
  // Render
  // ═══════════════════════════════════════════════════════════════════════

  if (!isAuthenticated) {
    return <PasswordGate onAuthenticated={() => setIsAuthenticated(true)} />;
  }

  if (authLoading) {
    return (
      <div className="min-h-screen bg-[#050505] flex items-center justify-center">
        <div className="text-gray-500 font-mono text-sm animate-pulse">Loading...</div>
      </div>
    );
  }

  if (showLanding) {
    return (
      <LandingPage
        onStart={(cloneUrl: string) => {
          if (cloneUrl) {
            handleStartClone(cloneUrl);
          } else {
            setShowLanding(false);
          }
        }}
        history={history}
        onHistoryClick={handleHistoryClick}
        onDeleteClone={handleDeleteClone}
        onToggleActive={handleToggleActive}
        onReactivate={handleReactivate}
        reactivatingId={reactivatingId}
      />
    );
  }

  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden bg-background-dark">
      <Header
        viewMode={viewMode}
        setViewMode={setViewMode}
        user={user}
        onLogout={handleLogout}
        url={url}
        setUrl={setUrl}
        isCloning={isCloning}
        elapsedTime={elapsedTime}
        onClone={() => handleStartClone(url)}
        onStop={handleStop}
        onNewClone={handleNewClone}
        onBack={() => setShowLanding(true)}
        history={history}
        showHistory={showHistory}
        setShowHistory={setShowHistory}
        onHistoryClick={handleHistoryClick}
        onDeleteClone={handleDeleteClone}
        onToggleActive={handleToggleActive}
        onReactivate={handleReactivate}
        reactivatingId={reactivatingId}
        fetchHistory={fetchHistory}
        cloneId={cloneId}
      />

      <main className="flex-1 flex overflow-hidden flex-col md:flex-row">
        <PreviewPane
          isVisible={viewMode === 'split' || viewMode === 'preview'}
          previewUrl={previewUrl}
          iframeRef={iframeRef}
          files={files}
          activeFile={activeFile}
          setActiveFile={setActiveFile}
          onFileEdit={handleFileEdit}
          onFilesTabClick={handleFilesTabClick}
          isCloning={isCloning}
        />
        <TerminalPane
          currentSession={currentSession}
          sessions={sessions}
          onCreateSession={handleNewClone}
          onSwitchSession={switchSession}
          onDeleteSession={deleteSession}
          onDeleteMessage={deleteMessage}
          isVisible={viewMode === 'split' || viewMode === 'editor'}
          isCloning={isCloning}
          cloneId={cloneId}
          pipelineSteps={pipelineSteps}
          onRetry={(retryUrl: string) => handleStartClone(retryUrl)}
        />
      </main>
    </div>
  );
};

export default App;
