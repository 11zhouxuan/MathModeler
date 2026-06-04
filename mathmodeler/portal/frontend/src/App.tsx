import { useEffect, useMemo, useRef, useState } from 'react';
import { useChat } from '@ai-sdk/react';
import { DefaultChatTransport } from 'ai';
import { marked } from 'marked';
import renderMathInElement from 'katex/contrib/auto-render';

// ---------------------------------------------------------------------------
// Types for our custom AI SDK v6 data parts (data-stage / data-final / data-error).
// The backend (portal/backend/server.py) forwards the Orchestrator's internal
// four-stage SSE as these parts.
// ---------------------------------------------------------------------------
type StageEvent = {
  type: 'stage' | 'subagent';
  stage?: 'analysis' | 'modeling' | 'solving' | 'report';
  status?: 'start' | 'done';
  order?: string[];
  task_id?: string;
  method?: string;
  success?: boolean;
  attempts?: number;
};
type FinalEvent = { type: 'final'; report_key?: string; report_url?: string };
type AgentEvent = {
  agent?: string;
  chunk?: { kind: 'token' | 'result' | 'tool'; delta?: string; text?: string; tool?: string };
};
type AskEvent = { interruptId?: string; question?: string; agent?: string };
type SessionEvent = { session_id?: string };

// Map sub-agent name -> the four-stage timeline key.
const AGENT_STAGE: Record<string, string> = {
  analyst: 'analysis', modeler: 'modeling', solver: 'solving', reporter: 'report',
};

const STAGES: { key: string; label: string; en: string }[] = [
  { key: 'analysis', label: '问题分析', en: 'Analysis' },
  { key: 'modeling', label: '数学建模', en: 'Modeling' },
  { key: 'solving', label: '计算求解', en: 'Solving' },
  { key: 'report', label: '方案报告', en: 'Report' },
];

// SVG icons (replacing emojis per ui-ux-pro-max guidelines – consistent Lucide-style set)
const Icons = {
  fire: (
    <svg className="chip-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z" />
    </svg>
  ),
  bike: (
    <svg className="chip-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="18.5" cy="17.5" r="3.5" /><circle cx="5.5" cy="17.5" r="3.5" />
      <circle cx="15" cy="5" r="1" /><path d="M12 17.5V14l-3-3 4-3 2 3h2" />
    </svg>
  ),
  hospital: (
    <svg className="chip-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 6v4" /><path d="M14 14h-4" /><path d="M14 18h-4" /><path d="M14 8h-4" />
      <path d="M18 12h2a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2h2" />
      <path d="M18 22V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v18" />
    </svg>
  ),
  arrow: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12h14" /><path d="m12 5 7 7-7 7" />
    </svg>
  ),
  external: (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15,3 21,3 21,9" /><line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  ),
  sigma: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M18 7V4H6l6 8-6 8h12v-3" />
    </svg>
  ),
  check: (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20,6 9,17 4,12" />
    </svg>
  ),
  spinner: (
    <svg className="icon-spinner" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
    </svg>
  ),
  sparkles: (
    <svg className="icon-sparkles" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" />
      <path d="M5 3v4" /><path d="M19 17v4" /><path d="M3 5h4" /><path d="M17 19h4" />
    </svg>
  ),
  logout: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16,17 21,12 16,7" /><line x1="21" y1="12" x2="9" y2="12" />
    </svg>
  ),
};

const SAMPLES = [
  { icon: Icons.fire, label: '森林火灾调度', text: '为某城市设计森林火灾应急资源调度方案，在预算约束下最小化平均响应时间与受灾面积。' },
  { icon: Icons.bike, label: '单车再平衡', text: '为城市共享单车系统设计动态调度与再平衡策略，平衡运营成本与用户可用性。' },
  { icon: Icons.hospital, label: '急诊排队', text: '为医院急诊科建立排队与资源分配模型，降低患者平均等待时间。' },
];

