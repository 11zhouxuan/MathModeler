'use client';

// MathModeler password-gate auth. POST /api/login -> HMAC signed bearer token.
// Global 401 interceptor: any /api/* 401 -> auto logout -> login page.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

const TOKEN_KEY = 'mm_token';

// In dev mode, bypass Next.js proxy (Turbopack doesn't stream SSE properly).
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || '';

type AuthUser = { access_token: string; profile: { sub: string } };

type AuthContextValue = {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string;
  login: (user: string, password: string) => Promise<boolean>;
  logout: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

function readToken(): string {
  if (typeof window === 'undefined') return '';
  try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string>('');
  const [hydrated, setHydrated] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setToken(readToken());
    setHydrated(true);
  }, []);

  // Global 401 interceptor: monkey-patch fetch so ANY /api/* returning 401
  // automatically clears the token -> page re-renders to LoginGate.
  useEffect(() => {
    const originalFetch = window.fetch;
    window.fetch = async (...args: Parameters<typeof fetch>) => {
      const response = await originalFetch(...args);
      if (response.status === 401) {
        const url = typeof args[0] === 'string' ? args[0] : (args[0] as Request).url;
        if (url.includes('/api/') && !url.includes('/api/login')) {
          try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* */ }
          setToken('');
        }
      }
      return response;
    };
    return () => { window.fetch = originalFetch; };
  }, []);

  const login = useCallback(async (user: string, password: string) => {
    setError('');
    try {
      const r = await fetch(`${API_BASE}/api/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, password }),
      });
      if (!r.ok) {
        setError('用户名或密码错误 / Invalid credentials');
        return false;
      }
      const t = (await r.json()).token as string;
      try { sessionStorage.setItem(TOKEN_KEY, t); } catch { /* */ }
      setToken(t);
      return true;
    } catch {
      setError('网络错误，请重试 / Network error');
      return false;
    }
  }, []);

  const logout = useCallback(() => {
    try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* */ }
    setToken('');
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: token ? { access_token: token, profile: { sub: 'admin' } } : null,
      isAuthenticated: !!token,
      isLoading: !hydrated,
      error,
      login,
      logout,
    }),
    [token, hydrated, error, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    return {
      user: null, isAuthenticated: false, isLoading: false, error: '',
      login: async () => false, logout: () => {},
    };
  }
  return ctx;
}
