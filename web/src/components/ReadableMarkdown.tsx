import * as React from "react";
import Markdown, { type Components } from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import "./readableMarkdown.css";

const components: Components = {
  a: ({ href, children }) =>
    href && /^(https?:\/\/|mailto:)/i.test(href) ? (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ) : (
      children
    ),
  img: ({ alt }) => (alt ? <span>{alt}</span> : null),
  table: ({ children }) => (
    <div className="readable-markdown-table">
      <table>{children}</table>
    </div>
  ),
};

export const ReadableMarkdown = React.memo(function ReadableMarkdown({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  return (
    <div className={className ? `readable-markdown ${className}` : "readable-markdown"}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw, rehypeSanitize]}
        components={components}
      >
        {children}
      </Markdown>
    </div>
  );
});