function useAuth() {
  const [token, setToken] = useState<string>(() => sessionStorage.getItem('mm_token') || '');
  const save = (t: string) => {
    sessionStorage.setItem('mm_token', t);
    setToken(t);
  };
  const clear = () => {
    sessionStorage.removeItem('mm_token');
    setToken('');
  };
  return { token, save, clear };
}

function LoginGate({ onLogin }: { onLogin: (t: string) => void }) {
  const [user, setUser] = useState('admin');
  const [pass, setPass] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr('');
    setBusy(true);
    try {
      const r = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, password: pass }),
      });
      if (!r.ok) {
        setErr('用户名或密码错误');
        return;
      }
      const data = await r.json();
      onLogin(data.token);
    } catch {
      setErr('网络错误，请重试');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit} aria-labelledby="gate-title">
        <div className="brand-mark">MATHMODELER</div>
        <h1 id="gate-title">数学建模智能体控制台</h1>
        <p className="gate-sub">基于 Amazon Bedrock AgentCore 的多智能体协同求解系统</p>
        <label htmlFor="login-user">管理员账号</label>
        <input id="login-user" value={user} onChange={(e) => setUser(e.target.value)} autoComplete="username" />
        <label htmlFor="login-pass">访问密码</label>
        <input
          id="login-pass"
          type="password"
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          autoComplete="current-password"
          placeholder="请输入部署时配置的密码"
        />
        {err && <div className="gate-err" role="alert">{err}</div>}
        <button type="submit" disabled={busy}>
          {busy && Icons.spinner}
          {busy ? '验证中…' : '进入控制台'}
        </button>
      </form>
    </div>
  );
}

// Report renderer with Markdown + KaTeX.
function Report({ markdown }: { markdown: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    ref.current.innerHTML = marked.parse(markdown) as string;
    renderMathInElement(ref.current, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\(', right: '\\)', display: false },
        { left: '\\[', right: '\\]', display: true },
      ],
      throwOnError: false,
    });
  }, [markdown]);
  return <div className="report-body" ref={ref} />;
}

