// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// CloudFront Function (viewer-request) — rewrite SPA paths to index.html.
//
// Next.js static export with `trailingSlash: true` produces an index.html
// inside every route folder. S3 returns 403 for "directory" requests
// because there's no list permission, so we have to rewrite to the
// concrete object name before the request reaches S3.
//
// Examples:
//   /validation/detail/        -> /validation/detail/index.html
//   /validation/detail/?id=x   -> /validation/detail/index.html?id=x  (querystring preserved by CloudFront)
//   /favicon.ico               -> unchanged (has extension)
//   /                          -> handled by DefaultRootObject already

function handler(event) {
    var request = event.request;
    var uri = request.uri;

    // If the URI ends with `/`, append index.html
    if (uri.endsWith('/')) {
        request.uri = uri + 'index.html';
        return request;
    }

    // If the URI has no file extension and doesn't already point at a file,
    // it's a SPA route like `/validation/detail` — rewrite to its index.html
    var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
    if (lastSegment.indexOf('.') === -1) {
        request.uri = uri + '/index.html';
    }

    return request;
}
