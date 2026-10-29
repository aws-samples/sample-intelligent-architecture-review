// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ChevronRight,
  LoaderCircle,
  LogOut,
  Menu,
  Sparkles,
} from "lucide-react";
import { BrandMark } from "./BrandMark";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth/useAuth";
import { useCuratorJobs } from "@/lib/use-curator-jobs";

const pageTitles: Record<string, string> = {
  "/": "Dashboard",
  "/documents": "Documents",
  "/validation": "Validation results",
  "/standards": "Standards library",
  "/standards/curate": "AI rule curator",
  "/standards/references": "Reference context",
  "/standards/icon-legend": "Custom icon legend",
  "/admin/roles": "Users & roles",
  "/query": "Ask your documents",
  "/getting-started": "Getting started",
};

function normalizePath(pathname: string): string {
  return pathname.replace(/\/$/, "") || "/";
}

function getPageTitle(pathname: string): string {
  const normalized = normalizePath(pathname);
  if (pageTitles[normalized]) return pageTitles[normalized];
  if (normalized.startsWith("/documents/")) return "Document detail";
  if (normalized.startsWith("/validation/")) return "Validation detail";
  return "Dashboard";
}

function getBreadcrumbs(pathname: string): { label: string; href?: string }[] {
  const normalized = normalizePath(pathname);
  const crumbs: { label: string; href?: string }[] = [
    { label: "Dashboard", href: "/" },
  ];

  if (normalized === "/") return crumbs;
  if (normalized.startsWith("/documents")) {
    crumbs.push({ label: "Documents", href: "/documents/" });
    if (normalized !== "/documents") crumbs.push({ label: "Detail" });
  } else if (normalized.startsWith("/validation")) {
    crumbs.push({ label: "Validations", href: "/validation/" });
    if (normalized !== "/validation") crumbs.push({ label: "Detail" });
  } else if (normalized.startsWith("/standards")) {
    crumbs.push({ label: "Standards", href: "/standards/" });
    if (normalized === "/standards/curate") crumbs.push({ label: "Curate" });
    if (normalized === "/standards/references") crumbs.push({ label: "References" });
    if (normalized === "/standards/icon-legend") crumbs.push({ label: "Icon legend" });
  } else if (normalized === "/admin/roles") {
    crumbs.push({ label: "Users & roles" });
  } else if (normalized === "/query") {
    crumbs.push({ label: "Query" });
  } else if (normalized === "/getting-started") {
    crumbs.push({ label: "Getting started" });
  }

  return crumbs;
}

