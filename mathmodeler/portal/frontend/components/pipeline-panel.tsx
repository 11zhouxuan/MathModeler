'use client';

import { useMemo, useState, useCallback, useEffect } from 'react';
import {
  ReactFlow,
  Handle,
  type Node,
  type Edge,
  Position,
  MarkerType,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import dagre from 'dagre';
import {
  CheckIcon,
  Loader2Icon,
  CircleDotIcon,
  PanelRightCloseIcon,
  PanelRightOpenIcon,
  FolderIcon,
  FileIcon,
  DownloadIcon,
  RefreshCwIcon,
  NetworkIcon,
  FolderOpenIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import type { TaskItem } from '@/lib/types';

// ---------------------------------------------------------------------------
// Custom node component for each task in the DAG (compact: ID + status icon only)
// ---------------------------------------------------------------------------
function TaskNode({ data }: { data: { task: TaskItem } }) {
  const task = data.task;
  const isActive = task.status === 'active';
  const bgColor =
    task.status === 'done'
      ? 'bg-emerald-500 text-white border-emerald-600'
      : isActive
        ? 'bg-primary text-white border-primary animate-pulse'
        : 'bg-muted text-muted-foreground border-border';

  return (
    <div
      className={cn(
        'relative flex items-center justify-center rounded-full border-2 shadow-sm transition-colors',
        'w-10 h-10 text-xs font-bold',
        bgColor,
      )}
      title={task.title}
    >
      {/* Ripple ring for active task */}
      {isActive && (
        <span className="absolute inset-0 rounded-full border-2 border-primary animate-ping opacity-40" />
      )}
      <Handle type="target" position={Position.Top} className="!w-2 !h-2 !bg-transparent !border-0" />
      <span className="relative z-10">{task.id}</span>
      <Handle type="source" position={Position.Bottom} className="!w-2 !h-2 !bg-transparent !border-0" />
    </div>
  );
}

const nodeTypes = { task: TaskNode };

// ---------------------------------------------------------------------------
// Legend: task ID -> title mapping (shown above the DAG)
// ---------------------------------------------------------------------------
function TaskLegend({ tasks }: { tasks: TaskItem[] }) {
  return (
    <div className="px-3 py-2 border-b border-border overflow-auto max-h-[160px]">
      {tasks.map((task, idx) => (
        <div key={task.id} className="flex items-baseline gap-1.5 py-0.5">
          <span className="text-[10px] font-mono text-muted-foreground shrink-0 w-4 text-right">
            {idx + 1}
          </span>
          <span className="text-[11px] text-foreground leading-tight truncate">
            <span className="font-mono text-muted-foreground">{task.id}:</span>{' '}
            {task.title}
          </span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dagre layout helper — vertical (TB), compact circle nodes
// ---------------------------------------------------------------------------
const NODE_WIDTH = 44;
const NODE_HEIGHT = 44;

function getLayoutedElements(tasks: TaskItem[]) {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'TB', nodesep: 24, ranksep: 50 });

  for (const task of tasks) {
    g.setNode(task.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }

  // Add edges (from dep -> task) — register with BOTH dagre and ReactFlow
  const edgeStyle = { stroke: '#6b7280', strokeWidth: 2 };
  const edgeMarker = { type: MarkerType.ArrowClosed, width: 14, height: 14, color: '#6b7280' };
  const edges: Edge[] = [];
  for (const task of tasks) {
    for (const dep of task.deps || []) {
      g.setEdge(dep, task.id);
      edges.push({
        id: `${dep}->${task.id}`,
        source: dep,
        target: task.id,
        markerEnd: edgeMarker,
        style: edgeStyle,
      });
    }
  }

  // If no edges at all (deps all empty), create a linear chain so dagre
  // stacks them vertically instead of placing them all on the same rank.
  if (edges.length === 0 && tasks.length > 1) {
    for (let i = 1; i < tasks.length; i++) {
      g.setEdge(tasks[i - 1].id, tasks[i].id);
      edges.push({
        id: `_auto_${tasks[i - 1].id}->${tasks[i].id}`,
        source: tasks[i - 1].id,
        target: tasks[i].id,
        markerEnd: edgeMarker,
        style: edgeStyle,
      });
    }
  }

  dagre.layout(g);

  const nodes: Node[] = tasks.map((task) => {
    const pos = g.node(task.id);
    return {
      id: task.id,
      type: 'task',
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      data: { task },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
    };
  });

  return { nodes, edges };
}

// ---------------------------------------------------------------------------
// File tree types
// ---------------------------------------------------------------------------
interface FileTreeItem {
  name: string;
  rel_path: string;
  is_dir: boolean;
  size?: number;
  children?: FileTreeItem[];
}

// ---------------------------------------------------------------------------
// File Browser component
// ---------------------------------------------------------------------------
function FileBrowser({ sessionId }: { sessionId: string | null }) {
  const [tree, setTree] = useState<FileTreeItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || '';

  const fetchFiles = useCallback(async () => {
    if (!sessionId) return;
    setLoading(true);
    setError('');
    try {
      const token = typeof window !== 'undefined' ? sessionStorage.getItem('mm_token') : '';
      const res = await fetch(`${API_BASE}/api/files/${sessionId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setTree(data.tree || []);
    } catch (e: any) {
      setError(e.message || 'Failed to load files');
    } finally {
      setLoading(false);
    }
  }, [sessionId, API_BASE]);

  useEffect(() => {
    fetchFiles();
  }, [fetchFiles]);

  const handleDownload = useCallback(
    (relPath: string, fileName: string) => {
      if (!sessionId) return;
      const token = typeof window !== 'undefined' ? sessionStorage.getItem('mm_token') : '';
      const url = `${API_BASE}/api/files/${sessionId}/${relPath}`;
      // Create a temporary link to trigger download with auth
      fetch(url, { headers: { Authorization: `Bearer ${token}` } })
        .then((res) => res.blob())
        .then((blob) => {
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = fileName;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(a.href);
        });
    },
    [sessionId, API_BASE],
  );

  if (!sessionId) {
    return (
      <div className="flex items-center justify-center h-full text-xs text-muted-foreground">
        等待任务启动…
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border">
        <button
          type="button"
          onClick={fetchFiles}
          disabled={loading}
          className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
          title="刷新文件列表"
        >
          <RefreshCwIcon className={cn('size-3.5', loading && 'animate-spin')} />
        </button>
        <span className="text-[10px] text-muted-foreground truncate">
          {sessionId}
        </span>
      </div>

      {/* File tree */}
      <div className="flex-1 overflow-auto px-2 py-1.5">
        {error && (
          <div className="text-xs text-red-500 py-2">{error}</div>
        )}
        {!error && tree.length === 0 && !loading && (
          <div className="text-xs text-muted-foreground py-4 text-center">
            暂无文件
          </div>
        )}
        {tree.map((item) => (
          <FileTreeNode
            key={item.rel_path}
            item={item}
            depth={0}
            onDownload={handleDownload}
          />
        ))}
      </div>
    </div>
  );
}

function FileTreeNode({
  item,
  depth,
  onDownload,
}: {
  item: FileTreeItem;
  depth: number;
  onDownload: (relPath: string, name: string) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 2);

  if (item.is_dir) {
    return (
      <div>
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-1.5 w-full py-0.5 px-1 rounded hover:bg-accent text-left"
          style={{ paddingLeft: `${depth * 12 + 4}px` }}
        >
          {expanded ? (
            <FolderOpenIcon className="size-3.5 text-amber-500 shrink-0" />
          ) : (
            <FolderIcon className="size-3.5 text-amber-500 shrink-0" />
          )}
          <span className="text-xs text-foreground truncate">{item.name}</span>
        </button>
        {expanded && item.children?.map((child) => (
          <FileTreeNode
            key={child.rel_path}
            item={child}
            depth={depth + 1}
            onDownload={onDownload}
          />
        ))}
      </div>
    );
  }

  // File item
  const sizeStr = item.size != null
    ? item.size < 1024
      ? `${item.size}B`
      : item.size < 1024 * 1024
        ? `${(item.size / 1024).toFixed(1)}KB`
        : `${(item.size / 1024 / 1024).toFixed(1)}MB`
    : '';

  return (
    <div
      className="flex items-center gap-1.5 py-0.5 px-1 rounded hover:bg-accent group"
      style={{ paddingLeft: `${depth * 12 + 4}px` }}
    >
      <FileIcon className="size-3.5 text-muted-foreground shrink-0" />
      <span className="text-xs text-foreground truncate flex-1">{item.name}</span>
      <span className="text-[10px] text-muted-foreground shrink-0">{sizeStr}</span>
      <button
        type="button"
        onClick={() => onDownload(item.rel_path, item.name)}
        className="opacity-0 group-hover:opacity-100 rounded p-0.5 text-muted-foreground hover:text-foreground transition-opacity"
        title="下载"
      >
        <DownloadIcon className="size-3" />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pipeline Panel (Tabbed: DAG + File Browser)
// ---------------------------------------------------------------------------
export function PipelinePanel({
  tasks,
  sessionId,
}: {
  tasks: TaskItem[];
  sessionId?: string | null;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [activeTab, setActiveTab] = useState<'dag' | 'files'>('dag');

  const { nodes, edges } = useMemo(() => {
    if (!tasks || tasks.length === 0) return { nodes: [], edges: [] };
    return getLayoutedElements(tasks);
  }, [tasks]);

  // Don't render at all if no tasks yet
  if (!tasks || tasks.length === 0) return null;

  // Collapsed state: thin strip with expand button
  if (collapsed) {
    return (
      <aside className="hidden lg:flex shrink-0 w-10 flex-col items-center border-l border-border bg-sidebar/40 pt-4">
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
          title="展开任务面板"
        >
          <PanelRightOpenIcon className="size-4" />
        </button>
      </aside>
    );
  }

  return (
    <aside className="hidden lg:flex shrink-0 w-80 flex-col border-l border-border bg-sidebar/40">
      {/* Header with tabs */}
      <div className="flex items-center justify-between px-2 py-1.5 border-b border-border">
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setActiveTab('dag')}
            className={cn(
              'flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors',
              activeTab === 'dag'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground hover:bg-accent',
            )}
          >
            <NetworkIcon className="size-3.5" />
            任务
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('files')}
            className={cn(
              'flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors',
              activeTab === 'files'
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:text-foreground hover:bg-accent',
            )}
          >
            <FolderIcon className="size-3.5" />
            文件
          </button>
        </div>
        <button
          type="button"
          onClick={() => setCollapsed(true)}
          className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
          title="折叠面板"
        >
          <PanelRightCloseIcon className="size-4" />
        </button>
      </div>

      {/* Tab content */}
      <div className="flex-1 min-h-0 flex flex-col">
        {activeTab === 'dag' ? (
          <>
            {/* Legend: ID -> title mapping */}
            <TaskLegend tasks={tasks} />
            {/* DAG graph (compact circle nodes) */}
            <div className="flex-1 min-h-0 bg-white">
              <ReactFlow
                key={`dag-${nodes.length}`}
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                fitView
                fitViewOptions={{ padding: 0.15, maxZoom: 0.85, minZoom: 0.3 }}
                nodesDraggable={false}
                nodesConnectable={false}
                elementsSelectable={false}
                panOnDrag={true}
                zoomOnScroll={true}
                minZoom={0.4}
                maxZoom={1.5}
                proOptions={{ hideAttribution: true }}
              />
            </div>
          </>
        ) : (
          <FileBrowser sessionId={sessionId ?? null} />
        )}
      </div>
    </aside>
  );
}
