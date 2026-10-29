// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useState, type ComponentType } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BookOpen,
  ClipboardList,
  FileStack,
  LayoutDashboard,
  MessageSquareText,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  ScanSearch,
  ShieldCheck,
  Sun,
  X,
} from "lucide-react";
import { BrandMark } from "./BrandMark";
import { cn } from "@/lib/utils";
import { api, UserRole } from "@/lib/api";
import { useRole } from "@/lib/use-role";

interface NavItem {
  label: string;
  href: string;
  icon: ComponentType<{ className?: string; strokeWidth?: number }>;
  count?: number;
}

interface NavSection {
  title: string;
  items: NavItem[];
}

const navSections: NavSection[] = [
  {
    title: "Review",
    items: [
      { label: "Dashboard", href: "/", icon: LayoutDashboard },
      { label: "Documents", href: "/documents", icon: FileStack, count: 0 },
      { label: "Standards", href: "/standards", icon: ClipboardList, count: 0 },
      { label: "Validations", href: "/validation", icon: ScanSearch, count: 0 },
    ],
  },
  {
    title: "Explore",
    items: [
      { label: "Ask documents", href: "/query", icon: MessageSquareText },
    ],
  },
];

// Role badge colors tuned for the dark sidebar surface (white-on-dark),
// mirroring the admin Users & Roles page hues so a role reads the same
// everywhere in the app.
const ROLE_BADGE: Record<UserRole, string> = {
  admin: "bg-rose-400/15 text-rose-200 ring-1 ring-inset ring-rose-300/25",
  validator: "bg-sky-400/15 text-sky-200 ring-1 ring-inset ring-sky-300/25",
  curator: "bg-emerald-400/15 text-emerald-200 ring-1 ring-inset ring-emerald-300/25",
  viewer: "bg-white/10 text-white/70 ring-1 ring-inset ring-white/15",
};

interface SidebarProps {
  collapsed: boolean;
  mobileOpen: boolean;
  onMobileClose: () => void;
  onToggle: () => void;
}

