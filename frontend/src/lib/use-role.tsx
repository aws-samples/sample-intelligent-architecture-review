// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * RBAC — client-side role awareness.
 *
 * Fetches the caller's role once (GET /api/roles/me) and exposes it plus
 * capability helpers matching the backend permission matrix. This is
 * DEFENSE IN DEPTH / UX only — the backend require_role() dependency is the
 * real gate. We hide actions a role can't perform so users don't hit 403s,
 * but a determined client bypassing this still can't mutate anything.
 *
 * Capability matrix:
 *   admin     — everything
 *   validator — upload SDD + run validation; view results/library; chat KB
 *   curator   — upload standards, edit library, approve/reject rules,
 *               Curate-with-AI; view results; chat KB
 *   viewer    — read-only + chat KB
 */

import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, UserRole } from "@/lib/api";

interface RoleState {
  role: UserRole | null;   // null while loading / unknown
  email: string | null;    // caller's email (from GET /api/roles/me); null until loaded
  loading: boolean;
  // Capabilities (derived from role).
  canValidate: boolean;    // upload SDD + run validation
  canCurate: boolean;      // upload standards, edit library, approve/reject
  isAdmin: boolean;
}

const DEFAULT: RoleState = {
  role: null,
  email: null,
  loading: true,
  canValidate: false,
  canCurate: false,
  isAdmin: false,
};

function deriveCaps(role: UserRole | null): Omit<RoleState, "role" | "email" | "loading"> {
  const isAdmin = role === "admin";
  return {
    isAdmin,
    canValidate: isAdmin || role === "validator",
    canCurate: isAdmin || role === "curator",
  };
}

const RoleContext = createContext<RoleState>(DEFAULT);

export function RoleProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<RoleState>(DEFAULT);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const me = await api.getMyRole();
        if (cancelled) return;
        setState({ role: me.role, email: me.email ?? null, loading: false, ...deriveCaps(me.role) });
      } catch {
        if (cancelled) return;
        // On failure, fail CLOSED in the UI (viewer — hide write actions).
        setState({ role: "viewer", email: null, loading: false, ...deriveCaps("viewer") });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return <RoleContext.Provider value={state}>{children}</RoleContext.Provider>;
}

export function useRole(): RoleState {
  return useContext(RoleContext);
}
