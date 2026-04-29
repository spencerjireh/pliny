import { useState } from "react";

import type { ApiClient } from "../api/client";
import { NotFoundError } from "../api/client";

interface DeleteButtonProps {
  client: ApiClient;
  itemId: string;
  onDeleted: () => void;
  label?: string;
}

export function DeleteButton({ client, itemId, onDeleted, label = "Delete" }: DeleteButtonProps) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleClick = async () => {
    if (busy) return;
    if (!confirm(`Delete this item? This cannot be undone.`)) return;
    setBusy(true);
    setErr(null);
    try {
      await client.deleteItem(itemId);
      onDeleted();
    } catch (e) {
      if (e instanceof NotFoundError) {
        // Already gone — treat as success from the user's perspective.
        onDeleted();
        return;
      }
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        disabled={busy}
        onClick={handleClick}
        className="rounded border border-rose-300 px-3 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
      >
        {busy ? "Deleting…" : label}
      </button>
      {err !== null && <span className="text-xs text-rose-600">{err}</span>}
    </div>
  );
}
