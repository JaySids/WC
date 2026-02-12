import React, { useState } from 'react';
import type { User } from '@supabase/supabase-js';

interface LandingPageProps {
  onStart: (url: string, format?: 'html' | 'react') => void;
  onLogin: () => void;
  user: User | null;
}

const LandingPage: React.FC<LandingPageProps> = ({ onStart, onLogin, user }) => {
  const [url, setUrl] = useState('');
  const [format, setFormat] = useState<'html' | 'react'>('html');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onStart(url.trim(), format);
  };

  return (
    <div className="min-h-screen bg-[#050505] text-white font-display selection:bg-emerald-900 selection:text-white overflow-x-hidden overflow-y-auto">
      {/* Navigation */}
      <nav className="flex items-center justify-between px-6 md:px-8 py-6 border-b border-white/5">
        <div className="flex items-center gap-2 font-mono text-sm tracking-tight">
          <span className="text-gray-500">/</span>
          <span className="font-semibold">clone.ai</span>
        </div>
        <div className="hidden md:flex items-center gap-8 text-[11px] font-mono tracking-wider text-gray-500 uppercase">
          <button className="hover:text-white transition-colors">Features</button>
          <button className="hover:text-white transition-colors">API</button>
          <button className="hover:text-white transition-colors">Pricing</button>
        </div>
        <div className="flex items-center gap-3">
          {user ? (
            <div className="flex items-center gap-2">
              <span className="text-[11px] font-mono text-gray-400">{user.email?.split('@')[0]}</span>
              <button
                onClick={() => onStart('', undefined)}
                className="px-5 py-2 text-[11px] font-mono uppercase tracking-wider bg-white text-black rounded hover:bg-gray-200 transition-all"
              >
                Dashboard
              </button>
            </div>
          ) : (
            <button
              onClick={onLogin}
              className="px-5 py-2 text-[11px] font-mono uppercase tracking-wider border border-white/20 rounded hover:bg-white hover:text-black transition-all"
            >
              Login
            </button>
          )}
        </div>
      </nav>

      {/* Hero Section */}
      <section className="pt-24 pb-32 px-4 flex flex-col items-center text-center relative">
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
            {/* Format toggle */}
            <div className="flex border border-white/10 rounded overflow-hidden shrink-0">
              <button
                type="button"
                onClick={() => setFormat('html')}
                className={`px-2 py-1 text-[10px] font-mono uppercase tracking-wider transition-colors ${
                  format === 'html' ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-white'
                }`}
              >
                HTML
              </button>
              <button
                type="button"
                onClick={() => setFormat('react')}
                className={`px-2 py-1 text-[10px] font-mono uppercase tracking-wider transition-colors ${
                  format === 'react' ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-white'
                }`}
              >
                React
              </button>
            </div>
            <button
              type="submit"
              className="px-6 py-2 bg-white text-black font-mono text-xs font-bold uppercase tracking-wider rounded-md hover:bg-gray-200 transition-colors"
            >
              Init_Clone
            </button>
          </div>
          <div className="flex justify-between mt-3 px-1 text-[10px] font-mono text-gray-600 uppercase tracking-wider">
            <span>Supported: React, Vue, Static HTML</span>
            <span>Enter to submit</span>
          </div>
        </form>
      </section>

      {/* Processing Logic Section */}
      <section className="py-24 px-6 md:px-12 border-t border-white/5 bg-[#08080a]">
        <div className="max-w-7xl mx-auto grid grid-cols-1 lg:grid-cols-2 gap-20 items-center">
          <div>
            <span className="font-mono text-xs text-gray-500 uppercase tracking-widest mb-6 block">Processing Logic</span>
            <h2 className="text-3xl md:text-4xl font-medium mb-6">From URL to live clone.</h2>
            <p className="text-gray-400 leading-relaxed mb-12 max-w-md">
              Our AI agent scrapes the page, captures every asset, generates pixel-perfect code,
              deploys to a cloud sandbox, then self-corrects until it matches.
            </p>

            <div className="space-y-8">
              <div className="group">
                <div className="flex items-baseline gap-4 mb-2">
                  <span className="font-mono text-xs text-emerald-500">01</span>
                  <h3 className="font-medium text-white group-hover:text-emerald-400 transition-colors">Network Interception</h3>
                </div>
                <p className="pl-8 text-sm text-gray-500">Headless browser captures all assets, styles, fonts, and images.</p>
              </div>
              <div className="group">
                <div className="flex items-baseline gap-4 mb-2">
                  <span className="font-mono text-xs text-emerald-500">02</span>
                  <h3 className="font-medium text-white group-hover:text-emerald-400 transition-colors">AI Code Generation</h3>
                </div>
                <p className="pl-8 text-sm text-gray-500">Claude generates HTML+Tailwind or React+Next.js from scraped data.</p>
              </div>
              <div className="group">
                <div className="flex items-baseline gap-4 mb-2">
                  <span className="font-mono text-xs text-emerald-500">03</span>
                  <h3 className="font-medium text-white group-hover:text-emerald-400 transition-colors">Cloud Deployment</h3>
                </div>
                <p className="pl-8 text-sm text-gray-500">Deployed to Daytona sandbox with live preview URL.</p>
              </div>
              <div className="group">
                <div className="flex items-baseline gap-4 mb-2">
                  <span className="font-mono text-xs text-emerald-500">04</span>
                  <h3 className="font-medium text-white group-hover:text-emerald-400 transition-colors">Self-Correction</h3>
                </div>
                <p className="pl-8 text-sm text-gray-500">Fix agent detects build errors and patches automatically.</p>
              </div>
            </div>
          </div>

          {/* Terminal Visual */}
          <div className="bg-[#0c0c0e] rounded-lg border border-white/10 p-1 shadow-2xl font-mono text-xs">
            <div className="flex items-center justify-between px-3 py-2 border-b border-white/5 bg-[#121214] rounded-t-md">
              <div className="flex gap-1.5">
                <div className="w-2.5 h-2.5 rounded-full bg-white/10"></div>
                <div className="w-2.5 h-2.5 rounded-full bg-white/10"></div>
                <div className="w-2.5 h-2.5 rounded-full bg-white/10"></div>
              </div>
              <span className="text-gray-600 text-[10px]">clone-agent</span>
            </div>
            <div className="p-6 space-y-2 h-[320px] overflow-hidden">
              <div className="flex gap-2">
                <span className="text-emerald-500">$</span>
                <span className="text-gray-300">clone-ai init --url https://stripe.com</span>
              </div>
              <div className="text-gray-500">[10:42:01] <span className="text-blue-400">INFO</span> Launching headless browser...</div>
              <div className="text-gray-500">[10:42:02] <span className="text-blue-400">INFO</span> Intercepting network requests</div>
              <div className="text-gray-500">[10:42:03] <span className="text-blue-400">INFO</span> Captured 47 assets (CSS, JS, fonts, images)</div>
              <div className="text-gray-500">[10:42:04] <span className="text-blue-400">INFO</span> Extracting sections, theme, clickables...</div>
              <div className="pl-2 text-gray-400 border-l border-white/10 ml-1">
                <div>&gt; 8 sections detected</div>
                <div>&gt; 23 images captured</div>
                <div>&gt; 12 SVG icons extracted</div>
              </div>
              <div className="text-gray-500 mt-2">[10:42:05] <span className="text-blue-400">INFO</span> Generating code with Claude...</div>
              <div className="text-gray-500">[10:42:15] <span className="text-blue-400">INFO</span> Deploying to Daytona sandbox...</div>
              <div className="flex items-center gap-2 mt-1">
                <div className="h-1 w-32 bg-gray-800 rounded overflow-hidden">
                  <div className="h-full bg-emerald-500 w-[100%]"></div>
                </div>
                <span className="text-gray-500 text-[10px]">100%</span>
              </div>
              <div className="text-emerald-500 mt-2">SUCCESS <span className="text-gray-300">Preview: <span className="text-blue-400 underline">https://sandbox-abc123.daytona.app</span></span></div>
              <div className="text-emerald-500 mt-2 animate-pulse">$ <span className="w-2 h-4 bg-emerald-500 inline-block align-middle ml-1"></span></div>
            </div>
          </div>
        </div>
      </section>

      {/* Query Section */}
      <section className="py-32 px-4 flex flex-col items-center border-t border-white/5">
        <h2 className="text-3xl font-medium mb-3">Iterate on your clone</h2>
        <p className="text-gray-500 font-mono text-sm mb-16">Chat with the AI to make changes.</p>

        <div className="w-full max-w-3xl bg-[#0c0c0e] border border-white/10 rounded-xl p-6 md:p-10 shadow-2xl relative">
          <div className="flex flex-col gap-8">
            {/* User Message */}
            <div className="self-end max-w-md">
              <div className="text-right text-sm text-white mb-2">Make the hero section gradient darker and add a CTA button</div>
              <div className="text-right text-[10px] font-mono text-gray-600 flex justify-end items-center gap-2">
                10:43 AM <div className="w-1.5 h-1.5 rounded-full bg-white"></div>
              </div>
            </div>

            {/* AI Response */}
            <div className="self-start max-w-2xl w-full">
              <div className="flex items-center gap-3 mb-3">
                <div className="w-5 h-5 rounded bg-white text-black flex items-center justify-center font-bold text-[10px] font-mono">AI</div>
                <span className="text-[10px] font-mono text-gray-600">10:43 AM</span>
              </div>
              <div className="text-gray-300 text-sm leading-relaxed mb-4">
                Updated the hero gradient and added a primary CTA. Changes deployed to sandbox.
              </div>
              <div className="bg-[#18181b] rounded border border-white/5 p-4 font-mono text-xs overflow-x-auto text-gray-300">
<pre>{`// Updated: app/page.tsx
<section className="bg-gradient-to-br from-slate-950 via-slate-900 to-blue-950">
  <button className="px-8 py-4 bg-white text-black rounded-lg font-bold">
    Get Started Free
  </button>
</section>`}</pre>
              </div>
              <div className="mt-4 flex gap-2">
                <span className="text-[10px] font-mono border border-white/10 rounded px-2 py-1 text-emerald-400">
                  Live preview updated
                </span>
              </div>
            </div>
          </div>

          <div className="mt-12 relative">
            <input type="text" disabled placeholder="Ask a follow-up question..." className="w-full bg-[#121214] border border-white/5 rounded-lg py-3 px-4 text-sm font-mono text-gray-500 focus:outline-none cursor-not-allowed" />
            <div className="absolute right-3 top-3 text-gray-600">
              <span className="material-symbols-outlined text-sm">arrow_turn_left</span>
            </div>
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-white/5 py-12 px-6 md:px-12 bg-[#050505]">
        <div className="max-w-7xl mx-auto flex flex-col md:flex-row items-center justify-between gap-6 font-mono text-xs text-gray-600">
          <div>clone.ai &copy; 2025</div>
          <div className="flex gap-8">
            <button className="hover:text-white transition-colors">Manifesto</button>
            <button className="hover:text-white transition-colors">Twitter</button>
            <button className="hover:text-white transition-colors">GitHub</button>
          </div>
          <div className="flex items-center gap-2 text-emerald-500">
            <div className="w-1.5 h-1.5 rounded-full bg-emerald-500"></div>
            Systems Operational
          </div>
        </div>
      </footer>
    </div>
  );
};

export default LandingPage;
