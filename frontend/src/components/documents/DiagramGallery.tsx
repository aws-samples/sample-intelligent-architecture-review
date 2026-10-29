// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use client";

import { useState } from "react";
import Image from "next/image";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export interface DiagramItem {
  image_url: string;
  page_number: number;
  caption: string;
}

interface DiagramGalleryProps {
  diagrams: DiagramItem[];
  loading?: boolean;
  documentStatus?: string;
  pageCount?: number | null;
}

export function DiagramGallery({ diagrams, loading, documentStatus, pageCount }: DiagramGalleryProps) {
  const [selectedDiagram, setSelectedDiagram] = useState<DiagramItem | null>(null);
  const [lightboxOpen, setLightboxOpen] = useState(false);

  const openLightbox = (diagram: DiagramItem) => {
    setSelectedDiagram(diagram);
    setLightboxOpen(true);
  };

  if (loading) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Extracted Diagrams</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex gap-3 overflow-hidden">
            {[1, 2, 3].map((i) => (
              <div key={i} className="shrink-0 w-48 rounded-lg border border-border overflow-hidden">
                <div className="aspect-[4/3] bg-muted animate-pulse" />
                <div className="p-2 space-y-1.5">
                  <div className="h-2.5 bg-muted animate-pulse rounded w-3/4" />
                  <div className="h-2 bg-muted animate-pulse rounded w-1/2" />
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    );
  }

  if (diagrams.length === 0) {
    const isParsed = documentStatus === "parsed" || documentStatus === "analyzed";
    const isProcessing = documentStatus === "parsing" || documentStatus === "analyzing";

    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Extracted Diagrams</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-center py-8">
            <svg
              className="w-10 h-10 mx-auto text-muted-foreground/30 mb-3"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.5}
                d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"
              />
            </svg>
            {isProcessing ? (
              <>
                <p className="text-xs text-muted-foreground">
                  Diagrams will be available after document processing completes.
                </p>
                <p className="text-[10px] text-muted-foreground mt-1">
                  Document is currently being parsed{pageCount ? ` (${pageCount} pages)` : ""}.
                </p>
              </>
            ) : isParsed ? (
              <>
                <p className="text-xs text-muted-foreground">
                  No diagrams detected in this document.
                </p>
                {pageCount != null && pageCount > 0 && (
                  <p className="text-[10px] text-muted-foreground mt-1">
                    {pageCount} pages were analyzed.
                  </p>
                )}
              </>
            ) : (
              <>
                <p className="text-xs text-muted-foreground">
                  Diagrams are extracted during document processing and will appear here once parsing completes.
                </p>
                <p className="text-[10px] text-muted-foreground mt-1">
                  Architecture diagrams, topology charts, and component diagrams will be automatically detected.
                </p>
              </>
            )}
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">
            Extracted Diagrams ({diagrams.length})
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex gap-3 overflow-x-auto pb-2 -mx-1 px-1">
            {diagrams.map((diagram, index) => (
              <button
                key={index}
                onClick={() => openLightbox(diagram)}
                className="group relative shrink-0 w-48 rounded-lg border border-border overflow-hidden hover:border-[var(--color-brand-primary)]/50 hover:shadow-md transition-all cursor-pointer"
              >
                {/* Thumbnail */}
                <div className="relative aspect-[4/3] bg-muted">
                  <Image
                    src={diagram.image_url}
                    alt={diagram.caption}
                    fill
                    sizes="192px"
                    unoptimized
                    className="object-cover"
                  />
                  {/* Page number badge */}
                  <Badge
                    variant="default"
                    className="absolute top-1.5 right-1.5 text-[9px] bg-black/70 text-white border-transparent"
                  >
                    Page {diagram.page_number}
                  </Badge>
                  {/* Hover overlay */}
                  <div className="absolute inset-0 bg-black/0 group-hover:bg-black/10 transition-colors flex items-center justify-center">
                    <svg
                      className="w-6 h-6 text-white opacity-0 group-hover:opacity-100 transition-opacity drop-shadow-md"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0zM10 7v3m0 0v3m0-3h3m-3 0H7"
                      />
                    </svg>
                  </div>
                </div>
                {/* Caption */}
                <div className="p-2">
                  <p className="text-[10px] text-muted-foreground text-left line-clamp-2">
                    {diagram.caption}
                  </p>
                </div>
              </button>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Lightbox Dialog */}
      <Dialog open={lightboxOpen} onOpenChange={setLightboxOpen}>
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {selectedDiagram && (
                <>
                  <span className="text-sm">{selectedDiagram.caption}</span>
                  <Badge variant="outline" className="text-[10px]">
                    Page {selectedDiagram.page_number}
                  </Badge>
                </>
              )}
            </DialogTitle>
          </DialogHeader>
          {selectedDiagram && (
            <div className="relative w-full max-h-[70vh] overflow-auto rounded-lg border border-border">
              <Image
                src={selectedDiagram.image_url}
                alt={selectedDiagram.caption}
                width={1600}
                height={1200}
                unoptimized
                className="h-auto w-full object-contain"
              />
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
