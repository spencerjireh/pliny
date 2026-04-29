import { useEffect, useState } from "react";

import type { ApiClient, ItemDetail } from "../api/client";
import { DeleteButton } from "./DeleteButton";

interface DuplicateInlineProps {
  client: ApiClient;
  candidateId: string;
  onCandidateDeleted: () => void;
}

export function DuplicateInline({
  client,
  candidateId,
  onCandidateDeleted,
}: DuplicateInlineProps) {
  const [item, setItem] = useState<ItemDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItem(null);
    setError(null);
    client
      .getItem(candidateId)
      .then((res) => {
        if (cancelled) return;
        if ("redirect_to" in res) {
          // The candidate was merged elsewhere; chase one hop.
          return client.getItem(res.redirect_to).then((next) => {
            if (!cancelled && !("redirect_to" in next)) setItem(next);
          });
        }
        setItem(res);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [client, candidateId]);

  return (
    <aside className="mt-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
      <div className="text-xs font-semibold uppercase tracking-wide">Possible duplicate of</div>
      {error !== null && <div className="mt-1 text-xs text-rose-700">Couldn't load: {error}</div>}
      {item !== null && (
        <div className="mt-2 space-y-1">
          <div className="font-medium">{item.title ?? "(no title)"}</div>
          {item.summary !== null && <div className="text-amber-800">{item.summary}</div>}
          <div className="pt-2">
            <DeleteButton
              client={client}
              itemId={item.id}
              onDeleted={onCandidateDeleted}
              label="Delete duplicate"
            />
          </div>
        </div>
      )}
    </aside>
  );
}
