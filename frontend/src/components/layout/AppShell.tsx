// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useCallback, useEffect, useState } from "react";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";
import { api } from "@/lib/api";

export function AppShell({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [backendConnected, setBackendConnected] = useState<boolean | null>(null);
  const closeMobileNavigation = useCallback(() => setMobileOpen(false), []);

  useEffect(() => {
    async function checkBackend() {
      setBackendConnected(await api.checkHealth());
    }

    checkBackend();
    const interval = setInterval(checkBackend, 15000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="min-h-screen bg-background">
      <Sidebar
        collapsed={collapsed}
        mobileOpen={mobileOpen}
        onMobileClose={closeMobileNavigation}
        onToggle={() => setCollapsed((value) => !value)}
      />

      <div
        className={
          "flex min-h-screen flex-col transition-[padding] duration-200 " +
          (collapsed ? "lg:pl-[72px]" : "lg:pl-[248px]")
        }
      >
        <Header onMenuClick={() => setMobileOpen(true)} />

        <main className="app-surface flex-1 px-4 py-5 sm:px-6 sm:py-6 xl:px-8 xl:py-8">
          <div className="mx-auto w-full max-w-[1600px]">{children}</div>
        </main>

        <footer className="border-t border-slate-200/70 bg-white px-4 py-3 dark:border-white/8 dark:bg-[#121918] sm:px-6 xl:px-8">
          <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-2 text-[10px] text-slate-500 dark:text-slate-400">
            <span>Architecture Compliance Review · Standards assurance workspace</span>
            <div className="flex items-center gap-2">
              <span
                className={
                  "h-1.5 w-1.5 rounded-full " +
                  (backendConnected === null
                    ? "animate-pulse bg-slate-400"
                    : backendConnected
                      ? "bg-[var(--color-status-pass)]"
                      : "bg-[var(--color-status-fail)]")
                }
                aria-hidden="true"
              />
              <span>
                {backendConnected === null
                  ? "Checking service"
                  : backendConnected
                    ? "Service connected"
                    : "Service unavailable"}
              </span>
            </div>
          </div>
        </footer>
      </div>
    </div>
  );
}
