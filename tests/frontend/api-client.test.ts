// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * API client behaviour: auth header injection, 401 recovery, and error-message
 * extraction. These are the parts every page depends on, and a regression here
 * shows up as a confusing blank error rather than an obvious failure.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { signOut } from "aws-amplify/auth";

import { api } from "@/lib/api";

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("authFetch", () => {
  it("attaches the Cognito ID token as a Bearer header", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ icons: [] }));

    await api.listIconLegend();

    // authFetch passes a real Headers instance through to fetch.
    const [, init] = fetchMock.mock.calls[0];
    const headers = init?.headers as Headers;
    expect(headers).toBeInstanceOf(Headers);
    expect(headers.get("Authorization")).toBe("Bearer test-id-token");
  });

  it("signs the user out on 401 so the login gate can take over", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "expired" }, { status: 401 }),
    );

    // listIconLegend now surfaces the failure, but the sign-out must still fire.
    await expect(api.listIconLegend()).rejects.toThrow();

    expect(vi.mocked(signOut)).toHaveBeenCalled();
  });

  it("does not sign out on an ordinary 403", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "Requires role: curator" }, { status: 403 }),
    );

    await expect(api.listIconLegend("draft")).rejects.toThrow();

    expect(vi.mocked(signOut)).not.toHaveBeenCalled();
  });
});

describe("error surfacing", () => {
  it("prefers the FastAPI `detail` field", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "icon_type must be one of [...]" }, { status: 400 }),
    );

    await expect(
      api.createIcon({ label: "x", icon_type: "banana" }),
    ).rejects.toThrow("icon_type must be one of [...]");
  });

  it("falls back to a status line when the body is not JSON", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("<html>502</html>", { status: 502, statusText: "Bad Gateway" }),
    );

    await expect(
      api.createIcon({ label: "x", icon_type: "server" }),
    ).rejects.toThrow(/Bad Gateway/);
  });
});

describe("icon legend requests", () => {
  it("requests the review queue by status and batch", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ icons: [] }));

    await api.listIconLegend("draft", "iconbatch-abc");

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("status=draft");
    expect(url).toContain("batch=iconbatch-abc");
  });

  it("surfaces a load failure rather than reporting an empty legend", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("network down"));
    // A curator with pending drafts must not be shown "nothing to review" when
    // the request actually failed — that silently skips the approval step.
    await expect(api.listIconLegend()).rejects.toThrow();
  });

  it("reports a 403 on the review queue instead of returning empty", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ detail: "Only curators can view the review queue" }, { status: 403 }),
    );
    await expect(api.listIconLegend("draft")).rejects.toThrow(/curators/);
  });

  it("posts the image as multipart form data for interpretation", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ id: "ICON-1", status: "draft" }));

    const file = new File([new Uint8Array([1, 2, 3])], "icon.png", {
      type: "image/png",
    });
    await api.interpretIconImage(file);

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/icon-legend/interpret-image");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeInstanceOf(FormData);
  });
});
