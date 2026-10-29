// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

interface PdfViewerProps {
  url: string;
  title?: string;
}

export function PdfViewer({ url, title }: PdfViewerProps) {
  const [zoom, setZoom] = useState(100);

  return (
    <Card className="flex flex-col h-[calc(100vh-240px)] overflow-hidden">
      {/* Toolbar */}
      <div className="flex items-center justify-between border-b px-4 py-2 bg-card">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-foreground">
            {title || "Document Viewer"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setZoom(Math.max(50, zoom - 25))}
            disabled={zoom <= 50}
          >
            -
          </Button>
          <span className="text-xs text-muted-foreground w-12 text-center">
            {zoom}%
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setZoom(Math.min(200, zoom + 25))}
            disabled={zoom >= 200}
          >
            +
          </Button>
          <div className="w-px h-5 bg-border mx-1" />
          <Button
            variant="outline"
            size="sm"
            onClick={() => window.open(url, "_blank")}
          >
            Open in new tab
          </Button>
        </div>
      </div>

      {/* PDF Embed */}
      <div className="flex-1 overflow-auto bg-[#525659]">
        <iframe
          src={`${url}#toolbar=0&navpanes=0&zoom=${zoom}`}
          className="w-full h-full border-0"
          title={title || "PDF Document"}
          style={{ minHeight: "100%" }}
        />
      </div>
    </Card>
  );
}
