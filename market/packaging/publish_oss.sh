#!/bin/sh
# publish_oss.sh -- publish reCamera Pro App Center to the SenseCraft CDN.
#
# Mirrors the gen-1 scripts/release-app.py contract: upload each object, then
# DOWNLOAD IT BACK and compare sha256 -- `ossutil` reporting success is not the
# same as the bytes being retrievable from the CDN. The catalog.json is uploaded
# LAST, after every package is confirmed live, so the directory never points at
# a package that has not landed yet.
#
# This publishes to the PRODUCTION CDN (outward-facing, hard to unpublish). It is
# gated: it runs ONLY when invoked with `--yes`. A dry run (default) just prints
# the plan.
#
# Prereqs: ossutil configured (~/.ossutilconfig), packages built + signed +
# catalog regenerated with the CDN base-url (see build.py / sign.py /
# gen_catalog.py). .sig sidecars are NOT uploaded -- signatures are embedded in
# catalog.json and the device verifies them against its baked-in public key.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
DIST="$HERE/dist"
CATALOG="$HERE/../catalog/catalog.json"

OSS_BASE="oss://sensecraft-statics/solution-app/recamera_pro"
CDN_BASE="https://sensecraft-statics.seeed.cc/solution-app/recamera_pro"

DO_IT=0
[ "${1:-}" = "--yes" ] && DO_IT=1

sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

verify_back() {   # <local_file> <oss_url>
    _local="$1"; _oss="$2"
    _tmp="$(mktemp)"
    ossutil cp -f "$_oss" "$_tmp" >/dev/null
    _want="$(sha256 "$_local")"; _got="$(sha256 "$_tmp")"
    rm -f "$_tmp"
    if [ "$_want" = "$_got" ]; then
        echo "  verify OK  $_got"
    else
        echo "  verify FAIL want=$_want got=$_got" >&2; exit 1
    fi
}

echo "=== reCamera Pro App Center -> CDN publish ==="
echo "OSS base: $OSS_BASE"
echo "CDN base: $CDN_BASE"
[ "$DO_IT" = 1 ] || echo "(DRY RUN -- pass --yes to actually publish)"
echo

# 1) packages first
for f in "$DIST"/*.tar.gz; do
    b="$(basename "$f")"
    echo "package  $b  ($(sha256 "$f"))"
    echo "  -> $OSS_BASE/packages/$b"
    if [ "$DO_IT" = 1 ]; then
        ossutil cp -f "$f" "$OSS_BASE/packages/$b" >/dev/null
        verify_back "$f" "$OSS_BASE/packages/$b"
    fi
done

# 1b) shared models (one-gen models[]+target_path). Layout mirrors the catalog
#     URLs exactly: models/<app_id>/<...subdirs>/<file>. Some apps stage files
#     in per-group subdirs (e.g. voice-transcribe: <app_id>/ plus <app_id>/kws/)
#     so we recurse to ARBITRARY depth -- a plain */* glob would skip the kws/
#     files and advertise a catalog URL whose bytes never landed. Absent dir ->
#     nothing to upload (those apps bundle their model inside the package).
#     Uploaded before the catalog so a models[] URL is never advertised before
#     the bytes are live.
MODELS="$HERE/models"
if [ -d "$MODELS" ]; then
    find "$MODELS" -type f | sort | while IFS= read -r f; do
        rel="${f#"$MODELS"/}"           # <app_id>/<...subdirs>/<file>
        echo "model    $rel  ($(sha256 "$f"))"
        echo "  -> $OSS_BASE/models/$rel"
        if [ "$DO_IT" = 1 ]; then
            ossutil cp -f "$f" "$OSS_BASE/models/$rel" >/dev/null
            verify_back "$f" "$OSS_BASE/models/$rel"
        fi
    done
fi

# 2) catalog.json LAST (only after every package is live)
echo
echo "catalog  catalog.json  ($(sha256 "$CATALOG"))"
echo "  -> $OSS_BASE/catalog.json"
if [ "$DO_IT" = 1 ]; then
    ossutil cp -f "$CATALOG" "$OSS_BASE/catalog.json" >/dev/null
    verify_back "$CATALOG" "$OSS_BASE/catalog.json"
fi

echo
echo "done. Catalog will be live at: $CDN_BASE/catalog.json"