export function Header({ onMenuClick }: { onMenuClick: () => void }) {
  const pathname = usePathname();
  const title = getPageTitle(pathname);
  const breadcrumbs = getBreadcrumbs(pathname);
  const [activeJobCount, setActiveJobCount] = useState(0);
  const { activeJobs: curatorJobs } = useCuratorJobs();
  const { user, signOut } = useAuth();
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);

  const email = user?.email || user?.username || "";
  const initials =
    (email.split("@")[0] || "U")
      .split(/[._-]/)
      .map((part) => part[0])
      .filter(Boolean)
      .slice(0, 2)
      .join("")
      .toUpperCase() || "U";

  useEffect(() => {
    async function fetchPipeline() {
      try {
        const status = await api.getPipelineStatus();
        setActiveJobCount(status.active_jobs);
      } catch {
        setActiveJobCount(0);
      }
    }

    fetchPipeline();
    const interval = setInterval(fetchPipeline, 5000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (
        userMenuRef.current &&
        !userMenuRef.current.contains(event.target as Node)
      ) {
        setUserMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  return (
    <header className="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-slate-200/80 bg-white/95 px-4 backdrop-blur-md dark:border-white/8 dark:bg-[#121918]/95 sm:px-6 xl:px-8">
      <div className="flex min-w-0 items-center gap-3">
        <button
          type="button"
          onClick={onMenuClick}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-slate-200 text-slate-600 transition-colors hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900 dark:border-white/10 dark:text-slate-300 dark:hover:bg-white/8 dark:hover:text-white lg:hidden"
          aria-label="Open navigation"
        >
          <Menu className="h-4.5 w-4.5" />
        </button>

        <BrandMark className="sm:hidden" />

        <div className="hidden min-w-0 sm:block">
          <nav
            className="mb-1 flex items-center gap-1 text-[10px] text-slate-400 dark:text-slate-500"
            aria-label="Breadcrumb"
          >
            {breadcrumbs.map((crumb, index) => (
              <span key={`${crumb.label}-${index}`} className="flex items-center gap-1">
                {index > 0 && <ChevronRight className="h-2.5 w-2.5" />}
                {crumb.href ? (
                  <Link
                    href={crumb.href}
                    className="transition-colors hover:text-[var(--color-brand-primary)]"
                  >
                    {crumb.label}
                  </Link>
                ) : (
                  <span className="font-medium text-slate-600 dark:text-slate-300">
                    {crumb.label}
                  </span>
                )}
              </span>
            ))}
          </nav>
          <h1 className="truncate text-base font-semibold leading-none text-slate-900 dark:text-slate-50">
            {title}
          </h1>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2 sm:gap-3">
        {curatorJobs.length > 0 && (
          <Link
            href="/standards/curate"
            className="hidden h-8 items-center gap-2 rounded-lg border border-teal-200 bg-teal-50 px-2.5 text-[11px] font-medium text-teal-800 transition-colors hover:border-teal-300 hover:bg-teal-100 dark:border-teal-400/20 dark:bg-teal-400/10 dark:text-teal-200 dark:hover:bg-teal-400/15 md:flex"
            title="Open the AI curator"
          >
            <Sparkles className="h-3.5 w-3.5" />
            Curator working{curatorJobs.length > 1 ? ` (${curatorJobs.length})` : ""}
          </Link>
        )}

        {activeJobCount > 0 && (
          <Link
            href="/documents"
            className="flex h-8 items-center gap-2 rounded-lg border border-sky-200 bg-sky-50 px-2.5 text-[11px] font-medium text-sky-800 transition-colors hover:bg-sky-100 dark:border-sky-400/20 dark:bg-sky-400/10 dark:text-sky-200 dark:hover:bg-sky-400/15"
            title="View processing documents"
          >
            <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
            <span className="hidden sm:inline">{activeJobCount} processing</span>
            <span className="sm:hidden">{activeJobCount}</span>
          </Link>
        )}

        <span className="hidden text-[10px] text-slate-400 dark:text-slate-500 xl:inline">
          {new Date().toLocaleDateString("en-GB", {
            day: "numeric",
            month: "short",
            year: "numeric",
          })}
        </span>

        <div className="relative" ref={userMenuRef}>
          <button
            type="button"
            onClick={() => setUserMenuOpen((value) => !value)}
            aria-label="Account menu"
            aria-haspopup="menu"
            aria-expanded={userMenuOpen}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-[var(--color-brand-primary)] text-[10px] font-semibold text-white transition-colors hover:bg-[var(--color-brand-primary-hover)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/30 focus-visible:ring-offset-2"
          >
            {initials}
          </button>

          {userMenuOpen && (
            <div
              role="menu"
              className="animate-fade-in-scale absolute right-0 mt-2 w-60 origin-top-right overflow-hidden rounded-lg border border-slate-200 bg-white shadow-xl shadow-slate-900/10 ring-1 ring-slate-900/5 dark:border-white/10 dark:bg-[#17201f] dark:shadow-black/40 dark:ring-white/10"
            >
              <div className="border-b border-slate-100 px-4 py-3 dark:border-white/8">
                <div className="text-[10px] font-medium uppercase tracking-normal text-slate-400 dark:text-slate-500">
                  Signed in as
                </div>
                <div className="mt-1 truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                  {email || "Unknown user"}
                </div>
              </div>
              <button
                type="button"
                role="menuitem"
                onClick={async () => {
                  setUserMenuOpen(false);
                  await signOut();
                }}
                className="flex w-full items-center gap-2 px-4 py-2.5 text-sm text-slate-700 transition-colors hover:bg-slate-50 hover:text-[var(--color-brand-primary)] dark:text-slate-200 dark:hover:bg-white/5"
              >
                <LogOut className="h-4 w-4" />
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
