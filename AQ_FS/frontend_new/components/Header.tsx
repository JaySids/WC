import React from 'react';
import type { User } from '@supabase/supabase-js';
import { CloneRecord } from '../types';

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

interface HeaderProps {
  viewMode: 'split' | 'editor' | 'preview';
  setViewMode: (mode: 'split' | 'editor' | 'preview') => void;
  user: User | null;
  onLogout: () => void;
  url: string;
  setUrl: (url: string) => void;
  outputFormat: 'html' | 'react';
  setOutputFormat: (f: 'html' | 'react') => void;
  isCloning: boolean;
  elapsedTime: number;
  onClone: () => void;
  onStop: () => void;
  onNewClone: () => void;
  onBack: () => void;
  history: CloneRecord[];
  showHistory: boolean;
  setShowHistory: (v: boolean) => void;
  onHistoryClick: (record: CloneRecord) => void;
  onDeleteClone: (record: CloneRecord, e: React.MouseEvent) => void;
  onToggleActive: (record: CloneRecord, e: React.MouseEvent) => void;
  onReactivate: (record: CloneRecord) => void;
  reactivatingId: string | null;
  fetchHistory: () => void;
  cloneId: string;
}

const Header: React.FC<HeaderProps> = ({
  viewMode, setViewMode, user, onLogout,
  url, setUrl, outputFormat, setOutputFormat,
  isCloning, elapsedTime, onClone, onStop, onNewClone, onBack,
  history, showHistory, setShowHistory,
  onHistoryClick, onDeleteClone, onToggleActive, onReactivate,
  reactivatingId, fetchHistory, cloneId,
}) => {
  const [showUserMenu, setShowUserMenu] = React.useState(false);

  const statusDot = (r: CloneRecord): string => {
    if (r.status === 'failed') return 'bg-red-400';
    if (r.status === 'processing') return 'bg-yellow-400 animate-pulse';
    if (r.is_active === false) return 'bg-gray-600';
    if (r.status === 'success') return 'bg-emerald-500';
    return 'bg-gray-600';
  };

  return (
    <header className="h-12 border-b border-border-dark flex items-center justify-between px-3 bg-white dark:bg-panel-dark z-20 shrink-0 select-none gap-2">
      {/* Left: logo + back + new */}
      <div className="flex items-center gap-2 shrink-0">
        <button onClick={onBack} className="opacity-60 hover:opacity-100 transition-opacity" title="Back to landing">
          <span className="material-symbols-outlined text-sm text-gray-400">arrow_back</span>
        </button>
        <div className="w-5 h-5 bg-white text-black rounded flex items-center justify-center" aria-hidden="true">
          <span className="material-symbols-outlined text-[14px] font-bold">terminal</span>
        </div>
        <h1 className="font-mono text-xs font-medium tracking-tight text-gray-500 dark:text-gray-400 hidden sm:block">
          Clone<span className="text-gray-900 dark:text-white">Studio</span>
        </h1>
        <button
          onClick={onNewClone}
          className="w-5 h-5 flex items-center justify-center rounded hover:bg-white/10 text-gray-500 hover:text-white transition-colors text-sm"
          title="New clone"
        >
          <span className="material-symbols-outlined text-[16px]">add</span>
        </button>
      </div>

      {/* Center: URL bar + format + clone/stop */}
      <div className="flex-1 flex items-center gap-2 max-w-2xl mx-2">
        <div className="flex-1 relative">
          <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-600 font-mono text-xs">{'>'}</span>
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com"
            className="w-full pl-7 pr-3 py-1.5 bg-[#121214] border border-white/10 rounded text-gray-300 text-xs font-mono placeholder-gray-700 focus:outline-none focus:border-white/20 transition-colors"
            onKeyDown={(e) => e.key === 'Enter' && !isCloning && onClone()}
            disabled={isCloning}
          />
        </div>

        {/* Format toggle */}
        <div className="flex border border-white/10 rounded overflow-hidden shrink-0">
          <button
            onClick={() => setOutputFormat('html')}
            className={`px-2 py-1.5 text-[10px] font-mono uppercase tracking-wider transition-colors ${
              outputFormat === 'html' ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-white'
            }`}
          >
            HTML
          </button>
          <button
            onClick={() => setOutputFormat('react')}
            className={`px-2 py-1.5 text-[10px] font-mono uppercase tracking-wider transition-colors ${
              outputFormat === 'react' ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-white'
            }`}
          >
            React
          </button>
        </div>

        {/* Clone / Stop */}
        {isCloning ? (
          <div className="flex items-center gap-2 shrink-0">
            <span className="text-[10px] text-gray-500 font-mono tabular-nums whitespace-nowrap">
              {Math.floor(elapsedTime / 60)}:{String(elapsedTime % 60).padStart(2, '0')}
            </span>
            <button
              onClick={onStop}
              className="px-3 py-1.5 bg-red-600/80 hover:bg-red-500 text-white text-[10px] font-mono uppercase tracking-wider rounded transition-colors"
            >
              Stop
            </button>
          </div>
        ) : (
          <button
            onClick={onClone}
            disabled={!url.trim()}
            className="px-4 py-1.5 bg-white text-black text-[10px] font-mono font-bold uppercase tracking-wider rounded hover:bg-gray-200 transition-colors disabled:opacity-30 disabled:cursor-not-allowed shrink-0"
          >
            Clone
          </button>
        )}
      </div>

      {/* Right: view toggle, connection, history, user */}
      <div className="flex items-center gap-1 shrink-0">
        {/* View toggle */}
        <div className="flex items-center gap-0.5 bg-transparent rounded p-0.5 mr-1" role="tablist">
          <button
            onClick={() => setViewMode(viewMode === 'editor' ? 'split' : 'editor')}
            className={`px-2 py-1 text-[10px] font-mono transition-colors rounded ${
              viewMode === 'editor' ? 'bg-[#18181b] text-white border border-border-dark' : 'text-gray-500 hover:text-white'
            }`}
          >
            Editor
          </button>
          <button
            onClick={() => setViewMode(viewMode === 'preview' ? 'split' : 'preview')}
            className={`px-2 py-1 text-[10px] font-mono transition-colors rounded ${
              viewMode === 'preview' ? 'bg-[#18181b] text-white border border-border-dark' : 'text-gray-500 hover:text-white'
            }`}
          >
            Preview
          </button>
        </div>

        {/* Connection dot */}
        <div className="flex items-center gap-1.5 mr-2 hidden sm:flex">
          <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 shadow-[0_0_4px_rgba(16,185,129,0.4)]"></div>
          <span className="text-[9px] uppercase tracking-widest text-gray-500 font-mono">Live</span>
        </div>

        {/* Clone ID badge */}
        {cloneId && (
          <span className="text-[9px] font-mono text-gray-600 mr-1 hidden sm:inline">
            {cloneId.slice(0, 8)}
          </span>
        )}

        {/* History */}
        <div className="relative">
          <button
            onClick={() => { setShowHistory(!showHistory); if (!showHistory) fetchHistory(); }}
            className="p-1.5 rounded hover:bg-white/10 text-gray-400 hover:text-white transition-colors"
            title="Clone history"
          >
            <span className="material-symbols-outlined text-[16px]">history</span>
          </button>

          {showHistory && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setShowHistory(false)} />
              <div className="absolute right-0 top-full mt-1 w-96 bg-[#0c0c0e] border border-white/10 rounded-lg shadow-2xl z-50 overflow-hidden">
                <div className="px-3 py-2 border-b border-white/5 flex items-center justify-between">
                  <span className="text-[10px] font-mono text-gray-500 uppercase tracking-wider">History</span>
                  <button onClick={() => { setShowHistory(false); fetchHistory(); }} className="text-[10px] text-gray-500 hover:text-white font-mono">
                    Refresh
                  </button>
                </div>
                <div className="max-h-96 overflow-y-auto custom-scrollbar">
                  {history.length === 0 ? (
                    <p className="px-3 py-6 text-xs text-gray-600 text-center font-mono">No clones yet</p>
                  ) : (
                    history.map(record => {
                      const inactive = record.is_active === false;
                      const hasSaved = record.metadata?.files && Object.keys(record.metadata.files).length > 0;
                      const fmt = record.output_format || record.metadata?.output_format || 'html';

                      return (
                        <div
                          key={record.id}
                          className={`px-3 py-2.5 hover:bg-white/[0.03] transition-colors border-b border-white/5 last:border-0 ${inactive ? 'opacity-50' : ''}`}
                        >
                          <button
                            onClick={() => onHistoryClick(record)}
                            className="w-full flex items-center gap-2"
                          >
                            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDot(record)}`} />
                            <span className="text-xs text-gray-300 truncate flex-1 text-left font-mono">
                              {record.url?.replace(/^https?:\/\//, '').slice(0, 35)}
                            </span>
                            <span className={`px-1.5 py-0.5 rounded text-[8px] font-mono uppercase shrink-0 ${
                              fmt === 'react' ? 'bg-blue-500/15 text-blue-400' : 'bg-orange-500/15 text-orange-400'
                            }`}>
                              {fmt}
                            </span>
                            {record.created_at && (
                              <span className="text-[9px] text-gray-600 shrink-0 font-mono">{timeAgo(record.created_at)}</span>
                            )}
                          </button>

                          <div className="flex items-center gap-1 mt-1.5 ml-3.5">
                            {record.status === 'success' && (
                              <button
                                onClick={(e) => onToggleActive(record, e)}
                                className={`px-1.5 py-0.5 rounded text-[9px] font-mono transition-colors ${
                                  inactive
                                    ? 'bg-white/5 text-gray-500 hover:text-white'
                                    : 'bg-emerald-500/10 text-emerald-400 hover:bg-red-500/10 hover:text-red-400'
                                }`}
                              >
                                {inactive ? 'Inactive' : 'Active'}
                              </button>
                            )}
                            {inactive && hasSaved && (
                              <button
                                onClick={(e) => { e.stopPropagation(); onReactivate(record); }}
                                disabled={reactivatingId === record.id}
                                className="px-1.5 py-0.5 rounded text-[9px] font-mono bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 transition-colors disabled:opacity-50"
                              >
                                {reactivatingId === record.id ? 'Starting...' : 'Reactivate'}
                              </button>
                            )}
                            <button
                              onClick={(e) => onDeleteClone(record, e)}
                              className="px-1.5 py-0.5 rounded text-[9px] font-mono text-gray-600 hover:bg-red-500/10 hover:text-red-400 transition-colors ml-auto"
                            >
                              Delete
                            </button>
                          </div>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>
            </>
          )}
        </div>

        {/* User menu */}
        <div className="relative">
          <button
            onClick={() => setShowUserMenu(!showUserMenu)}
            className="p-1.5 rounded hover:bg-white/10 transition-colors"
            title={user ? user.email || 'Account' : 'Not logged in'}
          >
            <span className="material-symbols-outlined text-[16px] text-gray-400">account_circle</span>
          </button>

          {showUserMenu && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setShowUserMenu(false)} />
              <div className="absolute right-0 top-full mt-1 w-56 bg-[#0c0c0e] border border-white/10 rounded-lg shadow-2xl z-50 overflow-hidden">
                {user ? (
                  <>
                    <div className="px-3 py-2 border-b border-white/5">
                      <p className="text-[10px] font-mono text-gray-500 uppercase tracking-wider">Signed in</p>
                      <p className="text-xs text-gray-300 font-mono truncate mt-0.5">{user.email}</p>
                    </div>
                    <button
                      onClick={() => { setShowUserMenu(false); onLogout(); }}
                      className="w-full px-3 py-2 text-left text-xs text-gray-400 hover:text-white hover:bg-white/[0.03] transition-colors font-mono"
                    >
                      Sign Out
                    </button>
                  </>
                ) : (
                  <div className="px-3 py-2">
                    <p className="text-[10px] font-mono text-gray-600">Not signed in</p>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
};

export default Header;
