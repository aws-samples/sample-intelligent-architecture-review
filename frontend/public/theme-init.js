// Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Sets the theme class before paint so there's no flash of the wrong mode on
// first load. Defaults to light unless the user has previously chosen dark.
// Served as a static file rather than inlined, so the page needs no
// dangerouslySetInnerHTML.
(function () {
  try {
    if (localStorage.getItem("theme") === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
  } catch {
    document.documentElement.classList.remove("dark");
  }
})();
