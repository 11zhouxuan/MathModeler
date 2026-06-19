'use client';

import { useCallback, useEffect, useState } from 'react';
import { useSearchParams, useRouter } from 'next/navigation';
import { Loader2Icon, SigmaIcon } from 'lucide-react';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { AppSidebar } from '@/components/app-sidebar';
import { Chat } from '@/components/chat';
import { useAuth } from '@/lib/auth';
import {
  listSessionsAsync,
  loadMessagesAsync,
  newSessionId,
  deleteSession,
  type SessionMeta,
} from '@/lib/history';
import type { ChatMessage } from '@/lib/types';

// ---------------------------------------------------------------------------
// Password login gate (MathModeler /api/login). Shown until authenticated.
// ---------------------------------------------------------------------------
function LoginGate() {
  const auth = useAuth();
  const [user, setUser] = useState('admin');
  const [pass, setPass] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    await auth.login(user, pass);
    setBusy(false);
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-background p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-border bg-card p-8 shadow-lg"
      >
        <div className="mb-1 flex items-center gap-2 font-semibold text-lg">
          <span className="flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <SigmaIcon className="size-5" />
          </span>
          MathModeler
        </div>
        <p className="mb-6 text-muted-foreground text-sm">
          数学建模智能体控制台 · 基于 Amazon Bedrock AgentCore
        </p>
        <label className="mb-1 block text-sm" htmlFor="u">
          管理员账号
        </label>
        <input
          id="u"
          value={user}
          onChange={(e) => setUser(e.target.value)}
          autoComplete="username"
          className="mb-3 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary"
        />
        <label className="mb-1 block text-sm" htmlFor="p">
          访问密码
        </label>
        <input
          id="p"
          type="password"
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          autoComplete="current-password"
          placeholder="请输入部署时配置的密码"
          className="mb-4 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary"
        />
        {auth.error && (
          <div className="mb-3 text-destructive text-sm" role="alert">
            {auth.error}
          </div>
        )}
        <button
          type="submit"
          disabled={busy}
          className="flex w-full items-center justify-center gap-2 rounded-lg bg-primary py-2 font-medium text-primary-foreground text-sm disabled:opacity-50"
        >
          {busy && <Loader2Icon className="size-4 animate-spin" />}
          {busy ? '验证中…' : '进入控制台'}
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Authenticated console: sidebar (server-side history) + chat + pipeline.
// URL parameter ?session=mm-xxx loads an existing session directly.
// ---------------------------------------------------------------------------
function Console() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [currentId, setCurrentId] = useState<string>('');
  const [initial, setInitial] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(true);

  // Load sessions from server on mount; handle ?session= URL param.
  useEffect(() => {
    (async () => {
      setLoading(true);
      const list = await listSessionsAsync();
      setSessions(list);

      const urlSession = searchParams.get('session');
      if (urlSession && urlSession.startsWith('mm-')) {
        // Load existing session from URL
        setCurrentId(urlSession);
        const msgs = await loadMessagesAsync(urlSession);
        setInitial(msgs as ChatMessage[]);
      } else {
        // Start a new session and push its ID into the URL
        const id = newSessionId();
        setCurrentId(id);
        setInitial([]);
        router.replace(`/?session=${id}`, { scroll: false });
      }
      setLoading(false);
    })();
  }, [searchParams]);

  const refresh = useCallback(async () => {
    const list = await listSessionsAsync();
    setSessions(list);
  }, []);

  const startNew = useCallback(() => {
    const id = newSessionId();
    setCurrentId(id);
    setInitial([]);
    // Update URL without full reload
    router.push(`/?session=${id}`, { scroll: false });
  }, [router]);

  const pick = useCallback(async (id: string) => {
    if (id === currentId) return;
    setCurrentId(id);
    setLoading(true);
    const msgs = await loadMessagesAsync(id);
    setInitial(msgs as ChatMessage[]);
    setLoading(false);
    // Update URL
    router.push(`/?session=${id}`, { scroll: false });
  }, [currentId, router]);

  const remove = useCallback((id: string) => {
    setSessions(deleteSession(id));
    if (id === currentId) startNew();
  }, [currentId, startNew]);

  if (loading || !currentId) {
    return (
      <div className="flex h-dvh items-center justify-center">
        <Loader2Icon className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <SidebarProvider defaultOpen>
      <AppSidebar
        sessions={sessions}
        currentId={currentId}
        onNew={startNew}
        onPick={pick}
        onDelete={remove}
      />
      <SidebarInset>
        <Chat
          key={currentId}
          id={currentId}
          initialMessages={initial}
          onPersisted={refresh}
        />
      </SidebarInset>
    </SidebarProvider>
  );
}

export default function Page() {
  const auth = useAuth();
  if (auth.isLoading) return <div className="flex h-dvh" />;
  if (!auth.isAuthenticated) return <LoginGate />;
  return <Console />;
}
