// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import type { Metadata } from "next";
import Script from "next/script";
import { Open_Sans, Source_Code_Pro } from "next/font/google";
import "./globals.css";
import { AppShell } from "@/components/layout/AppShell";
import { AmplifyProvider } from "@/lib/auth/AmplifyProvider";
import { AuthGate } from "@/components/auth/AuthGate";

const openSans = Open_Sans({
  variable: "--font-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const sourceCode = Source_Code_Pro({
  variable: "--font-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "Architecture Compliance Review",
  description:
    "AI-assisted resilience and High Availability validation for system design documents",
  // app/favicon.ico is picked up by Next automatically. These add the larger
  // sizes browsers use for bookmarks, pinned tabs, and home-screen shortcuts.
  icons: {
    icon: [
      { url: "/icon.svg", type: "image/svg+xml" },
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/apple-icon.png", sizes: "180x180", type: "image/png" }],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${openSans.variable} ${sourceCode.variable} h-full antialiased`}
    >
      <head>
        {/* Sets the theme class before paint (see public/theme-init.js). */}
        <Script src="/theme-init.js" strategy="beforeInteractive" />
      </head>
      <body className="min-h-full bg-background">
        <AmplifyProvider>
          <AuthGate>
            <AppShell>{children}</AppShell>
          </AuthGate>
        </AmplifyProvider>
      </body>
    </html>
  );
}
