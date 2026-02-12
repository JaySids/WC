import React, { useState } from 'react';
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

function statusDot(r: CloneRecord): string {
  if (r.status === 'failed') return 'bg-red-400';
  if (r.status === 'processing') return 'bg-yellow-400 animate-pulse';
  if (r.is_active === false) return 'bg-gray-600';
  if (r.status === 'success') return 'bg-emerald-500';
  return 'bg-gray-600';
}

interface LandingPageProps {
  onStart: (url: string) => void;
  history: CloneRecord[];
  onHistoryClick: (record: CloneRecord) => void;
  onDeleteClone: (record: CloneRecord, e: React.MouseEvent) => void;
  onToggleActive: (record: CloneRecord, e: React.MouseEvent) => void;
  onReactivate: (record: CloneRecord) => void;
  reactivatingId: string | null;
}

const LandingPage: React.FC<LandingPageProps> = ({
  onStart, history, onHistoryClick, onDeleteClone, onToggleActive, onReactivate, reactivatingId,
}) => {
  const [url, setUrl] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onStart(url.trim());
  };

  return (
    <div className="h-screen bg-[#050505] text-white font-display selection:bg-emerald-900 selection:text-white overflow-x-hidden overflow-y-auto">
      {/* Navigation */}
      <nav className="flex items-center justify-between px-6 md:px-8 py-6 border-b border-white/5">
        <div className="flex items-center gap-2 font-mono text-sm tracking-tight">
          <span className="text-gray-500">/</span>
          <span className="font-semibold">clone.ai</span>
        </div>
        <button
          onClick={() => onStart('')}
          className="px-5 py-2 text-[11px] font-mono uppercase tracking-wider bg-white text-black rounded hover:bg-gray-200 transition-all"
        >
          Dashboard
        </button>
      </nav>

      {/* Hero Section */}
      <section className="pt-24 pb-16 px-4 flex flex-col items-center text-center relative">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-white/5 border border-white/10 text-[10px] font-mono text-emerald-400 mb-12">
          <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></div>
          <span>v2.0.0 stable</span>
        </div>

        <h1 className="text-5xl md:text-7xl lg:text-8xl font-medium tracking-tight mb-8 max-w-5xl mx-auto leading-[1.1]">
          Turn any website into <br />
          <span className="font-serif italic text-gray-400">a live clone.</span>
        </h1>

        <p className="text-lg text-gray-500 max-w-2xl mx-auto mb-16 font-light leading-relaxed">
          Paste URL. AI scrapes, generates, deploys.<br />
          Live preview in a cloud sandbox in seconds.
        </p>

        <form onSubmit={handleSubmit} className="w-full max-w-2xl relative group">
          <div className="absolute inset-0 bg-gradient-to-r from-emerald-500/20 to-blue-500/20 blur-xl opacity-0 group-hover:opacity-100 transition-opacity duration-700 rounded-lg"></div>
          <div className="relative bg-[#0c0c0e] border border-white/10 rounded-lg p-2 flex items-center gap-3 shadow-2xl">
            <span className="pl-4 text-gray-600 font-mono text-sm">{'>'}{' '}</span>
            <input
              type="text"
              placeholder="https://example.com"
              className="flex-1 bg-transparent border-none focus:ring-0 text-gray-300 font-mono text-sm placeholder-gray-700 outline-none"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
            <span className="px-2 py-1 text-[10px] font-mono uppercase tracking-wider bg-white/10 text-white rounded shrink-0">React</span>
            <button
              type="submit"
              className="px-6 py-2 bg-white text-black font-mono text-xs font-bold uppercase tracking-wider rounded-md hover:bg-gray-200 transition-colors"
            >
              Init_Clone
            </button>
          </div>
          <div className="flex justify-between mt-3 px-1 text-[10px] font-mono text-gray-600 uppercase tracking-wider">
            <span>Output: React (Next.js)</span>
            <span>Enter to submit</span>
          </div>
        </form>
      </section>

      {/* Clone History Section */}
      {history.length > 0 && (
        <section className="pb-24 px-4 md:px-8">
          <div className="max-w-3xl mx-auto">
            <div className="flex items-center gap-3 mb-6">
              <span className="font-mono text-xs text-gray-500 uppercase tracking-widest">Recent Clones</span>
              <div className="flex-1 h-px bg-white/5" />
              <span className="text-[10px] font-mono text-gray-600">{history.length} total</span>
            </div>

            <div className="bg-[#0c0c0e] border border-white/10 rounded-lg overflow-hidden divide-y divide-white/5">
              {history.map(record => {
                const inactive = record.is_active === false;
                const hasSaved = record.metadata?.files && Object.keys(record.metadata.files).length > 0;
                const fmt = record.output_format || record.metadata?.output_format || 'react';

                return (
                  <div
                    key={record.id}
                    className={`px-4 py-3 hover:bg-white/[0.03] transition-colors ${inactive ? 'opacity-50' : ''}`}
                  >
                    <button
                      onClick={() => onHistoryClick(record)}
                      className="w-full flex items-center gap-3"
                    >
                      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDot(record)}`} />
                      <span className="text-sm text-gray-300 truncate flex-1 text-left font-mono">
                        {record.url?.replace(/^https?:\/\//, '').slice(0, 50)}
                      </span>
                      <span className={`px-1.5 py-0.5 rounded text-[9px] font-mono uppercase shrink-0 ${
                        fmt === 'react' ? 'bg-blue-500/15 text-blue-400' : 'bg-orange-500/15 text-orange-400'
                      }`}>
                        {fmt}
                      </span>
                      {record.created_at && (
                        <span className="text-[10px] text-gray-600 shrink-0 font-mono">{timeAgo(record.created_at)}</span>
                      )}
                    </button>

                    <div className="flex items-center gap-1.5 mt-2 ml-4">
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
              })}
            </div>
          </div>
        </section>
      )}
    </div>
  );
};

export default LandingPage;
