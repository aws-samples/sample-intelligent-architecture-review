// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useEffect, useState, type ReactNode } from "react";
import { initializeAmplify } from "./amplifyConfig";

export function AmplifyProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    initializeAmplify();
    const kickoff = window.setTimeout(() => setReady(true), 0);
    return () => window.clearTimeout(kickoff);
  }, []);

  if (!ready) return null;
  return <>{children}</>;
}
