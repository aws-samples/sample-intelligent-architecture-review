// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import Image from "next/image";
import { useState, type ReactNode } from "react";
import {
  ArrowRight,
  Eye,
  EyeOff,
  LoaderCircle,
  LockKeyhole,
  Mail,
} from "lucide-react";
import { BrandMark } from "@/components/layout/BrandMark";
import { useAuth } from "@/lib/auth/useAuth";
import { authService, ssoProvider } from "@/lib/auth/authService";
import { RoleProvider } from "@/lib/use-role";

export function AuthGate({ children }: { children: ReactNode }) {
  const { user, isLoading, signIn, refreshUser } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [needsNewPassword, setNeedsNewPassword] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // While the hosted-UI/SAML redirect is completing its code↔token exchange
  // (we're on /callback or carry an OAuth ?code=/?error=), show a spinner
  // rather than flashing the login form before the session materializes.
  const isOAuthCallback =
    typeof window !== "undefined" &&
    (window.location.pathname.replace(/\/$/, "").endsWith("/callback") ||
      /[?&](code|error)=/.test(window.location.search));

  if (isLoading || (isOAuthCallback && !user)) {
    return (
      <div className="login-shell flex min-h-screen items-center justify-center">
        <div className="text-center">
          <LoaderCircle className="mx-auto h-6 w-6 animate-spin text-[var(--color-brand-primary)]" />
          <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
            {isOAuthCallback ? "Completing sign-in" : "Opening workspace"}
          </p>
        </div>
      </div>
    );
  }

  if (user) return <RoleProvider>{children}</RoleProvider>;

  const handleSignIn = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await signIn(email, password);
      if (result.success) return;
      if (result.nextStep === "CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED") {
        setNeedsNewPassword(true);
        return;
      }
      setError(result.error || "Sign in failed");
    } finally {
      setSubmitting(false);
    }
  };

  const handleSSO = async () => {
    setError(null);
    setSubmitting(true);
    // Navigates away on success; only returns here on failure to start.
    const result = await authService.signInWithSSO(ssoProvider as string);
    if (!result.success) {
      setError(result.error || "Failed to start SSO sign in");
      setSubmitting(false);
    }
  };

  const handleNewPassword = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await authService.confirmNewPassword(newPassword);
      if (result.success) {
        await refreshUser();
        return;
      }
      setError(result.error || "Failed to set new password");
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass =
    "h-11 w-full rounded-lg border border-slate-200 bg-white pl-10 pr-11 text-sm text-slate-900 outline-none transition-all placeholder:text-slate-400 hover:border-slate-300 focus:border-[var(--color-brand-primary)]/50 focus:ring-3 focus:ring-[var(--color-brand-primary)]/12 dark:border-white/12 dark:bg-white/5 dark:text-white dark:placeholder:text-slate-500 dark:hover:border-white/20";

  return (
    <div className="login-shell grid min-h-screen w-full lg:grid-cols-[minmax(0,1.15fr)_minmax(420px,0.85fr)]">
      <section className="relative hidden min-h-screen overflow-hidden lg:block">
        <Image
          src="/login-bg.jpg"
          alt=""
          fill
          priority
          sizes="60vw"
          className="login-visual object-cover"
        />
        <div className="login-visual-overlay absolute inset-0" />

        <div className="absolute inset-x-0 bottom-0 p-10 xl:p-14">
          <div className="max-w-lg">
            <div className="mb-5 h-px w-12 bg-teal-300" />
            <p className="text-3xl font-semibold leading-tight tracking-normal text-white xl:text-4xl">
              Clear evidence for resilient architecture.
            </p>
            <p className="mt-3 max-w-md text-sm leading-relaxed text-white/70">
              Review system designs against governed standards and keep every
              decision traceable.
            </p>
          </div>
        </div>
      </section>

      <section className="relative flex min-h-screen flex-col bg-white px-6 py-6 dark:bg-[#111817] sm:px-10 lg:px-12 xl:px-16">
        <BrandMark className="shrink-0" />

        <div className="flex flex-1 items-center justify-center py-10">
          <div className="animate-fade-in-scale w-full max-w-sm">
            <div className="mb-8">
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-normal text-[var(--color-brand-primary)]">
                Secure workspace
              </p>
              <h1 className="text-3xl font-semibold tracking-normal text-slate-950 dark:text-white">
                {needsNewPassword ? "Set your password" : "Welcome back"}
              </h1>
              <p className="mt-2 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
                {needsNewPassword
                  ? "Create a new password to finish activating your account."
                  : "Sign in to continue your architecture reviews."}
              </p>
            </div>

            {!needsNewPassword ? (
              <form onSubmit={handleSignIn} className="space-y-5">
                <div className="space-y-1.5">
                  <label
                    className="text-xs font-medium text-slate-700 dark:text-slate-200"
                    htmlFor="email"
                  >
                    Work email
                  </label>
                  <div className="relative">
                    <Mail className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                    <input
                      id="email"
                      type="email"
                      autoComplete="email"
                      required
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                      placeholder="name@company.com"
                      className={inputClass}
                    />
                  </div>
                </div>

                <div className="space-y-1.5">
                  <label
                    className="text-xs font-medium text-slate-700 dark:text-slate-200"
                    htmlFor="password"
                  >
                    Password
                  </label>
                  <div className="relative">
                    <LockKeyhole className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                    <input
                      id="password"
                      type={showPassword ? "text" : "password"}
                      autoComplete="current-password"
                      required
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      placeholder="Enter your password"
                      className={inputClass}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((value) => !value)}
                      className="absolute right-2 top-1/2 flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-white/8 dark:hover:text-white"
                      aria-label={showPassword ? "Hide password" : "Show password"}
                      title={showPassword ? "Hide password" : "Show password"}
                    >
                      {showPassword ? (
                        <EyeOff className="h-4 w-4" />
                      ) : (
                        <Eye className="h-4 w-4" />
                      )}
                    </button>
                  </div>
                </div>

                {error && (
                  <div
                    role="alert"
                    className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-xs leading-relaxed text-rose-700 dark:border-rose-400/20 dark:bg-rose-400/10 dark:text-rose-200"
                  >
                    {error}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={submitting}
                  className="flex h-11 w-full items-center justify-center gap-2 rounded-lg bg-[var(--color-brand-primary)] px-4 text-sm font-semibold text-white transition-all hover:bg-[var(--color-brand-primary-hover)] focus:outline-none focus-visible:ring-3 focus-visible:ring-[var(--color-brand-primary)]/25 focus-visible:ring-offset-2 active:translate-y-px disabled:pointer-events-none disabled:opacity-55"
                >
                  {submitting ? (
                    <LoaderCircle className="h-4 w-4 animate-spin" />
                  ) : (
                    <ArrowRight className="h-4 w-4" />
                  )}
                  {submitting ? "Signing in" : "Sign in"}
                </button>

                {ssoProvider && (
                  <>
                    <div className="flex items-center gap-3 pt-1">
                      <span className="h-px flex-1 bg-slate-200 dark:bg-white/10" />
                      <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                        or
                      </span>
                      <span className="h-px flex-1 bg-slate-200 dark:bg-white/10" />
                    </div>
                    <button
                      type="button"
                      onClick={handleSSO}
                      disabled={submitting}
                      className="flex h-11 w-full items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-700 transition-all hover:border-slate-300 hover:bg-slate-50 focus:outline-none focus-visible:ring-3 focus-visible:ring-[var(--color-brand-primary)]/25 focus-visible:ring-offset-2 active:translate-y-px disabled:pointer-events-none disabled:opacity-55 dark:border-white/12 dark:bg-white/5 dark:text-white dark:hover:border-white/20 dark:hover:bg-white/10"
                    >
                      <LockKeyhole className="h-4 w-4" />
                      Sign in with SSO
                    </button>
                  </>
                )}
              </form>
            ) : (
              <form onSubmit={handleNewPassword} className="space-y-5">
                <div className="space-y-1.5">
                  <label
                    className="text-xs font-medium text-slate-700 dark:text-slate-200"
                    htmlFor="new-password"
                  >
                    New password
                  </label>
                  <div className="relative">
                    <LockKeyhole className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                    <input
                      id="new-password"
                      type={showPassword ? "text" : "password"}
                      autoComplete="new-password"
                      required
                      minLength={8}
                      value={newPassword}
                      onChange={(event) => setNewPassword(event.target.value)}
                      placeholder="Create a secure password"
                      className={inputClass}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((value) => !value)}
                      className="absolute right-2 top-1/2 flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-white/8 dark:hover:text-white"
                      aria-label={showPassword ? "Hide password" : "Show password"}
                      title={showPassword ? "Hide password" : "Show password"}
                    >
                      {showPassword ? (
                        <EyeOff className="h-4 w-4" />
                      ) : (
                        <Eye className="h-4 w-4" />
                      )}
                    </button>
                  </div>
                  <p className="text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
                    Use at least 8 characters with uppercase, lowercase, number,
                    and symbol.
                  </p>
                </div>

                {error && (
                  <div
                    role="alert"
                    className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-xs leading-relaxed text-rose-700 dark:border-rose-400/20 dark:bg-rose-400/10 dark:text-rose-200"
                  >
                    {error}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={submitting}
                  className="flex h-11 w-full items-center justify-center gap-2 rounded-lg bg-[var(--color-brand-primary)] px-4 text-sm font-semibold text-white transition-all hover:bg-[var(--color-brand-primary-hover)] focus:outline-none focus-visible:ring-3 focus-visible:ring-[var(--color-brand-primary)]/25 focus-visible:ring-offset-2 active:translate-y-px disabled:pointer-events-none disabled:opacity-55"
                >
                  {submitting ? (
                    <LoaderCircle className="h-4 w-4 animate-spin" />
                  ) : (
                    <ArrowRight className="h-4 w-4" />
                  )}
                  {submitting ? "Saving password" : "Set password and continue"}
                </button>
              </form>
            )}
          </div>
        </div>

        <p className="shrink-0 text-center text-[10px] text-slate-400 dark:text-slate-500">
          Protected access · Architecture Compliance Review
        </p>
      </section>
    </div>
  );
}
