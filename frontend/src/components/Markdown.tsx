import { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const CITATION_PATTERN = /\[(\d{1,2})\](?![(\[:])/g;

function decorateInline(text: string): string {
  return text.replace(CITATION_PATTERN, (_match, index: string) => `\`⟦${index}⟧\``);
}

/**
 * 把正文中的 [1] [2] 引用角标转成特殊的行内代码标记，
 * 由自定义 code 渲染器替换成可点击的上标按钮；代码块内部不处理。
 */
export function decorateCitations(source: string): string {
  const fenceParts = source.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~)/g);
  return fenceParts
    .map((part, fenceIndex) => {
      if (fenceIndex % 2 === 1) return part;
      const inlineParts = part.split(/(`+[^`]*`+)/g);
      return inlineParts
        .map((segment, inlineIndex) => (inlineIndex % 2 === 1 ? segment : decorateInline(segment)))
        .join("");
    })
    .join("");
}

interface MarkdownProps {
  content: string;
  onCitationClick?: (index: number) => void;
}

export function Markdown({ content, onCitationClick }: MarkdownProps) {
  const source = useMemo(
    () => (onCitationClick ? decorateCitations(content) : content),
    [content, onCitationClick],
  );

  const components = useMemo<Components>(
    () => ({
      code({ className, children, ...rest }) {
        const text = typeof children === "string" ? children : String(children ?? "");
        const citation = /^⟦(\d{1,2})⟧$/.exec(text);
        if (!className && citation && onCitationClick) {
          const index = Number(citation[1]);
          return (
            <button
              type="button"
              className="cite-sup"
              onClick={() => onCitationClick(index)}
              aria-label={`查看第 ${index} 条参考来源`}
              title={`查看来源 ${index}`}
            >
              {index}
            </button>
          );
        }
        // eslint-disable-next-line react/jsx-props-no-spreading
        return (
          <code className={className} {...rest}>
            {children}
          </code>
        );
      },
      a({ href, children }) {
        return (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        );
      },
      table({ children }) {
        return (
          <div className="markdown-table-wrap">
            <table>{children}</table>
          </div>
        );
      },
    }),
    [onCitationClick],
  );

  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
