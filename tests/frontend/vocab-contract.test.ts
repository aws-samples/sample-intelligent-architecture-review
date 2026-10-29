// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Backend<->frontend vocabulary contract.
 *
 * The rule layer / rule type / hosting scope / icon type vocabularies are closed
 * enums that the backend embeds verbatim in model prompts and parses back out of
 * model JSON. The frontend keeps its own copies to build dropdowns and filter
 * chips. Drift is silent and nasty: the UI offers a value the validator rejects,
 * or hides one the extractor produces.
 *
 * Backend<->backend copies are already guarded by a pytest. This closes the
 * remaining gap by reading the Python sources directly, so the two sides cannot
 * diverge without a test failing.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  HOSTING_SCOPE_VALUES,
  ICON_TYPES,
  LAYER_VALUES,
  RULE_TYPE_VALUES,
} from "@/lib/api";

const BACKEND = join(__dirname, "..", "..", "backend");

function readBackend(...parts: string[]): string {
  return readFileSync(join(BACKEND, ...parts), "utf8");
}

/** Pull the quoted members out of a Python Literal[...] / set literal body. */
function pythonStringMembers(source: string, anchor: RegExp): string[] {
  const match = source.match(anchor);
  if (!match) throw new Error(`anchor not found: ${anchor}`);
  return [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

describe("vocabulary parity with the backend", () => {
  it("rule layers match app/models/standard.py", () => {
    const src = readBackend("app", "models", "standard.py");
    const backendLayers = pythonStringMembers(
      src,
      /RuleLayer\s*=\s*Literal\[([\s\S]*?)\]/,
    );
    expect([...LAYER_VALUES].sort()).toEqual(backendLayers.sort());
  });

  it("rule types match app/models/standard.py", () => {
    const src = readBackend("app", "models", "standard.py");
    const backendTypes = pythonStringMembers(
      src,
      /RuleType\s*=\s*Literal\[([\s\S]*?)\]/,
    );
    expect([...RULE_TYPE_VALUES].sort()).toEqual(backendTypes.sort());
  });

  it("hosting scopes match app/models/standard.py", () => {
    const src = readBackend("app", "models", "standard.py");
    const backendScopes = pythonStringMembers(
      src,
      /HostingScope\s*=\s*Literal\[([\s\S]*?)\]/,
    );
    expect([...HOSTING_SCOPE_VALUES].sort()).toEqual(backendScopes.sort());
  });

  it("icon types match VALID_ICON_TYPES in the icon legend store", () => {
    const src = readBackend("app", "services", "icon_legend_store.py");
    const backendIconTypes = pythonStringMembers(
      src,
      /VALID_ICON_TYPES\s*=\s*\{([\s\S]*?)\}/,
    );
    expect([...ICON_TYPES].sort()).toEqual(backendIconTypes.sort());
  });
});

/**
 * Response-shape parity for the types the UI reads field-by-field.
 *
 * The frontend asserts API shapes with `as T` and no runtime validation, so a
 * renamed backend field is invisible until a user notices wrong data. SlideStatus
 * drifted exactly this way: the frontend declared `rules_saved` while the model
 * has `rule_count`, so `undefined > 0` was false and EVERY slide of a PPTX
 * rendered "no rules" no matter how many it produced.
 */
describe("response-shape parity with the backend", () => {
  /** Field names declared on a Pydantic model (`name: type = ...` lines). */
  function pydanticFields(source: string, className: string): string[] {
    const start = source.indexOf(`class ${className}(BaseModel):`);
    if (start === -1) throw new Error(`model not found: ${className}`);
    // Up to the next top-level class, or end of file.
    const rest = source.slice(start + 1);
    const end = rest.search(/\nclass \w+/);
    const body = end === -1 ? rest : rest.slice(0, end);
    return [...body.matchAll(/^\s{4}(\w+):\s/gm)].map((m) => m[1]);
  }

  /** Field names declared on a TS interface. */
  function tsFields(source: string, name: string): string[] {
    const match = source.match(
      new RegExp(`export interface ${name} \\{([\\s\\S]*?)\\n\\}`),
    );
    if (!match) throw new Error(`interface not found: ${name}`);
    return [...match[1].matchAll(/^\s{2}(\w+)\??:/gm)].map((m) => m[1]);
  }

  it("SlideStatus fields all exist on the backend model", () => {
    const backend = pydanticFields(
      readBackend("app", "models", "document.py"),
      "SlideStatus",
    );
    const frontend = tsFields(
      readFileSync(join(__dirname, "..", "..", "frontend", "src", "lib", "api.ts"), "utf8"),
      "SlideStatus",
    );
    const unknown = frontend.filter((f) => !backend.includes(f));
    expect(unknown).toEqual([]);
  });

  it("SlideStatus declares the fields the UI needs to tell failure from empty", () => {
    const frontend = tsFields(
      readFileSync(join(__dirname, "..", "..", "frontend", "src", "lib", "api.ts"), "utf8"),
      "SlideStatus",
    );
    for (const required of ["rule_count", "status", "error"]) {
      expect(frontend).toContain(required);
    }
  });
});
