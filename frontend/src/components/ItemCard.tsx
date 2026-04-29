import { useState } from "react";

import type { ApiClient, SearchResultItem } from "../api/client";
import { DeleteButton } from "./DeleteButton";
import { DuplicateInline } from "./DuplicateInline";
import { MatchingChunks } from "./MatchingChunks";

interface ItemCardProps {
  client: ApiClient;
  item: SearchResultItem;
  onDeleted: (id: string) => void;
}

export function ItemCard({ client, item, onDeleted }: ItemCardProps) {
  const [expanded, setExpanded] = useState(false);
  const hasChunks = item.matching_chunks.length > 0;

  return (
    <article className="space-y-2 rounded-lg border border-zinc-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-base font-medium text-zinc-900">
            {item.title ?? "(no title)"}
          </h3>
          {item.summary !== null && (
            <p className="mt-1 text-sm text-zinc-700">{item.summary}</p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-zinc-500">
            <span className="rounded bg-zinc-100 px-2 py-0.5 font-mono">{item.type}</span>
            <span>{formatTimestamp(item.captured_at)}</span>
            {typeof item.score === "number" && <span>score {item.score.toFixed(3)}</span>}
          </div>
        </div>
        <div className="flex flex-col gap-2">
          {hasChunks && (
            <button
              type="button"
              className="rounded border border-zinc-300 px-3 py-1 text-xs hover:bg-zinc-100"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "Hide" : "Expand"}
            </button>
          )}
          <DeleteButton client={client} itemId={item.id} onDeleted={() => onDeleted(item.id)} />
        </div>
      </div>

      {expanded && hasChunks && <MatchingChunks chunks={item.matching_chunks} />}

      {item.possible_duplicate_of != null && item.possible_duplicate_of !== "" && (
        <DuplicateInline
          client={client}
          candidateId={item.possible_duplicate_of}
          onCandidateDeleted={() =>
            // The candidate's id is unrelated to this card's id; the spec says
            // "either side can be deleted from the inline view". We leave this
            // card in place; the duplicate is gone, so a refetch on next search
            // will surface the updated state.
            onDeleted("__candidate__")
          }
        />
      )}
    </article>
  );
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
