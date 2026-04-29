import { useEffect, useState } from "react";

import { clearApiKey, loadApiKey, onUnauthorized, saveApiKey } from "../auth";

interface AuthGateProps {
  children: (apiKey: string) => React.ReactNode;
}

export function AuthGate({ children }: AuthGateProps) {
  const [apiKey, setApiKey] = useState<string | null>(() => loadApiKey());
  const [draft, setDraft] = useState<string>("");

  useEffect(() => {
    return onUnauthorized(() => {
      clearApiKey();
      setApiKey(null);
    });
  }, []);

  if (apiKey === null || apiKey === "") {
    return (
      <main className="flex min-h-screen items-center justify-center bg-zinc-50">
        <form
          className="w-full max-w-sm space-y-4 rounded-lg border border-zinc-200 bg-white p-6 shadow-sm"
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = draft.trim();
            if (!trimmed) return;
            saveApiKey(trimmed);
            setApiKey(trimmed);
          }}
        >
          <h1 className="text-xl font-semibold text-zinc-800">Pliny</h1>
          <p className="text-sm text-zinc-600">Enter your API key.</p>
          <input
            type="password"
            autoFocus
            placeholder="API key"
            className="w-full rounded border border-zinc-300 px-3 py-2 text-sm focus:border-zinc-500 focus:outline-none"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button
            type="submit"
            className="w-full rounded bg-zinc-900 px-3 py-2 text-sm font-medium text-white hover:bg-zinc-700"
          >
            Continue
          </button>
        </form>
      </main>
    );
  }

  return <>{children(apiKey)}</>;
}
