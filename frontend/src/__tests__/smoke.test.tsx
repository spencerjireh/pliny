// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import App from "../App";

interface ApiCall {
  url: string;
  method: string;
  authorization: string | null;
}

function makeFetchMock() {
  const calls: ApiCall[] = [];

  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    const authorization =
      (init?.headers as Record<string, string> | undefined)?.Authorization ?? null;
    calls.push({ url, method, authorization });

    if (method === "GET" && url.startsWith("/v1/search")) {
      return new Response(
        JSON.stringify({
          items: [
            {
              id: "11111111-1111-1111-1111-111111111111",
              title: "Hello World",
              summary: "Test summary text.",
              type: "text",
              captured_at: "2026-04-01T12:00:00Z",
              score: 0.42,
              matching_chunks: [],
              possible_duplicate_of: null,
            },
          ],
          next_cursor: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }

    if (method === "DELETE" && url.startsWith("/v1/items/")) {
      return new Response(null, { status: 204 });
    }

    return new Response(JSON.stringify({ detail: "unexpected" }), { status: 500 });
  });

  return { handler, calls };
}

describe("App smoke", () => {
  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("prompts for an API key when none is stored", () => {
    render(<App />);
    expect(screen.getByPlaceholderText(/API key/i)).toBeInTheDocument();
  });

  it("renders search results once authenticated", async () => {
    localStorage.setItem("pliny.api_key", "test-key");
    const { handler } = makeFetchMock();
    vi.stubGlobal("fetch", handler);

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText("Hello World")).toBeInTheDocument();
    });
    expect(screen.getByText("Test summary text.")).toBeInTheDocument();
  });

  it("deletes an item when confirmed", async () => {
    localStorage.setItem("pliny.api_key", "test-key");
    const { handler, calls } = makeFetchMock();
    vi.stubGlobal("fetch", handler);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);

    const card = await screen.findByText("Hello World");
    expect(card).toBeInTheDocument();

    const deleteBtn = screen.getByRole("button", { name: /^Delete$/ });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(screen.queryByText("Hello World")).not.toBeInTheDocument();
    });

    const deleteCall = calls.find((c) => c.method === "DELETE");
    expect(deleteCall).toBeDefined();
    expect(deleteCall?.url).toContain("/v1/items/11111111-1111-1111-1111-111111111111");
    expect(deleteCall?.authorization).toBe("Bearer test-key");
  });
});
