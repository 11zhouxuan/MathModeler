import type { UIMessage } from 'ai';
import { z } from 'zod';

export type DataPart = { type: 'append-message'; message: string };

export const messageMetadataSchema = z.object({
  createdAt: z.string().optional(),
});

export type MessageMetadata = z.infer<typeof messageMetadataSchema>;

// Custom AI SDK v6 data-part payloads emitted by the MathModeler portal /
// orchestrator. Kept intentionally small after stripping agent-craft's
// artifact / sandbox / vote machinery.
export type CustomUIDataTypes = {
  // The whole run is ONE assistant UIMessage. A lightweight `data-agent-marker`
  // part is emitted ONLY when the output author switches (supervisor <-> a
  // sub-agent); the frontend keeps a `currentAgent` cursor and tags each
  // following text/tool bubble with it. (design: replaces groupAssistantParts)
  'agent-marker': { agent: string; stage?: string };
  // Four-stage timeline marker (analysis | modeling | solving | report).
  stage: {
    stage?: 'analysis' | 'modeling' | 'solving' | 'report';
    status?: 'start' | 'done';
    agent?: string;
    order?: string[];
    task_id?: string;
    method?: string;
    success?: boolean;
    attempts?: number;
  };
  // Task progress panel (DAG): emitted by the update_task tool.
  task: {
    tasks: TaskItem[];
  };
  // HITL clarifying question (ask_user by supervisor or a sub-agent).
  ask: { interruptId?: string; question?: string; agent?: string };
  // Final report marker.
  final: { report_key?: string; report_url?: string; text?: string };
  // Session id handshake (frontend captures it for HITL resume).
  session: { session_id?: string };
};

export type TaskItem = {
  id: string;
  title: string;
  status: 'idle' | 'active' | 'done';
  deps: string[];
};

export type ChatMessage = UIMessage<MessageMetadata, CustomUIDataTypes>;

export type Attachment = {
  name: string;
  url: string;
  contentType: string;
};
