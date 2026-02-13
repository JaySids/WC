import React, { useState, useRef, useEffect } from 'react';
import { Message, Sender, ChatSession, PipelineStep } from '../types';
import CodeDiff from './CodeDiff';

interface TerminalPaneProps {
  currentSession: ChatSession;
  sessions: ChatSession[];
  onCreateSession: () => void;
  onSwitchSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string, e: React.MouseEvent) => void;
  onDeleteMessage: (messageId: string) => void;
  isVisible: boolean;
  isCloning: boolean;
  cloneId: string;
  pipelineSteps: PipelineStep[];
  onRetry: (url: string) => void;
}

const TerminalPane: React.FC<TerminalPaneProps> = ({
  currentSession, sessions,
  onCreateSession, onSwitchSession, onDeleteSession, onDeleteMessage,
  isVisible, isCloning, cloneId, pipelineSteps, onRetry,
}) => {
  const [showHistory, setShowHistory] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    if (!showHistory) scrollToBottom();
  }, [currentSession.messages, showHistory]);

  // Pipeline stepper
  const renderStepper = () => {
    if (pipelineSteps.length === 0) return null;
    const completed = pipelineSteps.filter(s => s.status === 'completed');
    const active = pipelineSteps.find(s => s.status === 'active');
    const recent = completed.slice(-3);
    const hidden = completed.length - recent.length;

    return (
      <div className="px-4 pt-3 pb-1 border-b border-border-dark">
        <div className="rounded bg-[#121214] border border-white/5 overflow-hidden">
          {hidden > 0 && (
            <div className="px-3 py-1 text-[9px] text-gray-600 font-mono">
              +{hidden} step{hidden !== 1 ? 's' : ''} completed
            </div>
          )}
          {recent.map((s, i) => (
            <div key={`d-${i}`} className={`flex items-center gap-2 px-3 py-1 ${i > 0 || hidden > 0 ? 'border-t border-white/5' : ''}`}>
              <span className="text-emerald-500 text-[10px]">&#10003;</span>
              <span className="text-[10px] text-gray-500 font-mono">{s.message}</span>
            </div>
          ))}
          {active && (
            <div className={`flex items-center gap-2 px-3 py-2 bg-white/[0.02] ${recent.length > 0 || hidden > 0 ? 'border-t border-white/5' : ''}`}>
              <span className="w-3 h-3 rounded-full border-2 border-blue-400 border-t-transparent animate-spin" />
              <span className="text-xs text-gray-300 font-mono">{active.message}</span>
            </div>
          )}
        </div>
      </div>
    );
  };

  // Message rendering
  const renderMessage = (msg: Message) => {
    const isSystem = msg.sender === Sender.SYSTEM;
    const isBot = msg.sender === Sender.BOT;
    const isUser = msg.sender === Sender.USER;
    const meta = msg.metadata || {};

    // Determine styling based on fix_step phase
    let borderAccent = 'border-transparent';
    if (meta.eventType === 'fix_step') {
      const phase = meta.phase || '';
      if (phase.includes('error') || phase === 'final_errors') borderAccent = 'border-red-500/50';
      else if (phase.includes('clean') || phase.includes('fixed')) borderAccent = 'border-emerald-500/50';
      else if (phase.includes('issues') || phase.includes('errors')) borderAccent = 'border-yellow-500/50';
      else borderAccent = 'border-blue-500/50';
    }

    const bgClass = isUser ? 'bg-[#121214]/50 hover:bg-[#121214]' : 'hover:bg-white/[0.02]';
    const userBorder = isUser ? 'border-l-2 border-blue-500/50' : `border-l-2 ${meta.eventType === 'fix_step' ? borderAccent : 'border-transparent hover:border-gray-700'}`;

    return (
      <div key={msg.id} className={`flex gap-0 py-3 px-4 group relative transition-colors ${bgClass} ${userBorder}`}>
        {/* Delete button */}
        <button
          onClick={() => onDeleteMessage(msg.id)}
          className="absolute right-2 top-2 p-0.5 text-gray-600 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity"
          title="Delete"
        >
          <span className="material-symbols-outlined text-[12px]">close</span>
        </button>

        {/* Timestamp */}
        <div className="w-11 shrink-0 text-[9px] text-gray-600 pt-0.5 border-r border-border-dark pr-2 text-right font-mono select-none">
          {msg.timestamp}
        </div>

        {/* Content */}
        <div className="pl-3 flex-1 min-w-0">
          <div className={`text-[10px] mb-1 font-bold tracking-wide flex items-center gap-1.5 ${
            isBot ? 'text-blue-400' : isUser ? 'text-white' : 'text-gray-500'
          }`}>
            {isSystem && <span className="w-1 h-1 rounded-full bg-blue-500"></span>}
            {isSystem ? 'SYSTEM' : isBot ? 'CLONE_BOT' : 'USER'}
            {meta.isUserAdmin && (
              <span className="text-[8px] font-normal text-gray-500 px-1 border border-gray-800 rounded">admin</span>
            )}
          </div>

          <div className="text-gray-300 leading-relaxed text-[12px] whitespace-pre-wrap font-mono">
            {msg.text}
          </div>

          {msg.codeDiff && <CodeDiff data={msg.codeDiff} />}

          {/* File badges */}
          {meta.files && meta.files.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {meta.files.map((fp, i) => (
                <span key={i} className="px-1.5 py-0.5 bg-[#121214] rounded text-[9px] text-gray-400 font-mono border border-white/5">
                  {fp}
                </span>
              ))}
            </div>
          )}

          {/* Error list */}
          {meta.errors && meta.errors.length > 0 && (
            <div className="mt-1.5 space-y-0.5">
              {meta.errors.slice(0, 5).map((err, i) => (
                <p key={i} className="text-[10px] text-gray-500 font-mono">- {err}</p>
              ))}
              {meta.errors.length > 5 && (
                <p className="text-[10px] text-gray-600 font-mono">...and {meta.errors.length - 5} more</p>
              )}
            </div>
          )}

          {/* DOM update status */}
          {meta.domUpdateStatus && (
            <div className="mt-2 flex items-center gap-2">
              <span className="flex h-1.5 w-1.5 relative">
                {meta.cloneIncomplete ? (
                  <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-red-500"></span>
                ) : (
                  <>
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                    <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500"></span>
                  </>
                )}
              </span>
              <span className={`text-[10px] font-mono ${meta.cloneIncomplete ? 'text-red-400' : 'text-gray-400'}`}>{meta.domUpdateStatus}</span>
            </div>
          )}

          {/* Retry button for incomplete clones */}
          {meta.cloneIncomplete && meta.retryUrl && !isCloning && (
            <button
              onClick={() => onRetry(meta.retryUrl!)}
              className="mt-2 inline-flex items-center gap-1.5 px-3 py-1.5 rounded bg-white/5 border border-white/10 text-[11px] font-mono text-gray-300 hover:bg-white/10 hover:border-white/20 hover:text-white transition-all"
            >
              <span className="material-symbols-outlined text-[14px]">refresh</span>
              Retry Clone
            </button>
          )}

          {/* Preview link */}
          {meta.previewUrl && (
            <a
              href={meta.previewUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1 block text-[10px] text-blue-400/70 hover:text-blue-300 font-mono truncate"
            >
              {meta.previewUrl}
            </a>
          )}
        </div>
      </div>
    );
  };

  return (
    <aside className={`flex-1 md:flex-none w-full md:w-[420px] flex flex-col bg-[#09090b] relative shrink-0 z-30 md:border-l border-border-dark h-full ${!isVisible ? 'hidden' : ''}`}>
      {/* Activity Header */}
      <div className="h-10 px-4 border-b border-border-dark flex items-center justify-between shrink-0 bg-[#09090b]">
        <div className="flex items-center gap-2">
          <span className="material-symbols-outlined text-sm text-gray-400">monitoring</span>
          <span className="font-mono text-xs text-gray-300">ACTIVITY</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => { onCreateSession(); setShowHistory(false); }}
            className="text-[10px] text-gray-400 hover:text-white font-mono flex items-center gap-1 transition-colors"
          >
            <span className="material-symbols-outlined text-[12px]">add</span> NEW
          </button>
          <div className="h-3 w-px bg-border-dark"></div>
          <button
            onClick={() => setShowHistory(!showHistory)}
            className={`text-[10px] font-mono flex items-center gap-1 transition-colors ${
              showHistory ? 'text-white' : 'text-gray-400 hover:text-white'
            }`}
          >
            <span className="material-symbols-outlined text-[12px]">{showHistory ? 'chat' : 'history'}</span>
            {showHistory ? 'BACK' : 'HISTORY'}
          </button>
          <div className="h-3 w-px bg-border-dark"></div>
          <span className={`w-1.5 h-1.5 rounded-sm ${
            isCloning ? 'bg-blue-500 animate-pulse' : showHistory ? 'bg-yellow-600' : 'bg-gray-600'
          }`}></span>
          <span className="font-mono text-[10px] text-gray-500 uppercase">
            {isCloning ? 'CLONING' : showHistory ? 'HISTORY' : cloneId ? 'CONNECTED' : 'IDLE'}
          </span>
        </div>
      </div>

      {/* Pipeline stepper */}
      {renderStepper()}

      {/* Main content: History or Messages */}
      <div className="flex-1 overflow-y-auto custom-scrollbar p-0 bg-[#09090b] relative">
        {showHistory ? (
          <div className="p-4 space-y-2">
            <h3 className="text-[10px] font-mono text-gray-500 mb-4 uppercase tracking-wider">Session History</h3>
            {sessions.length === 0 && (
              <div className="text-center py-10 text-gray-600 font-mono text-xs">No sessions</div>
            )}
            {sessions.map(session => (
              <div
                key={session.id}
                onClick={() => { onSwitchSession(session.id); setShowHistory(false); }}
                className={`group flex items-center justify-between p-3 rounded border cursor-pointer transition-all ${
                  session.id === currentSession.id
                    ? 'bg-[#18181b] border-blue-500/30'
                    : 'bg-[#121214]/50 border-border-dark hover:border-gray-600'
                }`}
              >
                <div className="flex flex-col gap-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[11px] text-gray-300 font-medium truncate">{session.title}</span>
                    {session.id === currentSession.id && <span className="w-1.5 h-1.5 rounded-full bg-blue-500"></span>}
                  </div>
                  <span className="text-[9px] text-gray-600 font-mono">
                    {new Date(session.lastModified).toLocaleString()}
                  </span>
                </div>
                <button
                  onClick={(e) => onDeleteSession(session.id, e)}
                  className="w-7 h-7 flex items-center justify-center rounded text-gray-600 hover:text-red-400 hover:bg-red-900/20 transition-colors opacity-0 group-hover:opacity-100"
                >
                  <span className="material-symbols-outlined text-[14px]">delete</span>
                </button>
              </div>
            ))}
          </div>
        ) : (
          <div className="flex flex-col font-mono text-sm min-h-full">
            {currentSession.messages.length === 0 && (
              <div className="flex-1 flex flex-col items-center justify-center text-gray-700 space-y-2 opacity-50 select-none">
                <span className="material-symbols-outlined text-4xl">monitoring</span>
                <span className="text-[10px]">Clone activity will appear here...</span>
              </div>
            )}
            {currentSession.messages.map(renderMessage)}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

    </aside>
  );
};

export default TerminalPane;
