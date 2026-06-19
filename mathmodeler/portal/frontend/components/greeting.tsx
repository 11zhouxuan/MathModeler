'use client';

import { motion } from 'framer-motion';
import {
  BarChart3Icon,
  GlobeIcon,
  SparklesIcon,
  TrophyIcon,
} from 'lucide-react';
import { SAMPLES, type Sample } from '@/lib/samples';

const ICONS: Record<Sample['icon'], React.ComponentType<{ className?: string }>> = {
  trophy: TrophyIcon,
  'bar-chart': BarChart3Icon,
  globe: GlobeIcon,
};

export function Greeting({ onSample }: { onSample?: (text: string) => void }) {
  return (
    <div
      className="mx-auto flex size-full max-w-3xl flex-col justify-center px-2 md:px-0"
      key="overview"
    >
      <motion.div
        animate={{ opacity: 1, y: 0 }}
        className="mb-2 flex items-center gap-2 font-semibold text-2xl"
        exit={{ opacity: 0, y: 10 }}
        initial={{ opacity: 0, y: 10 }}
        transition={{ delay: 0.4 }}
      >
        <span className="flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <SparklesIcon className="size-5" />
        </span>
        开始数学建模
      </motion.div>
      <motion.div
        animate={{ opacity: 1, y: 0 }}
        className="text-muted-foreground text-base"
        exit={{ opacity: 0, y: 10 }}
        initial={{ opacity: 0, y: 10 }}
        transition={{ delay: 0.5 }}
      >
        输入一道开放式数学建模问题，分析 · 建模 · 求解 · 报告 四位智能体将协同为你求解。选择一个赛题示例快速开始：
      </motion.div>

      <motion.div
        animate={{ opacity: 1, y: 0 }}
        className="mt-6 grid gap-3 sm:grid-cols-3"
        exit={{ opacity: 0, y: 10 }}
        initial={{ opacity: 0, y: 10 }}
        transition={{ delay: 0.6 }}
      >
        {SAMPLES.map((s) => {
          const Icon = ICONS[s.icon];
          return (
            <button
              key={s.label}
              type="button"
              onClick={() => onSample?.(s.text)}
              className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4 text-left transition-colors hover:border-primary/40 hover:bg-accent"
            >
              <span className="flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Icon className="size-4" />
              </span>
              <span className="font-mono text-[11px] text-muted-foreground">
                {s.source}
              </span>
              <span className="font-medium text-sm">{s.label}</span>
              <span className="line-clamp-2 text-muted-foreground text-xs">
                {s.text.slice(0, 40)}…
              </span>
            </button>
          );
        })}
      </motion.div>
    </div>
  );
}
