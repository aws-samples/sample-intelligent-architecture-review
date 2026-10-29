// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Authentication service for AWS Cognito via Amplify v6.
 *
 * Adapted from market-surveillance reference. Wraps Amplify auth calls
 * in a consistent interface and exposes a singleton.
 */

import {
  signIn,
  signInWithRedirect,
  signOut,
  resetPassword,
  confirmResetPassword,
  confirmSignIn,
  getCurrentUser,
  fetchAuthSession,
  type SignInInput,
} from "aws-amplify/auth";

/**
 * Name of the configured SAML/SSO identity provider (baked at build time),
 * or null when SSO isn't configured. The login UI uses this to decide whether
 * to render the "Sign in with SSO" button.
 */
export const ssoProvider: string | null =
  process.env.NEXT_PUBLIC_COGNITO_SAML_PROVIDER || null;

export interface AuthUser {
  username: string;
  userId: string;
  email?: string;
  /**
   * Human-friendly name for the header/menu. Preference order:
   * `name` claim (Entra ID DisplayName) → given_name + family_name →
   * email → Cognito username. Federated usernames are opaque
   * (`EntraID_<subject>`), so they're the last resort.
   */
  displayName: string;
}

export interface SignInResult {
  success: boolean;
  user?: AuthUser;
  nextStep?: string;
  error?: string;
}

export interface ResetPasswordResult {
  success: boolean;
  nextStep?: string;
  error?: string;
}

class AuthService {
  async signIn(username: string, password: string): Promise<SignInResult> {
    try {
      const input: SignInInput = { username, password };
      const { isSignedIn, nextStep } = await signIn(input);

      if (isSignedIn) {
        const user = await this.getCurrentUser();
        return {
          success: true,
          user: user || undefined,
          nextStep: nextStep.signInStep,
        };
      }

      return { success: false, nextStep: nextStep.signInStep };
    } catch (err) {
      const error = err as Error;
      console.error("Sign in error:", error);
      return { success: false, error: error.message || "Failed to sign in" };
    }
  }

  /**
   * Start the hosted-UI redirect flow for a SAML/SSO identity provider. This
   * navigates the browser away to the Cognito hosted UI, which redirects to
   * the IdP; on success the IdP posts back to Cognito and Cognito redirects to
   * /callback with an auth code. Does not resolve normally — the page unloads.
   */
  async signInWithSSO(provider: string): Promise<{ success: boolean; error?: string }> {
    try {
      await signInWithRedirect({ provider: { custom: provider } });
      return { success: true };
    } catch (err) {
      const error = err as Error;
      console.error("SSO sign in error:", error);
      return { success: false, error: error.message || "Failed to start SSO sign in" };
    }
  }

  /** Used after Cognito's NEW_PASSWORD_REQUIRED challenge. */
  async confirmNewPassword(newPassword: string): Promise<SignInResult> {
    try {
      const { isSignedIn, nextStep } = await confirmSignIn({
        challengeResponse: newPassword,
      });
      if (isSignedIn) {
        const user = await this.getCurrentUser();
        return { success: true, user: user || undefined };
      }
      return { success: false, nextStep: nextStep.signInStep };
    } catch (err) {
      const error = err as Error;
      console.error("Confirm new password error:", error);
      return { success: false, error: error.message || "Failed to confirm password" };
    }
  }

  async signOut(): Promise<{ success: boolean; error?: string }> {
    try {
      await signOut();
      return { success: true };
    } catch (err) {
      const error = err as Error;
      console.error("Sign out error:", error);
      return { success: false, error: error.message || "Failed to sign out" };
    }
  }

  async resetPassword(username: string): Promise<ResetPasswordResult> {
    try {
      const { nextStep } = await resetPassword({ username });
      return { success: true, nextStep: nextStep.resetPasswordStep };
    } catch (err) {
      const error = err as Error;
      console.error("Reset password error:", error);
      return { success: false, error: error.message || "Failed to reset password" };
    }
  }

  async confirmResetPassword(
    username: string,
    confirmationCode: string,
    newPassword: string,
  ): Promise<{ success: boolean; error?: string }> {
    try {
      await confirmResetPassword({ username, confirmationCode, newPassword });
      return { success: true };
    } catch (err) {
      const error = err as Error;
      console.error("Confirm reset password error:", error);
      return { success: false, error: error.message || "Failed to confirm password reset" };
    }
  }

  async isAuthenticated(): Promise<boolean> {
    try {
      const session = await fetchAuthSession();
      return !!session.tokens?.accessToken;
    } catch {
      return false;
    }
  }

  async getCurrentUser(): Promise<AuthUser | null> {
    try {
      const session = await fetchAuthSession();
      // The presence of a valid token IS the signed-in signal. Everything
      // below is identity decoration, so nothing after this may downgrade a
      // token-bearing session to "not signed in".
      if (!session.tokens?.accessToken) return null;

      // ID token claims are the only reliable identity source for federated
      // (SAML) sign-ins: signInDetails.loginId is unset and username is the
      // opaque `EntraID_<subject>` identifier. The claims carry whatever the
      // IdP attribute mapping provides.
      const claims = session.tokens.idToken?.payload ?? {};
      const str = (v: unknown): string =>
        typeof v === "string" ? v.trim() : "";

      // Amplify's getCurrentUser() reads the user-pool user record and THROWS
      // for a hosted-UI/SAML session even when fetchAuthSession() returned
      // valid tokens (the API calls made with those tokens still return 200).
      // Letting that throw reach the outer catch would return null here and
      // bounce a correctly signed-in federated user back to the login form
      // moments after the app renders. So treat it as best-effort: fall back
      // to the token claims, which are authoritative for federated sign-ins.
      let username = str(claims["cognito:username"]) || str(claims.sub);
      let userId = str(claims.sub);
      let loginId: string | undefined;
      try {
        const current = await getCurrentUser();
        username = current.username || username;
        userId = current.userId || userId;
        loginId = current.signInDetails?.loginId;
      } catch {
        // Federated session — keep the claim-derived identity above.
      }

      const email = str(claims.email) || loginId || undefined;
      const fullName =
        str(claims.name) ||
        [str(claims.given_name), str(claims.family_name)].filter(Boolean).join(" ");
      const displayName = fullName || email || username;

      return { username, userId, email, displayName };
    } catch {
      return null;
    }
  }

  /**
   * Returns the ID token for forwarding to the API.
   *
   * If the cached token is within 60 seconds of expiry, force a refresh
   * so we don't ship a soon-to-expire token. fetchAuthSession({forceRefresh})
   * uses the long-lived refresh token to mint a new ID token automatically.
   */
  async getIdToken(): Promise<string | null> {
    try {
      let session = await fetchAuthSession();
      const exp = session.tokens?.idToken?.payload?.exp as number | undefined;
      const nowSec = Math.floor(Date.now() / 1000);
      if (!exp || exp - nowSec < 60) {
        session = await fetchAuthSession({ forceRefresh: true });
      }
      return session.tokens?.idToken?.toString() || null;
    } catch {
      return null;
    }
  }
}

export const authService = new AuthService();
