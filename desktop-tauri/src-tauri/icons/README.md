# Desktop app icons

This directory contains the icon assets used by the Tauri desktop app in `desktop-tauri/src-tauri/`.

These files are committed in-repo so the Desktop Tauri Release workflow can build from a clean checkout on both macOS and Windows without requiring any locally generated assets.

## Files in this directory

### `icon.png`
Primary high-resolution PNG source for the desktop app icon.

- Recommended source/master asset
- Used as the canonical raster icon source
- Should remain square
- Recommended size: `1024x1024`
- Background outside the icon should be transparent

### `icon.ico`
Windows icon bundle.

Used by the Windows Tauri build and packaging flow. This file should contain multiple embedded sizes so the icon looks correct in Explorer, taskbar, shortcuts, and installer surfaces.

Recommended embedded sizes:

- `16x16`
- `24x24`
- `32x32`
- `48x48`
- `64x64`
- `128x128`
- `256x256`

### `icon.icns`
macOS icon bundle.

Used by the macOS Tauri build and packaging flow. This is the standard macOS app icon container format.

### `32x32.png`
Small PNG icon variant.

Useful for small previews and for icon generation workflows that expect a discrete small raster size.

### `128x128.png`
Standard PNG icon variant.

Useful for desktop previews and icon generation workflows.

### `128x128@2x.png`
Retina-style PNG icon variant.

This is typically a `256x256` raster asset stored using the `@2x` naming convention.

## Which workflow uses these?

These assets are used by the Tauri desktop app build under:

- `desktop-tauri/src-tauri/tauri.conf.json`

They are also required by the GitHub Actions desktop release workflow:

- `.github/workflows/desktop-release.yml`

The workflow now builds the frontend before running `tauri build`, but native packaging still requires the icon assets in this directory to exist in the repository.

## Why these files are committed

The desktop release workflow runs from a clean checkout in CI. That means required icon assets must already exist in the repo.

In particular:

- macOS packaging expects `icons/icon.png`
- Windows packaging expects `icons/icon.ico`

Without these files, `tauri build` can fail even if the frontend build succeeds.

## Update workflow

When changing the desktop icon:

1. Start from the high-resolution master asset and export/update `icon.png`
2. Regenerate `icon.ico` from the same master
3. Regenerate `icon.icns` from the same master
4. Regenerate the PNG size variants in this directory
5. Commit the full set together so CI stays reproducible

## Design guidance

Current icon conventions:

- square canvas
- transparent outer background
- opaque circular mark
- centered composition
- optimized for readability at small sizes
- works on both light and dark desktop surfaces

## Notes

- Keep filenames stable unless Tauri config is updated in the same change
- Avoid relying on local-only generated assets
- Prefer regenerating all formats from the same master artwork to keep branding consistent
