#!/usr/bin/env bash
# Rebuild src/keel_seo/static/keel_seo/gsc/search_console.utilities.css from input.css.
# Needs the Tailwind 4 standalone CLI: set $TAILWIND to its path, or put tailwindcss on PATH.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$here/../../src/keel_seo/static/keel_seo/gsc/search_console.utilities.css"

"${TAILWIND:-tailwindcss}" --input "$here/input.css" --output "$out" --minify
echo "wrote $out ($(wc -c < "$out") bytes)"
