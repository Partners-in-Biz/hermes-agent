---
name: google-play-console-cli
description: Use when managing Android app releases to Google Play, uploading screenshots and store listings, managing tracks/releases, or automating app distribution workflows via CLI. Covers authentication, release workflows, metadata sync, and staged rollouts.
---

# Google Play Console CLI (gplay)

## Overview

Google Play Console CLI automates Android app publishing to Google Play Store. It handles the full release workflow—from bundle uploads through staged rollouts—with support for metadata sync and screenshot management across multiple locales.

**Core principle:** The `gplay release` command bundles the common workflow (create edit → upload bundle → configure track → commit). Use it for straightforward releases; use granular commands (edits, bundles, tracks) for complex scenarios.

## When to Use

**Symptoms triggering this skill:**
- Need to publish Android app updates programmatically
- Managing releases for multiple Google Play apps (Velox, Lumen)
- Uploading screenshots across locales and device types
- Syncing store listings (descriptions, titles, etc.)
- Staged rollouts (% user testing before full release)
- Automating internal/beta/production tracks

**Common scenarios:**
- `gplay release` — complete release workflow in one command
- `gplay bundles upload` — upload app bundle to a specific edit
- `gplay images upload` — push screenshots with device framing
- `gplay tracks list` — check current release status

## Authentication Setup

Service account credentials stored in `~/.gplay/config.json`. Single service account manages both apps.

**Profile:** partners-in-biz
**Service Account Email:** partners-in-biz@partners-in-biz-85059.iam.gserviceaccount.com
**Access:** Velox (io.velox.math) + Lumen (com.lumenspeeds.lumen)

**Check active profile:**
```bash
gplay auth status
```

**Switch profile (if multiple configured):**
```bash
gplay auth switch --profile partners-in-biz
```

## Release Workflows

### Pattern 1: Complete Release (Simplest)

One command handles create edit → upload → configure → commit:

```bash
gplay release --package io.velox.math \
  --track internal \
  --bundle ./app-release.aab \
  --release-notes "Bug fixes and performance improvements"
```

**Key flags:**
- `--package` — App package name (io.velox.math for Velox, com.lumenspeeds.lumen for Lumen)
- `--track` — Target track (internal, alpha, beta, production)
- `--bundle` — Path to .aab file (or `--apk` for APK)
- `--release-notes` — Plain text, JSON array, or @file.json
- `--rollout` — Staged rollout fraction (default: 1.0 = full, 0.1 = 10%)
- `--wait` — Block until processing completes
- `--listings-dir` — Optional: metadata directory with translations
- `--screenshots-dir` — Optional: screenshots directory structure

### Pattern 2: Complete Release with Metadata & Screenshots

Include store listings and screenshots:

```bash
gplay release --package io.velox.math \
  --track beta \
  --bundle ./app-release.aab \
  --listings-dir ./metadata \
  --screenshots-dir ./screenshots \
  --release-notes @release-notes.json \
  --rollout 0.5 \
  --wait
```

**Directory structure required:**
```
metadata/
  en-US/
    title.txt
    short_description.txt
    full_description.txt
    changelog.txt
  es/
    (same files)

screenshots/
  en-US/
    phone/
      image1.png
      image2.png
    tablet/
      image1.png
  es/
    (same structure)
```

### Pattern 3: Staged Rollout (Gradual Release)

Test with subset of users before full rollout:

```bash
# Release to 5% of users
gplay release --package io.velox.math \
  --track production \
  --bundle ./app-release.aab \
  --rollout 0.05 \
  --release-notes "New features: [list]"

# After monitoring, promote to 25%
gplay rollout update --package io.velox.math \
  --track production \
  --rollout 0.25

# Full rollout
gplay rollout update --package io.velox.math \
  --track production \
  --rollout 1.0
```

### Pattern 4: Manual Edit Workflow (Complex Scenarios)

When you need fine-grained control:

```bash
# Create edit
gplay edits create --package io.velox.math

# Upload bundle to that edit
gplay bundles upload --package io.velox.math --edit EDIT_ID --bundle ./app-release.aab

# Configure track with release notes
gplay tracks update --package io.velox.math --edit EDIT_ID \
  --track internal \
  --release-notes "v1.5.0 release"

# Validate before commit
gplay validate --package io.velox.math --edit EDIT_ID

# Commit the edit
gplay edits commit --package io.velox.math --edit EDIT_ID
```

## Track & Release Management

