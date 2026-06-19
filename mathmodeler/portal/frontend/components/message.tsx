'use client';

import { memo } from 'react';
import {
  BotIcon,
  CheckIcon,
  ExternalLinkIcon,
  FileTextIcon,
  Loader2Icon,
  UserIcon,
} from 'lucide-react';
import type { ChatMessage } from '@/lib/types';
import { cn } from '@/lib/utils';
import { Message, MessageContent } from '@/components/ai-elements/message';
import {
  Tool,
  ToolHeader,
  ToolContent,
  ToolInput,
  ToolOutput,
} from '@/components/ai-elements/tool';
import { Response } from './response';

// ---------------------------------------------------------------------------
// Agent identity (supervisor + four sub-agents). Drives the left avatar/label.
// ---------------------------------------------------------------------------
const AGENT_META: Record<string, { label: string; en: string; tint: string }> =
  {
    supervisor: { label: '总控', en: 'SUPERVISOR', tint: 'text-violet-500' },
    analyst: { label: '分析', en: 'ANALYST', tint: 'text-sky-500' },
    modeler: { label: '建模', en: 'MODELER', tint: 'text-emerald-500' },
    solver: { label: '求解', en: 'SOLVER', tint: 'text-amber-500' },
    reporter: { label: '报告', en: 'REPORTER', tint: 'text-rose-500' },
  };

function agentMeta(agent: string) {
  return AGENT_META[agent] || { label: agent, en: agent.toUpperCase(), tint: 'text-muted-foreground' };
}

const STAGE_LABEL: Record<string, string> = {
  analysis: '问题分析',
  modeling: '数学建模',
  solving: '计算求解',
  report: '方案报告',
  planning: '规划下一步',
};

// A "bubble" = a contiguous run of parts attributed to ONE agent. We split the
// assistant message by the `data-agent-marker` cursor, then render each bubble
// with its own avatar/label gutter using agent-craft's Message components.
type Bubble = { agent: string; key: string; nodes: React.ReactNode[] };

function UserGutter() {
  return (
    <div className="flex shrink-0 flex-col items-center gap-1 pt-1 w-12">
      <span className="flex size-9 items-center justify-center rounded-full border border-border bg-primary/10 text-primary">
        <UserIcon className="size-5" />
      </span>
      <span className="text-[10px] font-semibold uppercase leading-none tracking-wide text-muted-foreground">
        YOU
      </span>
    </div>
  );
}

function AgentGutter({ agent }: { agent: string }) {
  const m = agentMeta(agent);
  return (
    <div className="flex shrink-0 flex-col items-center gap-1 -mt-1 w-12">
      <span
        className={cn(
          'flex size-8 items-center justify-center rounded-full border border-border bg-card',
          m.tint,
        )}
      >
        <BotIcon className="size-4" />
      </span>
      <span className="text-[9px] font-semibold uppercase leading-none tracking-wide text-muted-foreground">
        {m.en}
      </span>
    </div>
  );
}

// Render a single part to a React node (text / tool / stage / ask / final).
function renderPart(part: any, key: string): React.ReactNode {
  const type: string = part?.type || '';

  if (type === 'text') {
    if (!part.text) return null;
    return <Response key={key}>{part.text}</Response>;
  }

  if (type.startsWith('tool-') || type === 'dynamic-tool') {
    const name =
      type === 'dynamic-tool' ? part.toolName : type.slice('tool-'.length);
    // ask_user is surfaced as data-ask, never as a tool card.
    if (name === 'ask_user') return null;
    const state = (part.state || 'input-available') as any;
    // Use `description` from tool input (or part.title) as the display title.
    // For run_subagent, use `input.name` (the sub-agent name) as fallback.
    const rawDesc: string =
      part.title ||
      (typeof part.input === 'object' && (part.input?.description || part.input?.name)) ||
      '';
    const truncatedDesc = rawDesc.length > 10 ? rawDesc.slice(0, 10) + '…' : rawDesc;
    const displayTitle = truncatedDesc ? `${name}: ${truncatedDesc}` : name;
    const fullTitle = rawDesc ? `${name}: ${rawDesc}` : name;
    return (
      <Tool key={key} className="group" defaultOpen={state === 'output-error'} title={fullTitle}>
        <ToolHeader title={displayTitle} type={`tool-${name}` as any} state={state} />
        <ToolContent>
          <ToolInput input={part.input && Object.keys(part.input).length > 0 ? part.input : undefined} />
          <ToolOutput output={part.output} errorText={part.errorText} />
        </ToolContent>
      </Tool>
    );
  }

  if (type === 'data-stage') {
    const d = part.data || {};
    const label = STAGE_LABEL[d.stage || ''] || d.stage || d.agent || 'stage';
    const done = d.status === 'done';
    return (
      <div
        key={key}
        className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
      >
        {done ? (
          <CheckIcon className="size-3.5 text-emerald-500" />
        ) : (
          <Loader2Icon className="size-3.5 animate-spin text-amber-500" />
        )}
        <span className="font-medium text-foreground">{label}</span>
        <span>{done ? '已完成' : '进行中'}</span>
        {Array.isArray(d.order) && d.order.length ? (
          <span className="font-mono">· {d.order.join(' → ')}</span>
        ) : null}
        {d.task_id ? <span className="font-mono">· 任务 {d.task_id}</span> : null}
        {d.method ? <span>· {d.method}</span> : null}
        {typeof d.success === 'boolean' ? (
          <span>
            · {d.success ? '成功' : '失败'}（{d.attempts ?? 0} 次尝试）
          </span>
        ) : null}
      </div>
    );
  }

  if (type === 'data-ask') {
    const q = part.data?.question;
    if (!q) return null;
    const inputType = part.data?.inputType || 'text';
    return (
      <div key={key} className="flex flex-col gap-2">
        <Response>{String(q)}</Response>
        {inputType === 'confirm' && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
              onClick={() => {
                document.dispatchEvent(new CustomEvent('mm-quick-reply', { detail: '确认' }));
              }}
            >
              ✓ 确认
            </button>
          </div>
        )}
      </div>
    );
  }

  if (type === 'data-final') {
    const url = part.data?.report_url;
    if (!url) return null;
    return (
      <a
        key={key}
        className="inline-flex w-fit items-center gap-1.5 rounded-md border border-border bg-card px-3 py-2 text-sm text-primary hover:bg-accent"
        href={url}
        target="_blank"
        rel="noreferrer"
      >
        <FileTextIcon className="size-4" /> 查看原始报告 / View report
        <ExternalLinkIcon className="size-3" />
      </a>
    );
  }

  // Legacy data-agent (nested parts from old streaming format): render inline.
  if (type === 'data-agent') {
    const nested = part.data?.parts || [];
    if (nested.length === 0) return null;
    return (
      <div key={key} className="flex flex-col gap-2">
        {nested.map((np: any, ni: number) => {
          const r = renderPart(
            np.type === 'text'
              ? np
              : { ...np, type: np.type || `tool-${np.toolCallId || 'unknown'}` },
            `${key}-${ni}`,
          );
          return r;
        })}
      </div>
    );
  }

  // data-agent-marker / data-session carry no direct visual content.
  return null;
}


