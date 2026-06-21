'use client';

import { SigmaIcon } from 'lucide-react';
import { SidebarTrigger } from '@/components/ui/sidebar';

export function ChatHeader() {
  return (
    <header className="sticky top-0 z-10 flex items-center gap-2 border-b border-border bg-background/80 px-3 py-2 backdrop-blur">
      <SidebarTrigger />
      <div className="flex items-center gap-1.5 font-semibold">
        <span className="flex size-7 items-center justify-center rounded-md bg-primary/10 text-primary">
          <SigmaIcon className="size-4" />
        </span>
        <span>Math<span className="text-primary">Modeler</span></span>
      </div>
    </header>
  );
}