**List current releases:**
```bash
gplay tracks list --package io.velox.math --edit EDIT_ID
```

**Promote release between tracks:**
```bash
gplay promote --package io.velox.math \
  --source-track internal \
  --target-track beta
```

**Check app health snapshot:**
```bash
gplay status --package io.velox.math --pretty
```

## Screenshot Management

**Directory structure (required):**
- Locale folder: `en-US`, `es`, `fr`, etc.
- Device type folder: `phone`, `tablet`, `wearable`, `tv`
- PNG files: `image1.png`, `image2.png`, etc.

```
screenshots/
  en-US/
    phone/
      home.png
      search.png
    tablet/
      home.png
  es/
    phone/
      home.png
```

**Upload screenshots:**
```bash
gplay images upload --package io.velox.math \
  --edit EDIT_ID \
  --screenshots-dir ./screenshots
```

## Metadata Sync (Bidirectional)

**Pull store listings locally:**
```bash
gplay metadata pull --package io.velox.math --output-dir ./metadata
```

**Push local metadata to store:**
```bash
gplay metadata push --package io.velox.math --input-dir ./metadata
```

**Validate before push:**
```bash
gplay metadata validate --input-dir ./metadata
```

## In-App Products & Subscriptions

**List in-app products:**
```bash
gplay iap list --package io.velox.math
```

**List subscriptions:**
```bash
gplay subscriptions list --package io.velox.math
```

**Manage subscription base plans:**
```bash
gplay baseplans list --package io.velox.math --subscription-id PRODUCT_ID
```

## Reporting & Monitoring

**Download financial reports:**
```bash
gplay reports download --type financial --output-dir ./reports
```

**Download statistics:**
```bash
gplay reports download --type stats --output-dir ./reports
```

**Monitor app vitals (crashes, ANR):**
```bash
gplay vitals list --package io.velox.math
```

## Common Mistakes

| Issue | Fix |
|-------|-----|
| "Edit not found" | Edit ID expires after inactivity. Create new edit with `gplay edits create` and try again. |
| Screenshots rejected | Check: locale folder names (en-US, not en), device types (phone, tablet), PNG format, file names (no spaces). |
| Metadata validation fails | Run `gplay metadata validate --input-dir ./metadata` to see specific errors (title too long, description format). |
| "Package not found" error | Service account not granted access to this app. Add it in Play Console → Settings → Users & Permissions. |
| Rollout stuck at percentage | Check for error conditions. Run `gplay status --package APP_NAME` to diagnose. |
| Release notes missing after commit | Use `--release-notes @file.json` for complex notes; plain text works for simple messages. |
| Screenshots not visible after upload | Takes 24-48 hours for review before appearing in store. Check status with `gplay tracks list`. |

## Output Format Control

Most commands default to JSON. Use `--pretty` for readable output or `--output` where supported:

```bash
# Pretty-printed JSON (most commands)
gplay status --package io.velox.math --pretty
gplay release ... --pretty

# Table/markdown format (commands supporting --output)
gplay bundles list --package io.velox.math --edit EDIT_ID --output table
gplay tracks list --package io.velox.math --edit EDIT_ID --output markdown
```

**Note:** Not all commands support `--output`. Use `--pretty` as fallback for readable JSON.

## Real-World Examples

**Release to internal testers:**
```bash
gplay release --package io.velox.math --track internal --bundle app-release.aab \
  --release-notes "Internal beta: testing new UI"
```

**Staged rollout: 10% → 50% → 100%:**
```bash
# Initial 10%
gplay release --package io.velox.math --track production --bundle app.aab --rollout 0.1

# After 24h monitoring, expand to 50%
gplay rollout update --package io.velox.math --track production --rollout 0.5

# Final full rollout
gplay rollout update --package io.velox.math --track production --rollout 1.0
```

**Release with translations & screenshots:**
```bash
gplay release --package io.velox.math --track beta \
  --bundle app.aab \
  --listings-dir ./locales \
  --screenshots-dir ./play-console-screenshots \
  --release-notes @changelog.json \
  --wait
```

## Dry-Run Mode (Preview without executing)

Preview all operations without committing:

```bash
gplay release --package io.velox.math --track internal --bundle app.aab \
  --dry-run
```

Output shows exactly what would be created/updated without making changes.

## Related Commands

- `gplay doctor` — Diagnose auth, network, API setup
- `gplay audit` — View command history
- `gplay quota` — Check API quota usage
- `gplay workflow` — Run multi-step automation workflows