export function Sidebar({
  collapsed,
  mobileOpen,
  onMobileClose,
  onToggle,
}: SidebarProps) {
  const pathname = usePathname();
  const { role, email, isAdmin, loading: roleLoading } = useRole();
  const [isDark, setIsDark] = useState(false);
  const [sections, setSections] = useState<NavSection[]>(navSections);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      setIsDark(document.documentElement.classList.contains("dark"));
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    onMobileClose();
  }, [pathname, onMobileClose]);

  useEffect(() => {
    async function fetchCounts() {
      try {
        const [sddRes, standardsDocsRes, standardsRes, valsRes] =
          await Promise.all([
            api.listDocuments("sdd"),
            api.listDocuments("standards"),
            api.listStandards(),
            api.listValidations(),
          ]);

        const allDocsTotal = sddRes.total + standardsDocsRes.total;
        setSections(
          navSections.map((section) => ({
            ...section,
            items: section.items.map((item) => {
              if (item.href === "/documents") return { ...item, count: allDocsTotal };
              if (item.href === "/standards") return { ...item, count: standardsRes.total };
              if (item.href === "/validation") return { ...item, count: valsRes.total };
              return item;
            }),
          })),
        );
      } catch {
        // Navigation remains usable if counts cannot be refreshed.
      }
    }

    fetchCounts();
    const interval = setInterval(fetchCounts, 30000);
    // Refresh immediately when standards change (e.g. a reviewer approves
    // extracted rules) so the count updates without a page refresh or the
    // 30s poll delay. Also refresh when the tab regains focus.
    const onChanged = () => { void fetchCounts(); };
    window.addEventListener("standards-changed", onChanged);
    window.addEventListener("focus", onChanged);
    return () => {
      clearInterval(interval);
      window.removeEventListener("standards-changed", onChanged);
      window.removeEventListener("focus", onChanged);
    };
  }, []);

  return (
    <>
      <button
        type="button"
        onClick={onMobileClose}
        className={cn(
          "fixed inset-0 z-40 bg-slate-950/45 backdrop-blur-[2px] transition-opacity lg:hidden",
          mobileOpen ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0",
        )}
        aria-label="Close navigation"
      />

      <aside
        className={cn(
          "workspace-nav fixed inset-y-0 left-0 z-50 flex w-[248px] -translate-x-full flex-col border-r border-white/8 transition-[width,transform] duration-200 lg:z-40 lg:translate-x-0",
          mobileOpen && "translate-x-0",
          collapsed && "lg:w-[72px]",
        )}
      >
        <div className="flex h-16 items-center justify-between border-b border-white/8 px-4">
          <BrandMark compact={collapsed} inverse />
          <button
            type="button"
            onClick={onMobileClose}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-white/60 transition-colors hover:bg-white/8 hover:text-white lg:hidden"
            aria-label="Close navigation"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-5" aria-label="Primary">
          {sections.map((section, sectionIndex) => (
            <div
              key={section.title}
              className={cn(sectionIndex > 0 && "mt-7")}
            >
              {!collapsed && (
                <h2 className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-normal text-white/35">
                  {section.title}
                </h2>
              )}
              <div className="space-y-1">
                {section.items.map((item) => {
                  const active =
                    item.href === "/"
                      ? pathname === "/"
                      : pathname.startsWith(item.href);
                  const Icon = item.icon;

                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      title={collapsed ? item.label : undefined}
                      className={cn(
                        "group relative flex h-10 items-center justify-between gap-3 rounded-lg px-3 text-[13px] font-medium transition-colors",
                        active
                          ? "brand-nav-active"
                          : "text-white/62 hover:bg-white/7 hover:text-white",
                        collapsed && "lg:justify-center lg:px-2",
                      )}
                    >
                      <span
                        className={cn(
                          "flex min-w-0 items-center gap-3",
                          collapsed && "lg:gap-0",
                        )}
                      >
                        <Icon className="h-4 w-4 shrink-0" strokeWidth={1.8} />
                        <span className={cn("truncate", collapsed && "lg:hidden")}>
                          {item.label}
                        </span>
                      </span>
                      {item.count !== undefined && (
                        <span
                          className={cn(
                            "font-mono text-[10px] tabular-nums text-white/38 group-hover:text-white/65",
                            active && "text-teal-100/75",
                            collapsed && "lg:hidden",
                          )}
                        >
                          {item.count}
                        </span>
                      )}
                    </Link>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="space-y-1 border-t border-white/8 p-3">
          {/* Current role + identity indicator (RBAC). Always visible so a
              user can see what they can do; links to the admin Users & Roles
              page when the caller is an admin. This is UX only — the backend
              require_role() dependency is the real gate. */}
          {!roleLoading && role && (() => {
            const label = `Signed in${email ? ` as ${email}` : ""}, role ${role}${
              isAdmin ? " (open Users & Roles)" : ""
            }`;
            const badge = (
              <span
                className={cn(
                  "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-normal",
                  ROLE_BADGE[role],
                )}
              >
                {role}
              </span>
            );

            if (collapsed) {
              const dot = (
                <span
                  className={cn(
                    "flex h-6 w-6 items-center justify-center rounded-full",
                    ROLE_BADGE[role],
                  )}
                  aria-hidden="true"
                >
                  <ShieldCheck className="h-3.5 w-3.5" strokeWidth={2} />
                </span>
              );
              const inner = (
                <span className="mx-auto flex items-center justify-center" title={`${role}${email ? ` · ${email}` : ""}`}>
                  {dot}
                </span>
              );
              return isAdmin ? (
                <Link
                  href="/admin/roles"
                  aria-label={label}
                  className="flex h-9 w-full items-center justify-center rounded-lg transition-colors hover:bg-white/7"
                >
                  {inner}
                </Link>
              ) : (
                <div className="flex h-9 w-full items-center justify-center" aria-label={label} role="status">
                  {inner}
                </div>
              );
            }

            const content = (
              <>
                <span className="flex min-w-0 items-center gap-2">
                  <ShieldCheck className="h-4 w-4 shrink-0 text-white/45" strokeWidth={1.8} />
                  <span className="truncate text-[12px] text-white/70">
                    {email || "Signed in"}
                  </span>
                </span>
                {badge}
              </>
            );

            return isAdmin ? (
              <Link
                href="/admin/roles"
                aria-label={label}
                className="flex h-9 w-full items-center justify-between gap-2 rounded-lg px-3 transition-colors hover:bg-white/7"
              >
                {content}
              </Link>
            ) : (
              <div
                className="flex h-9 w-full items-center justify-between gap-2 rounded-lg px-3"
                aria-label={label}
                role="status"
              >
                {content}
              </div>
            );
          })()}

          <Link
            href="/getting-started/"
            title={collapsed ? "Getting started" : undefined}
            className={cn(
              "flex h-9 w-full items-center gap-2.5 rounded-lg px-3 text-[12px] text-white/58 transition-colors hover:bg-white/7 hover:text-white",
              collapsed && "lg:justify-center lg:px-2",
            )}
          >
            <BookOpen className="h-4 w-4 shrink-0" strokeWidth={1.8} />
            <span className={cn(collapsed && "lg:hidden")}>Getting started</span>
          </Link>

          <button
            type="button"
            onClick={() => {
              const next = !isDark;
              document.documentElement.classList.toggle("dark", next);
              setIsDark(next);
              try {
                localStorage.setItem("theme", next ? "dark" : "light");
              } catch {
                // Theme still applies for the current session.
              }
            }}
            title={collapsed ? (isDark ? "Use light mode" : "Use dark mode") : undefined}
            className={cn(
              "flex h-9 w-full items-center gap-2.5 rounded-lg px-3 text-[12px] text-white/58 transition-colors hover:bg-white/7 hover:text-white",
              collapsed && "lg:justify-center lg:px-2",
            )}
          >
            {isDark ? (
              <Sun className="h-4 w-4 shrink-0" strokeWidth={1.8} />
            ) : (
              <Moon className="h-4 w-4 shrink-0" strokeWidth={1.8} />
            )}
            <span className={cn(collapsed && "lg:hidden")}>
              {isDark ? "Light mode" : "Dark mode"}
            </span>
          </button>

          <button
            type="button"
            onClick={onToggle}
            className="hidden h-9 w-full items-center gap-2.5 rounded-lg px-3 text-[12px] text-white/58 transition-colors hover:bg-white/7 hover:text-white lg:flex"
            title={collapsed ? "Expand navigation" : "Collapse navigation"}
          >
            {collapsed ? (
              <PanelLeftOpen className="mx-auto h-4 w-4" strokeWidth={1.8} />
            ) : (
              <>
                <PanelLeftClose className="h-4 w-4" strokeWidth={1.8} />
                <span>Collapse navigation</span>
              </>
            )}
          </button>

          {!collapsed && (
            <div className="px-3 pt-3 text-[10px] text-white/30">
              Secure review workspace · v1.0
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
