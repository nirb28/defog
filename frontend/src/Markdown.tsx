// Loaded on demand: Markdown and math rendering are the bulk of the app's code,
// so the opening screen doesn't wait for them.
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

/**
 * Models write math several ways. remark-math only treats $$ as a display
 * equation when the delimiters sit on their own lines, so rewrite the
 * common variants into that form.
 */
export function normalizeMath(text: string): string {
  return (
    text
      // \[ ... \]  ->  display math
      .replace(/\\\[([\s\S]+?)\\\]/g, (_, body: string) => `\n$$\n${body.trim()}\n$$\n`)
      // \( ... \)  ->  inline math
      .replace(/\\\(([\s\S]+?)\\\)/g, (_, body: string) => `$${body.trim()}$`)
      // a line that is just $$ ... $$  ->  display math on its own lines
      .replace(/^[ \t]*\$\$(.+?)\$\$[ \t]*$/gm, (_, body: string) => `$$\n${body.trim()}\n$$`)
      // "$5" or "$1,200.50" followed by a space or punctuation is money, not math
      .replace(
        /(^|[^\\$])\$(?=\d[\d,]*(?:\.\d+)?(?:[\s.,;:!?)]|$))/gm,
        (_, before: string) => `${before}\\$`,
      )
  );
}

// Links in answers point to Canvas and Google Calendar: open them in a new tab.
const components: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
};

export default function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={components}>
      {normalizeMath(children)}
    </ReactMarkdown>
  );
}
