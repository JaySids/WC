import React, { useState } from 'react';

interface PasswordGateProps {
  onAuthenticated: () => void;
}

const PasswordGate: React.FC<PasswordGateProps> = ({ onAuthenticated }) => {
  const [password, setPassword] = useState('');
  const [error, setError] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (password === 'AQ_takehome!') {
      sessionStorage.setItem('aq_authenticated', 'true');
      onAuthenticated();
    } else {
      setError(true);
      setTimeout(() => setError(false), 1500);
    }
  };

  return (
    <div className="min-h-screen bg-[#050505] flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="flex items-center justify-center gap-2 font-mono text-sm tracking-tight mb-6">
            <span className="text-gray-500">/</span>
            <span className="font-semibold text-white">clone.ai</span>
          </div>
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-white/5 border border-white/10 text-[10px] font-mono text-emerald-400 mb-4">
            <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
            <span>access required</span>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="bg-[#0c0c0e] border border-white/10 rounded-lg p-2 flex items-center gap-3">
            <span className="pl-3 text-gray-600 font-mono text-sm">{'>'}</span>
            <input
              type="password"
              value={password}
              onChange={(e) => { setPassword(e.target.value); setError(false); }}
              placeholder="enter password"
              autoFocus
              className="flex-1 bg-transparent border-none focus:ring-0 text-gray-300 font-mono text-sm placeholder-gray-700 outline-none"
            />
            <button
              type="submit"
              className="px-5 py-2 bg-white text-black font-mono text-xs font-bold uppercase tracking-wider rounded-md hover:bg-gray-200 transition-colors"
            >
              Enter
            </button>
          </div>

          {error && (
            <div className="text-center text-xs font-mono text-red-400 animate-pulse">
              incorrect password
            </div>
          )}
        </form>
      </div>
    </div>
  );
};

export default PasswordGate;
