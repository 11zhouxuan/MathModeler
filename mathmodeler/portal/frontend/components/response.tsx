'use client';

import type { ComponentProps } from 'react';
import { Streamdown } from 'streamdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import { cn } from '@/lib/utils';

type ResponseProps = ComponentProps<typeof Streamdown>;

// Enable singleDollarTextMath so both $...$ (inline) and $$...$$ (block) math
// are rendered via KaTeX. The LLM frequently uses $...$ for inline formulas.
const remarkPlugins: any[] = [
  [remarkGfm, {}],
  [remarkMath, { singleDollarTextMath: true }],
];
const rehypePlugins: any[] = [
  [rehypeKatex, { errorColor: 'var(--color-muted-foreground)' }],
];

export function Response({ className, children, ...props }: ResponseProps) {
  return (
    <Streamdown
      className={cn(
        'size-full [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 [&_code]:whitespace-pre-wrap [&_code]:break-words [&_pre]:max-w-full [&_pre]:overflow-x-auto',
        className,
      )}
      remarkPlugins={remarkPlugins}
      rehypePlugins={rehypePlugins}
      {...props}
    >
      {children}
    </Streamdown>
  );
}