// Empty state shown when no messages yet
function EmptyState({ onSample }: { onSample: (text: string) => void }) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{Icons.sparkles}</div>
      <h2 className="empty-title">开始数学建模</h2>
      <p className="empty-desc">
        输入一道开放式数学建模问题，四位专家智能体将协同为你求解。
        <br />选择一个示例快速开始：
      </p>
      <div className="empty-samples">
        {SAMPLES.map((s) => (
          <button key={s.label} className="empty-card" onClick={() => onSample(s.text)}>
            <span className="empty-card-icon">{s.icon}</span>
            <span className="empty-card-label">{s.label}</span>
            <span className="empty-card-desc">{s.text.slice(0, 30)}…</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function Console({ token, onSignout }: { token: string; onSignout: () => void }) {
  const [input, setInput] = useState('');
  const threadRef = useRef<HTMLDivElement>(null);
  // Session id + pending HITL interrupt id are carried in refs so the transport's
  // request builder can inject them on the NEXT POST (resume turn).
  const sessionRef = useRef<string>('');
  const resumeRef = useRef<{ interruptId: string } | null>(null);

  const transport = useMemo(
    () =>
      new DefaultChatTransport({
        api: '/api/chat',
        headers: { Authorization: 'Bearer ' + token },
        // Inject the persistent session_id (so a paused ask_user resumes the SAME
        // Supervisor) and, on a resume turn, the interruptResponses payload.
        prepareSendMessagesRequest: ({ body, messages: msgs }: any) => {
          const extra: any = { session_id: sessionRef.current || undefined };
          const pending = resumeRef.current;
          if (pending) {
            const last = [...(msgs || [])].reverse().find((m: any) => m.role === 'user');
            const ans = ((last?.parts || [])
              .filter((p: any) => p.type === 'text')
              .map((p: any) => p.text)
              .join('') || '').trim();
            extra.interruptResponses = [
              { interruptResponse: { interruptId: pending.interruptId, response: ans } },
            ];
            resumeRef.current = null;
          }
          return { body: { ...body, ...extra } };
        },
      }),
    [token],
  );
  const { messages, sendMessage, status, error } = useChat({ transport });

  // Auto-scroll to bottom when messages change (streaming)
  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight;
    }
  }, [messages, status]);

  // If the session token is stale/invalid (e.g. the portal task was redeployed
  // and its in-memory token store reset), the chat request returns 401
  // "unauthorized". Detect that and bounce back to the login gate so the user
  // can re-authenticate instead of being stuck on a dead console.
  useEffect(() => {
    if (!error) return;
    const msg = String((error as any)?.message || error || '');
    if (msg.includes('unauthorized') || msg.includes('401')) {
      onSignout();
    }
  }, [error, onSignout]);

  // Derive timeline + live sub-agent tokens + report + pending HITL ask from the
  // streamed message parts.
  const { stageState, subRows, agentLive, reportUrl, ask } = useMemo(() => {
    const state: Record<string, 'idle' | 'active' | 'done'> = {
      analysis: 'idle', modeling: 'idle', solving: 'idle', report: 'idle',
    };
    const rows: Record<string, string[]> = { analysis: [], modeling: [], solving: [], report: [] };
    const live: Record<string, string> = { analysis: '', modeling: '', solving: '', report: '' };
    let url = '';
    let pendingAsk: AskEvent | null = null;
    for (const m of messages) {
      for (const p of (m as any).parts || []) {
        if (p.type === 'data-session') {
          const sid = (p.data as SessionEvent)?.session_id;
          if (sid) sessionRef.current = sid;
        } else if (p.type === 'data-stage') {
          const ev = p.data as StageEvent;
          const s = ev.stage;
          if (!s) continue;
          if (ev.status === 'start' && state[s] === 'idle') state[s] = 'active';
          if (ev.status === 'done') {
            state[s] = 'done';
            if (s === 'analysis' && ev.order) {
              rows.analysis.push('子任务顺序 ' + ev.order.join(' → '));
              if (state.modeling === 'idle') state.modeling = 'active';
            }
            if (s === 'modeling' && ev.task_id) rows.modeling.push(`任务 ${ev.task_id} · ${ev.method || 'model'}`);
            if (s === 'solving' && ev.task_id)
              rows.solving.push(`任务 ${ev.task_id} · ${ev.success ? '成功' : '失败'} · ${ev.attempts ?? 0} 次尝试`);
          }
        } else if (p.type === 'data-agent') {
          // Live sub-agent progress: accumulate token deltas under its stage.
          const ev = p.data as AgentEvent;
          const stage = AGENT_STAGE[ev.agent || ''] || '';
          if (stage) {
            if (state[stage] === 'idle') state[stage] = 'active';
            const c = ev.chunk;
            if (c?.kind === 'token' && c.delta) live[stage] = (live[stage] + c.delta).slice(-400);
            else if (c?.kind === 'tool' && c.tool) live[stage] = `调用工具：${c.tool}`;
          }
        } else if (p.type === 'data-ask') {
          // HITL: the supervisor (or a sub-agent) is waiting for the user.
          pendingAsk = p.data as AskEvent;
        } else if (p.type === 'data-final') {
          url = (p.data as FinalEvent).report_url || '';
          state.report = 'done';
        }
      }
    }
    return { stageState: state, subRows: rows, agentLive: live, reportUrl: url, ask: pendingAsk };
  }, [messages]);

  // When a fresh ask arrives, remember its interrupt id so the next user message
  // is sent as an interruptResponses resume (handled in the transport builder).
  useEffect(() => {
    if (ask?.interruptId) {
      resumeRef.current = { interruptId: ask.interruptId };
    }
  }, [ask]);

  // Assistant text (the streamed final report markdown).
  const reportMarkdown = useMemo(() => {
    const last = [...messages].reverse().find((m: any) => m.role === 'assistant');
    if (!last) return '';
    return ((last as any).parts || [])
      .filter((p: any) => p.type === 'text')
      .map((p: any) => p.text)
      .join('');
  }, [messages]);

  const busy = status === 'streaming' || status === 'submitted';
  const hasMessages = messages.length > 0;

  const submit = () => {
    const text = input.trim();
    if (!text || busy) return;
    sendMessage({ text });
    setInput('');
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="logo">
          <span className="logo-icon">{Icons.sigma}</span>
          Math<em>Modeler</em>
        </div>
        <button className="signout" onClick={onSignout} aria-label="退出登录">
          {Icons.logout}
          <span className="signout-text">退出</span>
        </button>
      </header>

      <div className="layout">
        {/* Left: chat */}
        <main className="chat">
          {!hasMessages && !busy && (
            <EmptyState onSample={(text) => { setInput(text); }} />
          )}

          {(hasMessages || busy) && (
            <>
              <div className="thread" ref={threadRef} role="log" aria-live="polite">
                {messages.map((m: any) => {
                  const text = (m.parts || []).filter((p: any) => p.type === 'text').map((p: any) => p.text).join('');
                  if (m.role === 'user') {
                    return (
                      <div key={m.id} className="bubble user">
                        {text}
                      </div>
                    );
                  }
                  if (!text) return null;
                  return (
                    <div key={m.id} className="bubble assistant">
                      <Report markdown={text} />
                    </div>
                  );
                })}
                {busy && <div className="bubble assistant thinking">智能体协同求解中…</div>}
                {error && <div className="bubble error" role="alert">错误：{String(error.message || error)}</div>}
              </div>
            </>
          )}

          <div className="composer">
            {ask && !busy && (
              <div className="ask-banner" role="alert">
                <span className="ask-tag">需要你的输入 · {ask.agent || 'supervisor'}</span>
                <span className="ask-q">{ask.question || '智能体需要更多信息。'}</span>
              </div>
            )}
            {hasMessages && !ask && (
              <div className="chips">
                {SAMPLES.map((s) => (
                  <button key={s.label} className="chip" onClick={() => setInput(s.text)} aria-label={`示例：${s.label}`}>
                    {s.icon} {s.label}
                  </button>
                ))}
              </div>
            )}
            <label htmlFor="chat-input" className="sr-only">输入数学建模问题</label>
            <textarea
              id="chat-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                ask
                  ? '回答智能体的问题后按 Ctrl+Enter 继续……'
                  : '输入数学建模问题，按 Ctrl+Enter 发送……'
              }
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit();
              }}
              rows={3}
            />
            <div className="composer-actions">
              <span className="composer-hint">Ctrl + Enter 发送</span>
              <button className="send" onClick={submit} disabled={busy} aria-label={busy ? '求解中' : '发送'}>
                {busy && Icons.spinner}
                {busy ? '求解中…' : ask ? '提交回答' : '开始求解'}
                {!busy && Icons.arrow}
              </button>
            </div>
          </div>
        </main>

        {/* Right: pipeline timeline */}
        <aside className="pipeline" aria-label="协同流水线">
          <div className="eyebrow">Pipeline · 协同流水线</div>
          {STAGES.map((st) => (
            <div key={st.key} className={`stage ${stageState[st.key]}`} aria-label={`${st.label} - ${stageState[st.key] === 'idle' ? '等待中' : stageState[st.key] === 'active' ? '进行中' : '已完成'}`}>
              <div className="stage-head">
                <span className="dot">
                  {stageState[st.key] === 'done' && Icons.check}
                </span>
                <span className="stage-label">{st.label}</span>
                <span className="stage-en">{st.en}</span>
              </div>
              {subRows[st.key].map((r, i) => (
                <div key={i} className="sub-row">
                  {r}
                </div>
              ))}
              {stageState[st.key] === 'active' && agentLive[st.key] && (
                <div className="sub-row live">{agentLive[st.key]}</div>
              )}
            </div>
          ))}

          {reportUrl && (
            <a className="report-link" href={reportUrl} target="_blank" rel="noreferrer">
              查看原始报告 {Icons.external}
            </a>
          )}
        </aside>
      </div>
    </div>
  );
}

export default function App() {
  const { token, save, clear } = useAuth();
  if (!token) return <LoginGate onLogin={save} />;
  return <Console token={token} onSignout={clear} />;
}
