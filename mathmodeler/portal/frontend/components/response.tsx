'use client';

import type { ComponentProps } from 'react';
import { Streamdown } from 'streamdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import { visit } from 'unist-util-visit';
import { cn } from '@/lib/utils';

type ResponseProps = ComponentProps<typeof Streamdown>;

// Strip LaTeX commands unsupported by KaTeX (e.g. \\[2pt] row spacing).
function remarkMathSanitize() {
  return (tree: any) => {
    visit(tree, ['math', 'inlineMath'], (node: any) => {
      if (node.value) {
        // \\[Xpt] or \\[X.Xpt] → plain \\ (KaTeX doesn't support row spacing)
        node.value = node.value.replace(/\\\\[\s]*\[\s*[\d.]+\s*(?:pt|em|ex|mm|cm)\s*\]/g, '\\\\');
      }
    });
  };
}

// Enable singleDollarTextMath so both $...$ (inline) and $$...$$ (block) math
// are rendered via KaTeX. The LLM frequently uses $...$ for inline formulas.
const remarkPlugins: any[] = [
  [remarkGfm, {}],
  [remarkMath, { singleDollarTextMath: true }],
  remarkMathSanitize,
];
const rehypePlugins: any[] = [
  [rehypeKatex, { throwOnError: false, strict: false, errorColor: 'var(--color-muted-foreground)' }],
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
