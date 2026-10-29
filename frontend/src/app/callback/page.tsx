// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect } from "react";
import { LoaderCircle } from "lucide-react";

/**
 * OAuth redirect landing page for the hosted-UI / SAML flow.
 *
 * Cognito redirects here with an auth code after a successful SSO login.
 * Amplify (configured in AmplifyProvider) detects the `?code=` param on load
 * and completes the code↔token exchange automatically, firing the Hub
 * `signedIn` event that `useAuth` listens for. Once the session exists,
 * AuthGate (which wraps this route) will have rendered this page as an
 * authenticated child — so we just bounce to the app home.
 *
 * Static-export safe: pure client component, reads nothing from the server.
 */
export default function CallbackPage() {
  useEffect(() => {
    // Give Amplify a tick to finish the exchange, then go home. If we got
    // here as an authenticated child, the session is already live; the small
    // delay avoids racing the Hub event on the very first paint.
    const t = window.setTimeout(() => {
      window.location.replace("/");
    }, 300);
    return () => window.clearTimeout(t);
  }, []);

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="text-center">
        <LoaderCircle className="mx-auto h-6 w-6 animate-spin text-[var(--color-brand-primary)]" />
        <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
          Completing sign-in
        </p>
      </div>
    </div>
  );
}
