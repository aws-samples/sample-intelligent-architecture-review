// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Shared frontend test setup.
 *
 * The API client reads NEXT_PUBLIC_API_URL at module load and asks Amplify for a
 * token on every request. We stub Amplify (the only real boundary) rather than
 * the app's own authService, so the real token-plumbing code stays under test
 * while nothing touches the network or needs a configured Cognito pool.
 */
import { vi, afterEach } from "vitest";

process.env.NEXT_PUBLIC_API_URL = "https://api.test";

export const TEST_ID_TOKEN = "test-id-token";

vi.mock("aws-amplify/auth", () => ({
  // authService.getIdToken() inspects tokens.idToken.payload.exp and forces a
  // refresh when the token is within 60s of expiry, so hand back a token that
  // is comfortably valid.
  fetchAuthSession: vi.fn().mockResolvedValue({
    tokens: {
      idToken: {
        payload: { exp: Math.floor(Date.now() / 1000) + 3600 },
        toString: () => TEST_ID_TOKEN,
      },
    },
  }),
  signOut: vi.fn().mockResolvedValue(undefined),
  signIn: vi.fn(),
  signInWithRedirect: vi.fn(),
  resetPassword: vi.fn(),
  confirmResetPassword: vi.fn(),
  confirmSignIn: vi.fn(),
  getCurrentUser: vi.fn(),
}));

afterEach(() => {
  vi.restoreAllMocks();
});
