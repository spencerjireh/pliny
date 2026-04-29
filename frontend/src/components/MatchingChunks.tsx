import type { MatchingChunk } from "../api/client";

const SAFE_HIGHLIGHT = /^(?:[^<]|<\/?b>)*$/;

interface MatchingChunksProps {
  chunks: MatchingChunk[];
}

export function MatchingChunks({ chunks }: MatchingChunksProps) {
  if (!chunks.length) return null;
  return (
    <ul className="mt-3 space-y-2 text-sm text-zinc-700">
      {chunks.map((c) => (
        <li key={c.chunk_id} className="rounded border border-zinc-200 bg-zinc-50 p-2">
          <Highlight html={c.highlights} />
        </li>
      ))}
    </ul>
  );
}

function Highlight({ html }: { html: string }) {
  // Postgres `ts_headline` emits `<b>...</b>` only. Reject anything else as a
  // defense-in-depth XSS guard before injecting via dangerouslySetInnerHTML.
  if (!SAFE_HIGHLIGHT.test(html)) {
    return <span>{html}</span>;
  }
  return <span dangerouslySetInnerHTML={{ __html: html }} />;
}
