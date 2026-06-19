'use client';

import { MessageSquareIcon, PlusIcon, SigmaIcon, Trash2Icon } from 'lucide-react';
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { SessionMeta } from '@/lib/history';

export function AppSidebar({
  sessions,
  currentId,
  onNew,
  onPick,
  onDelete,
}: {
  sessions: SessionMeta[];
  currentId: string;
  onNew: () => void;
  onPick: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-1 py-1.5 group-data-[collapsible=icon]:justify-center">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
            <SigmaIcon className="size-4" />
          </span>
          <span className="font-semibold group-data-[collapsible=icon]:hidden">
            Math<span className="text-primary">Modeler</span>
          </span>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={onNew}
          className="mt-1 w-full justify-start gap-2 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
        >
          <PlusIcon className="size-4" />
          <span className="group-data-[collapsible=icon]:hidden">新建会话</span>
        </Button>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>历史会话 · History</SidebarGroupLabel>
          <SidebarGroupContent>
            {sessions.length === 0 ? (
              <div className="px-2 py-1.5 text-muted-foreground text-xs group-data-[collapsible=icon]:hidden">
                暂无历史会话
              </div>
            ) : (
              <SidebarMenu>
                {sessions.map((s) => (
                  <SidebarMenuItem key={s.id}>
                    <SidebarMenuButton
                      isActive={s.id === currentId}
                      onClick={() => onPick(s.id)}
                      tooltip={s.title}
                      className={cn('group/item pr-8')}
                    >
                      <MessageSquareIcon className="size-4" />
                      <span className="truncate">{s.title}</span>
                    </SidebarMenuButton>
                    <button
                      type="button"
                      aria-label="删除会话"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDelete(s.id);
                      }}
                      className="absolute top-1.5 right-1.5 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-sidebar-accent hover:text-foreground group-hover/menu-item:opacity-100 group-data-[collapsible=icon]:hidden"
                    >
                      <Trash2Icon className="size-3.5" />
                    </button>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            )}
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  );
}
