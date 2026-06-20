'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowLeftIcon,
  DownloadIcon,
  MaximizeIcon,
  MinimizeIcon,
  FileTextIcon,
  ImageIcon,
  FileIcon,
  Loader2Icon,
} from 'lucide-react';
import { CodeBlock } from './ai-elements/code-block';
import type { BundledLanguage } from 'shiki';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
export interface PreviewFile {
  /** File path relative to session workspace (e.g. "report/report.pdf") */
  relPath: string;
  /** File name (e.g. "report.pdf") */
  name: string;
  /** Session ID */
  sessionId: string;
}

// ---------------------------------------------------------------------------
// File type detection
// ---------------------------------------------------------------------------
type FileCategory = 'pdf' | 'image' | 'markdown' | 'code' | 'unsupported';

function categorize(name: string): FileCategory {
  const ext = name.split('.').pop()?.toLowerCase() || '';
  if (ext === 'pdf') return 'pdf';
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp'].includes(ext)) return 'image';
  if (ext === 'md') return 'markdown';
  if (['py', 'json', 'tex', 'txt', 'log', 'csv', 'yaml', 'yml', 'toml', 'cfg', 'ini', 'sh', 'bash', 'html', 'css', 'js', 'ts', 'tsx', 'jsx', 'xml', 'sql', 'r', 'aux', 'out', 'toc'].includes(ext)) return 'code';
  return 'unsupported';
}

function langFromExt(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() || '';
  const map: Record<string, string> = {
    py: 'python', json: 'json', tex: 'latex', txt: 'text', log: 'text',
    csv: 'csv', yaml: 'yaml', yml: 'yaml', toml: 'toml', sh: 'bash',
    bash: 'bash', html: 'html', css: 'css', js: 'javascript', ts: 'typescript',
    tsx: 'tsx', jsx: 'jsx', xml: 'xml', sql: 'sql', r: 'r', md: 'markdown',
    aux: 'text', out: 'text', toc: 'text',
  };
  return map[ext] || 'text';
}

