---
name: app-store-connect-cli
description: Use when managing iOS/macOS app distribution, uploading screenshots to App Store Connect, or automating app release workflows via CLI. Covers authentication setup, screenshot validation, TestFlight distribution, and app store submissions.
---

# App Store Connect CLI (asc)

## Overview

App Store Connect CLI is a command-line tool for automating Apple app distribution. It handles the full workflow from local screenshot capture through App Store submission, with special support for TestFlight beta distribution.

**Core principle:** The tool has two distinct workflows—experimental local (capture → frame → review → upload) and production app store (validate → upload). Choose based on whether you're creating new screenshots or pushing existing assets.

## When to Use

**Symptoms triggering this skill:**
- Need to upload screenshots to App Store Connect programmatically
- Managing releases for multiple apps (Velox, Lumen, etc.)
- Automating TestFlight builds and distribution
- Validating screenshot dimensions before upload
- Managing app metadata and localizations via CLI

**Common scenarios:**
- `asc screenshots upload` — push new screenshots for a specific iOS/iPad version
- `asc auth switch` — toggle between app credentials (Velox vs Lumen)
- `asc screenshots capture` — local automation for screenshot generation (experimental)
- `asc publish` — submit app to App Store or TestFlight

## Authentication Setup

Credentials are stored in macOS keychain (auto-loaded). Two apps are pre-registered:

| Profile | Key ID | App |
|---------|--------|-----|
| Velox | LCM9GBHQLQ | com.veloxmath.com (Apple ID: 6761457423) |
| Lumen | LCM9GBHQLQ | com.lumenspeeds.com (Apple ID: 6761525502) |

**Check active profile:**
```bash
asc auth status --verbose
```

**Switch to a different app:**
```bash
asc auth switch --name Velox
```

## Screenshot Workflow Patterns

### Pattern 1: Upload Existing Screenshots (App Store Workflow)

Use this when you have screenshot files ready to push:

```bash
# Get required device types
asc screenshots sizes

# Validate before upload (essential!)
asc screenshots validate --path "./screenshots/iphone" --device-type "IPHONE_65"

# Upload for a specific version
asc screenshots upload --app "6761457423" --version "1.2.3" \
  --path "./screenshots/iphone" --device-type "IPHONE_65"

# Upload iPad set same version
asc screenshots upload --app "6761457423" --version "1.2.3" \
  --path "./screenshots/ipad" --device-type "IPAD_PRO_3GEN_129"
```

**Key flags:**
- `--app` — Apple ID (6761457423 for Velox, 6761525502 for Lumen)
- `--version` — App Store version string (e.g., "1.2.3")
- `--path` — Directory containing screenshot PNGs
- `--device-type` — IPHONE_65 (default) or IPAD_PRO_3GEN_129
- `--confirm` — Skip confirmation prompt (useful in automation)

### Pattern 2: Local Screenshot Automation (Experimental)

For automated capture and framing from simulator/app:

```bash
# Plan the workflow
asc screenshots plan --app "6761457423" --version "1.2.3"

# Or run full capture→frame→review→upload sequence
asc screenshots run --plan .asc/screenshots.json
```

## Required Screenshot Dimensions

**iPhone 6.5" (required):**
- Portrait: 1242×2688, 1284×2778
- Landscape: 2688×1242, 2778×1284

**iPad Pro 12.9" (recommended for full coverage):**
- Portrait: 2048×2732, 2064×2752
- Landscape: 2732×2048, 2752×2064

Use `asc screenshots sizes --all` for complete matrix (less commonly needed).

## Build & TestFlight Workflows

**List available builds:**
```bash
asc builds list --app 6761457423
```

**Publish to TestFlight:**
```bash
asc publish --app 6761457423 --build "BUILD_ID" --testflight --confirm
```

**List TestFlight groups:**
```bash
asc testflight list-beta-groups --app 6761457423
```

## Common Mistakes

| Issue | Fix |
|-------|-----|
| "Version not found" | Version must exist in App Store Connect before uploading screenshots. Create it in the web dashboard first. |
| "Device type mismatch" | Use exact casing: `IPHONE_65` not `iPhone_65`. Run `asc screenshots sizes` to confirm. |
| "JSON output overwhelming" | Add `--output table` or `--output markdown` for readable output; JSON is default but hard to parse manually. |
| Wrong app credentials active | Run `asc auth status` to verify active profile; use `asc auth switch --name Velox` to change. |
| Screenshot validation fails | Dimensions must be EXACT (down to pixel). PNG format required. Use ImageMagick or Xcode Simulator for export. |
| "ID of type screenshot not found" | You're deleting with wrong ID. List screenshots first: `asc screenshots list --version-localization "LOC_ID"` |

## Critical Commands Reference

**Authentication:**
- `asc auth login --name "AppName" --key-id "..." --issuer-id "..." --private-key "..."`
- `asc auth status --verbose`
- `asc auth switch --name Velox`

**Queries:**
- `asc builds list --app APP_ID`
- `asc screenshots list --version-localization LOC_ID`
- `asc screenshots sizes` (show required dimensions)

**Uploads & Management:**
- `asc screenshots validate --path DIR --device-type DEVICE`
- `asc screenshots upload --app APP_ID --version "X.Y.Z" --path DIR --device-type DEVICE --confirm`
- `asc screenshots delete --id SCREENSHOT_ID --confirm`
- `asc screenshots download --version-localization LOC_ID --output-dir DIR`

**Metadata:**
- `asc metadata list-localizations --app APP_ID`
- `asc metadata upload --app APP_ID --path "./metadata"`

## Output Format Control

Default output is JSON (machine-readable). For humans:

```bash
# Pretty-printed JSON
asc screenshots list --version-localization "LOC_ID" --pretty

# Table format (readable scanning)
asc builds list --app 6761457423 --output table

# Markdown (for documentation/reports)
asc screenshots plan --app 6761457423 --version "1.2.3" --output markdown
```

## Experimental Features

Local screenshot automation (capture, frame, review-generate, review-approve) is marked experimental. Use with caution:
- Commands work but behavior may change
- Framing requires Xcode/simulator setup
- Report issues at https://github.com/rorkai/App-Store-Connect-CLI/issues

For production screenshot workflows, use the "upload existing screenshots" pattern (Pattern 1 above).
