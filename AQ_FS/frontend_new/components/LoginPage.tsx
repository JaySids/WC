import React, { useState } from 'react';
import { supabase } from '../lib/supabase';

interface LoginPageProps {
  onBack: () => void;
  onSuccess: () => void;
}

const LoginPage: React.FC<LoginPageProps> = ({ onBack, onSuccess }) => {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isSignUp, setIsSignUp] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (isSignUp) {
        const { error } = await supabase.auth.signUp({ email, password });
        if (error) throw error;
        setError('Check your email for a confirmation link.');
      } else {
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) throw error;
        onSuccess();
      }
    } catch (err: any) {
      setError(err.message || 'Authentication failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#050505] text-white font-display flex flex-col">
      {/* Nav */}
      <nav className="flex items-center justify-between px-6 md:px-8 py-6 border-b border-white/5">
        <button
          onClick={onBack}
          className="flex items-center gap-2 font-mono text-sm tracking-tight text-gray-400 hover:text-white transition-colors"
        >
          <span className="material-symbols-outlined text-sm">arrow_back</span>
          <span className="text-gray-500">/</span>
          <span className="font-semibold text-white">clone.ai</span>
        </button>
      </nav>

      {/* Login Form */}
      <div className="flex-1 flex items-center justify-center px-4">
        <div className="w-full max-w-sm">
          <div className="text-center mb-10">
            <h1 className="text-3xl font-medium tracking-tight mb-3">
              {isSignUp ? 'Create account' : 'Welcome back'}
            </h1>
            <p className="text-sm text-gray-500 font-mono">
              {isSignUp ? 'Sign up to start cloning' : 'Sign in to your account'}
            </p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-[11px] font-mono text-gray-500 uppercase tracking-wider mb-2">
                Email
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full bg-[#0c0c0e] border border-white/10 rounded-lg py-3 px-4 text-sm font-mono text-gray-300 placeholder-gray-700 focus:outline-none focus:border-white/20 transition-colors"
                placeholder="you@example.com"
                required
              />
            </div>

            <div>
              <label className="block text-[11px] font-mono text-gray-500 uppercase tracking-wider mb-2">
                Password
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full bg-[#0c0c0e] border border-white/10 rounded-lg py-3 px-4 text-sm font-mono text-gray-300 placeholder-gray-700 focus:outline-none focus:border-white/20 transition-colors"
                placeholder="••••••••"
                required
                minLength={6}
              />
            </div>

            {error && (
              <div
                className={`text-xs font-mono px-3 py-2 rounded border ${
                  error.includes('Check your email')
                    ? 'text-emerald-400 bg-emerald-900/20 border-emerald-800/30'
                    : 'text-red-400 bg-red-900/20 border-red-800/30'
                }`}
              >
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-3 bg-white text-black font-mono text-xs font-bold uppercase tracking-wider rounded-lg hover:bg-gray-200 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? 'Processing...' : isSignUp ? 'Create Account' : 'Sign In'}
            </button>
          </form>

          <div className="mt-6 text-center">
            <button
              onClick={() => {
                setIsSignUp(!isSignUp);
                setError('');
              }}
              className="text-xs font-mono text-gray-500 hover:text-white transition-colors"
            >
              {isSignUp
                ? 'Already have an account? Sign in'
                : "Don't have an account? Sign up"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default LoginPage;
