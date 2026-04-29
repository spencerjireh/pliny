import { emitUnauthorized } from "../auth";
import type { components } from "./types";

export type SearchResponse = components["schemas"]["SearchResponse"];
export type SearchResultItem = components["schemas"]["SearchResultItem"];
export type MatchingChunk = components["schemas"]["MatchingChunk"];

export interface ItemDetail {
  id: string;
  type: string;
  title: string | null;
  summary: string | null;
  captured_at: string;
  canonical_url: string | null;
  content_hash: string;
  raw_ref: string | null;
  metadata: Record<string, unknown>;
  content: { extracted_text: string | null } | null;
  chunks: { index: number; text: string }[];
  sources: { source: string; source_ref: string | null; captured_at: string }[];
  entities: {
    name: string;
    type: string;
    mention_text: string | null;
    confidence: number | null;
  }[];
  tags: string[];
}

export type ItemDetailOrRedirect = ItemDetail | { redirect_to: string };

export interface SearchParams {
  q?: string;
  type?: string[];
  from?: string;
  to?: string;
  tag?: string[];
  entity?: string[];
  possibleDuplicate?: boolean;
  cursor?: string;
  limit?: number;
}

export interface ApiClient {
  search(params: SearchParams): Promise<SearchResponse>;
  getItem(id: string): Promise<ItemDetailOrRedirect>;
  deleteItem(id: string): Promise<void>;
}

const DEFAULT_BASE = "";

export function makeClient(apiKey: string, baseUrl: string = DEFAULT_BASE): ApiClient {
  const headers = { Authorization: `Bearer ${apiKey}` };

  async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
    const r = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: { ...headers, ...(init?.headers ?? {}) },
    });
    if (r.status === 401) {
      emitUnauthorized();
      throw new Error("unauthorized");
    }
    if (r.status === 404) {
      throw new NotFoundError(`not found: ${path}`);
    }
    if (!r.ok) {
      throw new Error(`request failed: ${r.status} ${r.statusText}`);
    }
    if (r.status === 204) return undefined as T;
    return (await r.json()) as T;
  }

  function buildSearchQuery(params: SearchParams): string {
    const sp = new URLSearchParams();
    if (params.q) sp.set("q", params.q);
    for (const t of params.type ?? []) sp.append("type", t);
    if (params.from) sp.set("from", params.from);
    if (params.to) sp.set("to", params.to);
    for (const t of params.tag ?? []) sp.append("tag", t);
    for (const e of params.entity ?? []) sp.append("entity", e);
    if (params.possibleDuplicate) sp.set("possible_duplicate", "true");
    if (params.cursor) sp.set("cursor", params.cursor);
    if (params.limit !== undefined) sp.set("limit", String(params.limit));
    return sp.toString();
  }

  return {
    search: (params) => fetchJson<SearchResponse>(`/v1/search?${buildSearchQuery(params)}`),
    getItem: (id) => fetchJson<ItemDetailOrRedirect>(`/v1/items/${id}`),
    deleteItem: (id) => fetchJson<void>(`/v1/items/${id}`, { method: "DELETE" }),
  };
}

export class NotFoundError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NotFoundError";
  }
}