function groupByAgent(parts: any[]): Bubble[] {
  const bubbles: Bubble[] = [];
  let current = 'supervisor';
  let seq = 0;
  let open: Bubble | null = null;

  // Pre-scan: find the latest status for each stage so we can skip rendering
  // stale "start" parts when a "done" part exists for the same stage.
  const latestStageStatus: Record<string, string> = {};
  for (const p of parts || []) {
    if (p?.type === 'data-stage' && p.data?.stage && p.data?.status) {
      latestStageStatus[p.data.stage] = p.data.status;
    }
  }

  const flush = () => {
    if (open && open.nodes.length) bubbles.push(open);
    open = null;
  };

  for (let i = 0; i < (parts || []).length; i++) {
    const p = parts[i];
    const t = p?.type || '';
    if (t === 'data-agent-marker') {
      const next = p.data?.agent || 'supervisor';
      if (next !== current) {
        flush();
        current = next;
      }
      continue;
    }
    // Legacy data-agent: switch agent from nested data and render its parts.
    if (t === 'data-agent') {
      const next = p.data?.agent || p.data?.name || 'supervisor';
      if (next !== current) {
        flush();
        current = next;
      }
      // Don't skip — let renderPart handle the nested parts rendering.
    }
    if (t === 'data-session') continue;

    // Skip stale data-stage "start" if a "done" exists for the same stage.
    if (t === 'data-stage' && p.data?.stage && p.data?.status === 'start') {
      if (latestStageStatus[p.data.stage] === 'done') continue;
    }

    const node = renderPart(p, `${i}`);
    if (node == null) continue;
    if (!open || open.agent !== current) {
      flush();
      open = { agent: current, key: `${current}-${seq++}`, nodes: [] };
    }
    open.nodes.push(node);
  }
  flush();
  return bubbles;
}

function PurePreviewMessage({
  message,
}: {
  message: ChatMessage;
  isLoading?: boolean;
  requiresScrollPadding?: boolean;
}) {
  if (message.role === 'user') {
    const text = (message.parts || [])
      .filter((p: any) => p.type === 'text')
      .map((p: any) => p.text)
      .join('');
    return (
      <div className="flex w-full gap-3">
        <UserGutter />
        <Message from="user" className="min-w-0 flex-1">
          <MessageContent>{text}</MessageContent>
        </Message>
      </div>
    );
  }

  const bubbles = groupByAgent(message.parts as any[]);
  if (bubbles.length === 0) return null;

  return (
    <>
      {bubbles.map((b) => (
        <div key={b.key} className="flex w-full gap-3">
          <AgentGutter agent={b.agent} />
          <Message from="assistant" className="min-w-0 flex-1">
            <MessageContent>
              <div className="flex flex-col gap-2">{b.nodes}</div>
            </MessageContent>
          </Message>
        </div>
      ))}
    </>
  );
}

export const PreviewMessage = memo(PurePreviewMessage, (prev, next) => {
  // The currently-streaming message must always re-render to show incremental parts.
  if (next.isLoading) return false;
  // For completed messages, skip re-render if the message object is unchanged.
  return prev.message === next.message;
});

export function ThinkingMessage() {
  return (
    <div className="flex items-center gap-2 px-1 text-sm text-muted-foreground">
      <Loader2Icon className="size-4 animate-spin" /> 智能体协同求解中… / Agents
      working…
    </div>
  );
}