// ---------------------------------------------------------------------------
// FilePreview Component
// ---------------------------------------------------------------------------
export function FilePreview({
  file,
  onClose,
}: {
  file: PreviewFile;
  onClose: () => void;
}) {
  const [fullscreen, setFullscreen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const category = categorize(file.name);

  const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || '';
  const token = typeof window !== 'undefined' ? sessionStorage.getItem('mm_token') || '' : '';
  const fileUrl = `${API_BASE}/api/files/${file.sessionId}/${file.relPath}`;

  const toggleFullscreen = useCallback(() => {
    if (!containerRef.current) return;
    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen().then(() => setFullscreen(true)).catch(() => {});
    } else {
      document.exitFullscreen().then(() => setFullscreen(false)).catch(() => {});
    }
  }, []);

  // Listen for fullscreen exit via Escape
  useEffect(() => {
    const handler = () => {
      if (!document.fullscreenElement) setFullscreen(false);
    };
    document.addEventListener('fullscreenchange', handler);
    return () => document.removeEventListener('fullscreenchange', handler);
  }, []);

  const handleDownload = useCallback(() => {
    fetch(fileUrl, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => res.blob())
      .then((blob) => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = file.name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
      });
  }, [fileUrl, token, file.name]);

  return (
    <div
      ref={containerRef}
      className="flex h-full flex-col bg-background"
    >
      {/* Toolbar */}
      <div className="flex items-center gap-2 border-b border-border bg-card px-4 py-2">
        <button
          type="button"
          onClick={onClose}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-3 py-1.5 text-sm font-medium text-foreground hover:bg-accent transition-colors"
        >
          <ArrowLeftIcon className="size-4" />
          返回对话
        </button>

        <div className="flex-1 min-w-0 flex items-center gap-2 px-2">
          {category === 'pdf' && <FileTextIcon className="size-4 text-red-500 shrink-0" />}
          {category === 'image' && <ImageIcon className="size-4 text-blue-500 shrink-0" />}
          {(category === 'code' || category === 'markdown') && <FileTextIcon className="size-4 text-green-500 shrink-0" />}
          {category === 'unsupported' && <FileIcon className="size-4 text-muted-foreground shrink-0" />}
          <span className="text-sm font-medium truncate">{file.name}</span>
          <span className="text-xs text-muted-foreground truncate hidden sm:inline">
            {file.relPath}
          </span>
        </div>

        <button
          type="button"
          onClick={handleDownload}
          className="rounded-lg border border-border bg-background p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
          title="下载文件"
        >
          <DownloadIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={toggleFullscreen}
          className="rounded-lg border border-border bg-background p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
          title={fullscreen ? '退出全屏' : '全屏显示'}
        >
          {fullscreen ? <MinimizeIcon className="size-4" /> : <MaximizeIcon className="size-4" />}
        </button>
      </div>

      {/* Preview content */}
      <div className="flex-1 min-h-0 overflow-auto">
        {category === 'pdf' && (
          <PdfPreview url={fileUrl} token={token} />
        )}
        {category === 'image' && (
          <ImagePreview url={fileUrl} token={token} name={file.name} />
        )}
        {category === 'code' && (
          <TextPreview url={fileUrl} token={token} name={file.name} />
        )}
        {category === 'markdown' && (
          <MarkdownPreview url={fileUrl} token={token} />
        )}
        {category === 'unsupported' && (
          <div className="flex flex-col items-center justify-center h-full gap-4 text-muted-foreground">
            <FileIcon className="size-16 opacity-30" />
            <p className="text-sm">此文件类型暂不支持预览</p>
            <button
              type="button"
              onClick={handleDownload}
              className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
            >
              <DownloadIcon className="size-4" />
              下载文件
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// PDF Preview (iframe with blob URL)
// ---------------------------------------------------------------------------
function PdfPreview({ url, token }: { url: string; token: string }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        const objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [url, token]);

  useEffect(() => {
    return () => { if (blobUrl) URL.revokeObjectURL(blobUrl); };
  }, [blobUrl]);

  if (loading) return <LoadingSpinner />;
  if (error) return <ErrorDisplay message={error} />;
  if (!blobUrl) return null;

  return (
    <iframe
      src={blobUrl}
      className="w-full h-full border-0"
      title="PDF Preview"
    />
  );
}

// ---------------------------------------------------------------------------
// Image Preview
// ---------------------------------------------------------------------------
function ImagePreview({ url, token, name }: { url: string; token: string; name: string }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        setBlobUrl(URL.createObjectURL(blob));
      })
      .catch((e) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [url, token]);

  useEffect(() => {
    return () => { if (blobUrl) URL.revokeObjectURL(blobUrl); };
  }, [blobUrl]);

  if (loading) return <LoadingSpinner />;
  if (error) return <ErrorDisplay message={error} />;
  if (!blobUrl) return null;

  return (
    <div className="flex items-center justify-center h-full p-4 bg-muted/30">
      <img
        src={blobUrl}
        alt={name}
        className="max-w-full max-h-full object-contain rounded-lg shadow-md"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Text/Code Preview
// ---------------------------------------------------------------------------
function TextPreview({ url, token, name }: { url: string; token: string; name: string }) {
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.text();
      })
      .then((t) => { if (!cancelled) setText(t); })
      .catch((e) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [url, token]);

  if (loading) return <LoadingSpinner />;
  if (error) return <ErrorDisplay message={error} />;

  const lang = langFromExt(name) as BundledLanguage;
  return (
    <div className="p-4">
      <CodeBlock code={text} language={lang} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Markdown Preview (rendered)
// ---------------------------------------------------------------------------
function MarkdownPreview({ url, token }: { url: string; token: string }) {
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.text();
      })
      .then((t) => { if (!cancelled) setText(t); })
      .catch((e) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [url, token]);

  if (loading) return <LoadingSpinner />;
  if (error) return <ErrorDisplay message={error} />;

  // Simple markdown rendering using prose styles (no extra dependency needed)
  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="prose prose-sm dark:prose-invert max-w-none whitespace-pre-wrap">
        {text}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared UI helpers
// ---------------------------------------------------------------------------
function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center h-full">
      <Loader2Icon className="size-8 animate-spin text-muted-foreground" />
    </div>
  );
}

function ErrorDisplay({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center h-full">
      <div className="text-center text-destructive">
        <p className="text-sm font-medium">加载失败</p>
        <p className="text-xs mt-1">{message}</p>
      </div>
    </div>
  );
}
