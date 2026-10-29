// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Amplify configuration for Cognito.
 *
 * Static export friendly: pulls config from NEXT_PUBLIC_* env vars baked
 * at build time. The user pool ID and web client ID are non-sensitive
 * identifiers — the actual server client secret stays in SSM and is
 * only read by the backend.
 */

import { Amplify } from "aws-amplify";

let initialized = false;

/**
 * Hosted-UI OAuth (redirect) config for SAML/SSO. Only assembled when the
 * hosted-UI domain is baked in at build time (NEXT_PUBLIC_COGNITO_HOSTED_UI_DOMAIN).
 * Absent domain → no oauth block → the SSO button stays hidden and only
 * email+password auth is active. The redirect targets are the app's own
 * origin: /callback after sign-in, / after sign-out.
 */
function buildOAuthConfig() {
  const domain = process.env.NEXT_PUBLIC_COGNITO_HOSTED_UI_DOMAIN;
  if (!domain) return undefined;

  // At build time (static export) we don't know the runtime origin, so resolve
  // it in the browser. Fall back to the configured CloudFront origin if set.
  const origin =
    typeof window !== "undefined"
      ? window.location.origin
      : process.env.NEXT_PUBLIC_APP_ORIGIN || "";

  return {
    domain,
    scopes: ["email", "openid", "profile"],
    redirectSignIn: [`${origin}/callback`],
    redirectSignOut: [`${origin}/`],
    responseType: "code" as const,
  };
}

export function initializeAmplify(): void {
  if (initialized) return;

  const userPoolId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID;
  const userPoolClientId = process.env.NEXT_PUBLIC_COGNITO_WEB_APP_CLIENT_ID;
  if (!userPoolId || !userPoolClientId) {
    console.warn(
      "Amplify auth not configured: NEXT_PUBLIC_COGNITO_USER_POOL_ID and " +
        "NEXT_PUBLIC_COGNITO_WEB_APP_CLIENT_ID env vars must be set at build time.",
    );
    return;
  }

  const oauth = buildOAuthConfig();

  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId,
        userPoolClientId,
        signUpVerificationMethod: "code",
        loginWith: {
          email: true,
          ...(oauth ? { oauth } : {}),
        },
      },
    },
  });

  initialized = true;
}
