'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport } from 'ai';
import { SendIcon, Loader2Icon, SquareIcon } from 'lucide-react';

import { useAuth } from '@/lib/auth';
import type { ChatMessage, TaskItem } from '@/lib/types';
import { saveSession, newSessionId } from '@/lib/history';
import { SAMPLES } from '@/lib/samples';
import { ChatHeader } from './chat-header';
import { Messages } from './messages';
import { PipelinePanel } from './pipeline-panel';

type Ask = { interruptId?: string; question?: string; agent?: string };

export function Chat({
  id,
  initialMessages,
  onPersisted,
}: {
  id: string;
  initialMessages: ChatMessage[];
  onPersisted?: () => void;
}) {
  const auth = useAuth();
  const token = auth.user?.access_token || '';
  const [input, setInput] = useState('');
  const resumeRef = useRef<{ interruptId: string } | null>(null);
  // Stable, frontend-owned session id (the backend reuses any session_id it gets).
  const sessionRef = useRef<string>(id || newSessionId());

  useEffect(() => {
    sessionRef.current = id;
    resumeRef.current = null;
  }, [id]);

  // In dev mode, bypass Next.js proxy (Turbopack doesn't stream SSE properly).
  // NEXT_PUBLIC_API_BASE_URL defaults to '' (same-origin, used in production static build).
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || '';

  const transport = useMemo(
    () =>
      new DefaultChatTransport({
        api: `${apiBase}/api/chat`,
        headers: { Authorization: 'Bearer ' + token },
        prepareSendMessagesRequest: ({
          body,
          messages,
          id: reqId,
          trigger,
          messageId,
        }: any) => {
          const extra: any = { session_id: sessionRef.current || undefined };
          const pending = resumeRef.current;
          if (pending) {
            const last = [...(messages || [])]
              .reverse()
              .find((m: any) => m.role === 'user');
            const ans = (
              (last?.parts || [])
                .filter((p: any) => p.type === 'text')
                .map((p: any) => p.text)
                .join('') || ''
            ).trim();
            extra.interruptResponses = [
              {
                interruptResponse: {
                  interruptId: pending.interruptId,
                  response: ans,
                },
              },
            ];
            resumeRef.current = null;
          }
          return {
            body: { ...body, messages, id: reqId, trigger, messageId, ...extra },
          };
        },
      }),
    [token],
  );

  const { messages, sendMessage, status, error, stop } = useChat<ChatMessage>({
    id,
    messages: initialMessages,
    transport,
  });


  // Capture the backend session id + derive the task list + HITL ask.
  const { taskList, ask } = useMemo(() => {
    let pendingAsk: Ask | null = null;
    let tasks: TaskItem[] = [];
    for (const m of messages as any[]) {
      for (const p of m.parts || []) {
        if (p.type === 'data-session') {
          const sid = p.data?.session_id;
          if (sid) sessionRef.current = sid;
        } else if (p.type === 'data-task') {
          // Always use the LATEST data-task part (full replacement).
          const t = p.data?.tasks;
          if (Array.isArray(t) && t.length > 0) tasks = t;
        } else if (p.type === 'data-ask') {
          pendingAsk = p.data as Ask;
        } else if (p.type === 'data-final') {
          // Mark all tasks as done when final is received.
          tasks = tasks.map((t: any) => ({ ...t, status: 'done' }));
        }
      }
    }
    return { taskList: tasks, ask: pendingAsk };
  }, [messages]);

  useEffect(() => {
    if (ask?.interruptId) resumeRef.current = { interruptId: ask.interruptId };
  }, [ask]);

  const busy = status === 'streaming' || status === 'submitted';

  // Listen for quick-reply events from confirm buttons in message bubbles.
  useEffect(() => {
    const handler = (e: Event) => {
      const text = (e as CustomEvent).detail;
      if (text && !busy) {
        sendMessage({ text });
      }
    };
    document.addEventListener('mm-quick-reply', handler);
    return () => document.removeEventListener('mm-quick-reply', handler);
  }, [sendMessage, busy]);

  // Persist to localStorage history when the conversation grows (and idle).
  useEffect(() => {
    if (busy || messages.length === 0) return;
    saveSession(id, messages as any[]);
    onPersisted?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, busy, id]);

  const submit = () => {
    const text = input.trim();
    if (!text || busy) return;
    sendMessage({ text });
    setInput('');
  };

  return (
    <div className="flex h-dvh min-w-0 flex-1 flex-row overflow-hidden">
      <div className="flex min-w-0 flex-1 flex-col bg-background">
        <ChatHeader />

        <Messages
          chatId={id}
          status={status}
          messages={messages}
          setMessages={() => {}}
          regenerate={(() => {}) as any}
          isReadonly={false}
          onSample={(t) => setInput(t)}
        />

        <div className="mx-auto w-full max-w-3xl px-4 pb-4 md:pb-6">
          {messages.length > 0 && !ask && (
            <div className="mb-2 flex flex-wrap gap-2">
              {SAMPLES.map((s) => (
                <button
                  key={s.label}
                  type="button"
                  onClick={() => setInput(s.text)}
                  className="rounded-full border border-border bg-card px-3 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent"
                >
                  {s.label}
                </button>
              ))}
            </div>
          )}
          <div className="flex flex-col gap-2 rounded-2xl border border-border bg-card p-2 shadow-sm">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                ask
                  ? '回答智能体的问题后按 Ctrl/⌘+Enter 继续……'
                  : '输入数学建模问题，按 Ctrl/⌘+Enter 发送……'
              }
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  submit();
                }
              }}
              rows={3}
              className="w-full resize-none bg-transparent px-2 py-1 text-sm outline-none placeholder:text-muted-foreground"
            />
            <div className="flex items-center justify-between px-1">
              <span className="text-xs text-muted-foreground">
                Ctrl / ⌘ + Enter 发送
              </span>
              <div className="flex items-center gap-2">
                {busy && (
                  <button
                    type="button"
                    onClick={() => {
                      stop();
                      // Fire-and-forget cancel to orchestrator (agent.stop()).
                      fetch(`${apiBase}/api/cancel`, {
                        method: 'POST',
                        headers: {
                          'Content-Type': 'application/json',
                          Authorization: 'Bearer ' + token,
                        },
                        body: JSON.stringify({ session_id: sessionRef.current }),
                      }).catch(() => {});
                    }}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-destructive/50 bg-destructive/10 px-3 py-1.5 font-medium text-destructive text-sm hover:bg-destructive/20"
                  >
                    <SquareIcon className="size-3.5" />
                    停止
                  </button>
                )}
                <button
                  type="button"
                  onClick={submit}
                  disabled={busy}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 font-medium text-primary-foreground text-sm disabled:opacity-50"
                >
                  {busy ? (
                    <Loader2Icon className="size-4 animate-spin" />
                  ) : (
                    <SendIcon className="size-4" />
                  )}
                  {busy ? '求解中…' : '发送'}
                </button>
              </div>

            </div>
          </div>
          {error && (
            <div
              className="mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive text-sm"
              role="alert"
            >
              错误：{String((error as any).message || error)}
            </div>
          )}
        </div>
      </div>

      <PipelinePanel tasks={taskList} sessionId={sessionRef.current} />
    </div>
  );
}
