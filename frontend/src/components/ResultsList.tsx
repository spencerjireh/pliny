import { useEffect, useState } from "react";

import type { ApiClient, SearchResponse, SearchResultItem } from "../api/client";
import type { SearchFilters } from "../state";
import { ItemCard } from "./ItemCard";

interface ResultsListProps {
  client: ApiClient;
  filters: SearchFilters;
}

export function ResultsList({ client, filters }: ResultsListProps) {
  const [items, setItems] = useState<SearchResultItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset and fetch fresh page whenever filters change.
  useEffect(() => {
    let cancelled = false;
    setItems([]);
    setCursor(null);
    setError(null);
    setLoading(true);
    client
      .search(toSearchParams(filters))
      .then((res: SearchResponse) => {
        if (cancelled) return;
        setItems(res.items);
        setCursor(res.next_cursor ?? null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [client, filters]);

  const loadMore = async () => {
    if (cursor === null || loading) return;
    setLoading(true);
    try {
      const res = await client.search({ ...toSearchParams(filters), cursor });
      setItems((prev) => [...prev, ...res.items]);
      setCursor(res.next_cursor ?? null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const removeFromList = (id: string) => {
    setItems((prev) => prev.filter((i) => i.id !== id));
  };

  if (error !== null) {
    return <div className="rounded border border-rose-300 bg-rose-50 p-4 text-sm text-rose-800">{error}</div>;
  }

  if (loading && items.length === 0) {
    return <div className="text-sm text-zinc-500">Loading…</div>;
  }

  if (items.length === 0) {
    return <div className="text-sm text-zinc-500">No items match.</div>;
  }

  return (
    <div className="space-y-3">
      {items.map((item) => (
        <ItemCard key={item.id} client={client} item={item} onDeleted={removeFromList} />
      ))}
      {cursor !== null && (
        <button
          type="button"
          disabled={loading}
          onClick={() => {
            void loadMore();
          }}
          className="w-full rounded border border-zinc-300 px-3 py-2 text-sm hover:bg-zinc-100 disabled:opacity-50"
        >
          {loading ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}

function toSearchParams(filters: SearchFilters) {
  return {
    q: filters.q || undefined,
    type: filters.type.length ? filters.type : undefined,
    from: filters.from || undefined,
    to: filters.to || undefined,
    tag: filters.tag.length ? filters.tag : undefined,
    entity: filters.entity.length ? filters.entity : undefined,
    possibleDuplicate: filters.possibleDuplicate || undefined,
  };
}
