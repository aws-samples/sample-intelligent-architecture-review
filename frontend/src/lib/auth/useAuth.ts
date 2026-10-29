// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * useAuth — React hook for Cognito auth state and actions.
 *
 * Listens to Amplify Hub for auth events so that token refreshes,
 * sign-outs, and external sign-ins update the UI automatically.
 */

"use client";

import { useState, useEffect, useCallback } from "react";
import { Hub } from "aws-amplify/utils";
import { authService, ssoProvider, type AuthUser } from "./authService";

interface UseAuthReturn {
  user: AuthUser | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  /** Set when the hosted-UI redirect flow fails (e.g. Entra ID rejects). */
  ssoError: string | null;
  signIn: (
    username: string,
    password: string,
  ) => Promise<{ success: boolean; error?: string; nextStep?: string }>;
  signInWithSSO: () => Promise<{ success: boolean; error?: string }>;
  signOut: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

export function useAuth(): UseAuthReturn {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [ssoError, setSsoError] = useState<string | null>(null);

  const loadUser = useCallback(async () => {
    try {
      setIsLoading(true);
      const current = await authService.getCurrentUser();
      setUser(current);
    } catch {
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    const kickoff = window.setTimeout(() => {
      void loadUser();
    }, 0);

    const unsub = Hub.listen("auth", ({ payload }) => {
      switch (payload.event) {
        case "signedIn":
          setSsoError(null);
          loadUser();
          break;
        // Fired after the hosted-UI code-for-token exchange completes.
        // "signedIn" follows it, but reloading here too avoids a stuck
        // spinner if that second event is ever missed.
        case "signInWithRedirect":
          loadUser();
          break;
        case "signInWithRedirect_failure":
          setSsoError(
            "SSO sign-in failed. Your session may have expired mid-flow — please try again.",
          );
          setUser(null);
          setIsLoading(false);
          break;
        case "signedOut":
          setUser(null);
          break;
        case "tokenRefresh_failure":
          setUser(null);
          break;
      }
    });

    return () => {
      window.clearTimeout(kickoff);
      unsub();
    };
  }, [loadUser]);

  const signIn = async (username: string, password: string) => {
    const result = await authService.signIn(username, password);
    if (result.success && result.user) {
      setUser(result.user);
      return { success: true };
    }
    return {
      success: false,
      error: result.error || "Sign in failed",
      nextStep: result.nextStep,
    };
  };

  const signInWithSSO = async () => {
    setSsoError(null);
    // No configured provider means SSO is not enabled for this deployment;
    // report it rather than starting a redirect that cannot succeed.
    if (!ssoProvider) {
      const error = "SSO is not configured for this environment";
      setSsoError(error);
      return { success: false, error };
    }
    const result = await authService.signInWithSSO(ssoProvider);
    if (!result.success) {
      setSsoError(result.error || "Failed to start SSO sign-in");
    }
    return result;
  };

  const signOut = async () => {
    await authService.signOut();
    setUser(null);
  };

  return {
    user,
    isLoading,
    isAuthenticated: user !== null,
    ssoError,
    signIn,
    signInWithSSO,
    signOut,
    refreshUser: loadUser,
  };
}
