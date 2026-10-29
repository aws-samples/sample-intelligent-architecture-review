// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

/**
 * Admin-only: Users & Roles management page.
 *
 * Lists all explicit role assignments, and lets the admin assign/change/
 * remove a user's role. Non-admin users are 403'd server-side and this
 * page redirects them if they somehow navigate here.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { PageHero } from "@/components/layout/PageHero";
import { api, UserRole, UserRoleAssignment } from "@/lib/api";
import { useRole } from "@/lib/use-role";

const ROLES: UserRole[] = ["admin", "validator", "curator", "viewer"];

const ROLE_BADGE: Record<UserRole, string> = {
  admin: "bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-500/20",
  validator: "bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/20",
  curator: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20",
  viewer: "bg-slate-100 text-slate-600 border-slate-200 dark:bg-white/8 dark:text-slate-300 dark:border-white/10",
};

export default function RolesAdminPage() {
  const { isAdmin, email: myEmail, loading: roleLoading } = useRole();
  const router = useRouter();
  const [users, setUsers] = useState<UserRoleAssignment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  // Form state for adding/editing a role.
  const [formSub, setFormSub] = useState("");
  const [formEmail, setFormEmail] = useState("");
  const [formRole, setFormRole] = useState<UserRole>("viewer");

  useEffect(() => {
    if (!roleLoading && !isAdmin) {
      router.replace("/");
    }
  }, [roleLoading, isAdmin, router]);

  const load = async () => {
    try {
      const res = await api.listUserRoles();
      setUsers(res.users);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load roles");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    if (isAdmin) {
      void (async () => {
        if (!cancelled) await load();
      })();
    }
    return () => { cancelled = true; };
  }, [isAdmin]);

  const handleAssign = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formSub.trim()) return;
    setActing(true);
    setError(null);
    try {
      await api.setUserRole(formSub.trim(), formRole, formEmail.trim());
      setFormSub("");
      setFormEmail("");
      setFormRole("viewer");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to assign role");
    } finally {
      setActing(false);
    }
  };

  const handleRemove = async (sub: string, email: string) => {
    // Removing your own assignment drops you to viewer and locks you out of this
    // page — with no way back if you are the only admin.
    const isSelf = Boolean(myEmail) && email.toLowerCase() === myEmail!.toLowerCase();
    const warning = isSelf
      ? "Remove your OWN role assignment? You will drop to viewer and lose access to this page."
      : `Remove the role assignment for ${email || sub}?`;
    if (!window.confirm(warning)) return;
    setActing(true);
    setError(null);
    try {
      await api.removeUserRole(sub);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove role");
    } finally {
      setActing(false);
    }
  };

  const changeRole = async (sub: string, newRole: UserRole, email: string) => {
    // A role <select> commits instantly, so demoting yourself is one stray click
    // away from locking yourself out. Confirm that specific case.
    const isSelf = Boolean(myEmail) && email.toLowerCase() === myEmail!.toLowerCase();
    if (isSelf && newRole !== "admin") {
      const ok = window.confirm(
        `Change your OWN role to ${newRole}? You will immediately lose admin ` +
        "access, including this page, and cannot undo it yourself.",
      );
      if (!ok) {
        await load();  // snap the select back to the stored value
        return;
      }
    }
    setActing(true);
    setError(null);
    try {
      await api.setUserRole(sub, newRole, email);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to change role");
    } finally {
      setActing(false);
    }
  };

  if (roleLoading || !isAdmin) return null;

  return (
    <div className="animate-in space-y-5 fade-in duration-300">
      <PageHero
        title="Users & Roles"
        subtitle="Manage who can validate, curate, or administer this workspace."
      />

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2.5 text-xs font-medium text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300">
          {error}
        </div>
      )}

      {/* Assign role form */}
      <div className="rounded-xl border border-slate-200/80 bg-white p-4 dark:border-white/8 dark:bg-white/[0.02]">
        <h2 className="mb-3 text-sm font-semibold text-slate-900 dark:text-slate-100">
          Assign role
        </h2>
        <form onSubmit={handleAssign} className="flex flex-wrap items-end gap-3">
          <div className="flex-1 min-w-[180px]">
            <label className="mb-1 block text-[11px] font-medium text-slate-500">
              Cognito sub (user ID)
            </label>
            <input
              type="text"
              value={formSub}
              onChange={(e) => setFormSub(e.target.value)}
              placeholder="e.g. abc-123-def"
              required
              className="h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm outline-none focus:border-[var(--color-brand-primary)]/40 focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/10 dark:bg-white/5 dark:text-white"
            />
          </div>
          <div className="flex-1 min-w-[160px]">
            <label className="mb-1 block text-[11px] font-medium text-slate-500">
              Email (display only)
            </label>
            <input
              type="email"
              value={formEmail}
              onChange={(e) => setFormEmail(e.target.value)}
              placeholder="user@company.com"
              className="h-9 w-full rounded-md border border-slate-200 bg-white px-3 text-sm outline-none focus:border-[var(--color-brand-primary)]/40 focus:ring-2 focus:ring-[var(--color-brand-primary)]/15 dark:border-white/10 dark:bg-white/5 dark:text-white"
            />
          </div>
          <div className="w-36">
            <label className="mb-1 block text-[11px] font-medium text-slate-500">
              Role
            </label>
            <select
              value={formRole}
              onChange={(e) => setFormRole(e.target.value as UserRole)}
              className="h-9 w-full rounded-md border border-slate-200 bg-white px-2 text-sm outline-none dark:border-white/10 dark:bg-white/5 dark:text-white"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            disabled={acting || !formSub.trim()}
            className="h-9 rounded-md bg-[var(--color-brand-primary)] px-4 text-xs font-semibold text-white transition-all hover:bg-[var(--color-brand-primary-hover)] disabled:opacity-50"
          >
            Assign
          </button>
        </form>
      </div>

      {/* Existing assignments */}
      <div className="rounded-xl border border-slate-200/80 bg-white dark:border-white/8 dark:bg-white/[0.02]">
        <div className="border-b border-slate-200/60 px-4 py-3 dark:border-white/8">
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            Current role assignments
            <span className="ml-2 text-xs font-normal text-slate-500">
              {users.length} user{users.length === 1 ? "" : "s"}
            </span>
          </h2>
        </div>
        {loading ? (
          <div className="px-4 py-8 text-center text-sm text-slate-500">Loading...</div>
        ) : users.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-slate-500">
            No explicit role assignments yet. Users default to <strong>viewer</strong> unless assigned here or matched by BOOTSTRAP_ADMIN_EMAIL.
          </div>
        ) : (
          <ul className="divide-y divide-slate-200/60 dark:divide-white/8">
            {users.map((u) => (
              <li key={u.sub} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                    {u.email || u.sub}
                  </p>
                  {u.email && (
                    <p className="truncate text-[11px] text-slate-500 font-mono">{u.sub}</p>
                  )}
                </div>
                <select
                  value={u.role}
                  onChange={(e) => changeRole(u.sub, e.target.value as UserRole, u.email || "")}
                  disabled={acting}
                  className={
                    "h-7 rounded-full border px-2.5 text-[11px] font-semibold uppercase tracking-normal outline-none " +
                    ROLE_BADGE[u.role]
                  }
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => handleRemove(u.sub, u.email || "")}
                  disabled={acting}
                  className="text-[11px] font-medium text-slate-500 hover:text-rose-600 disabled:opacity-50"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
