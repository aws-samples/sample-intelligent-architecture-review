// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { defineConfig } from "vitest/config";

/**
 * Frontend unit tests.
 *
 * The specs live in the repo-root /tests/frontend tree (alongside the backend,
 * infra, and e2e suites) so there is one place to find every test in the repo.
 * Two consequences of tests living outside frontend/:
 *   - Vite's filesystem guard must be widened to the repo root, and
 *   - the `@/*` alias is declared here rather than inferred from tsconfig
 *     (whose `include` only covers frontend/).
 */
const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");
const testsDir = resolve(repoRoot, "tests", "frontend");

export default defineConfig({
  resolve: {
    alias: [
      // Collapse both spellings of the auth service onto ONE module id so a
      // single vi.mock() intercepts it. api.ts imports it relatively
      // ("./auth/authService") while tests reference "@/lib/auth/authService";
      // without this they'd be two separate modules and the mock would miss.
      {
        find: /^(?:@\/lib|\.)\/auth\/authService$/,
        replacement: resolve(here, "src/lib/auth/authService.ts"),
      },
      { find: "@", replacement: resolve(here, "src") },
      // Tests live outside frontend/, so bare specifiers can't be resolved from
      // their own directory. Anchor the app's runtime deps explicitly.
      {
        find: "aws-amplify/auth",
        replacement: resolve(here, "node_modules/aws-amplify/dist/esm/auth/index.mjs"),
      },
    ],
  },
  server: {
    fs: { allow: [repoRoot] },
  },
  test: {
    environment: "jsdom",
    root: here,
    dir: testsDir,
    include: ["**/*.test.{ts,tsx}"],
    globals: true,
    setupFiles: [resolve(testsDir, "setup.ts")],
    coverage: {
      provider: "v8",
      reportsDirectory: resolve(repoRoot, "coverage", "frontend"),
      reporter: ["text", "cobertura"],
      // Coverage is scoped to code with BRANCHES worth testing: the API client,
      // hooks, and the stateful components/pages. Presentational files are
      // excluded — asserting that a wrapper renders its children inflates the
      // number without catching defects, and it would crowd out the logic that
      // does need tests.
      include: [
        "src/lib/**",
        "src/components/**",
        "src/app/**/client.tsx",
      ],
      exclude: [
        // shadcn/ui primitives: vendored markup, no project logic.
        "src/components/ui/**",
        // Next.js shells: layout/page wrappers that only mount a client
        // component or export metadata.
        "src/app/**/layout.tsx",
        "src/app/**/page.tsx",
        "src/lib/mock-data.ts",
        "**/*.d.ts",
      ],
      thresholds: {
        // RATCHET, not a target. Set just below the current measured value so
        // coverage cannot regress, and raised as suites land. Component and
        // page tests are still to be written, so this is well short of the
        // backend's 68% — see the coverage note in tests/README.md.
        lines: 6,
        statements: 6,
        // v8 counts every optional-chain and default parameter as a branch, so
        // these two run well ahead of the line figure and are not comparable.
        functions: 60,
        branches: 55,
      },
    },
  },
});
