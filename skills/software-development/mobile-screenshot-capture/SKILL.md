---
name: mobile-screenshot-capture
description: Use when capturing App Store or Play Store quality screenshots from iOS Simulator or Android Emulator. Pure CLI — no computer-use tools required. Handles simulator boot, app launch, deep-link navigation, status bar cleanup, and screenshot capture for iOS (xcrun simctl) and Android (adb).
---

# Mobile Screenshot Capture

Pure CLI screenshot pipeline for iOS Simulator and Android Emulator. Zero computer-use. Every step is a terminal command.

---

## iOS SIMULATOR (xcrun simctl)

### Step 1 — Find and boot the right device

```bash
# List all available simulators
xcrun simctl list devices available

# Boot by name (use the simulator created for ASO work)
xcrun simctl boot "iPhone 16 Pro ASO"

# Or boot by UUID
xcrun simctl boot C59BCA17-9456-40B0-BB95-ED90F3581C90

# Open Simulator.app so you can see what's happening (optional)
open -a Simulator
```

### Step 2 — Launch the app

```bash
# By bundle ID (find it in Xcode → General → Bundle Identifier)
xcrun simctl launch booted com.yourcompany.yourapp

# Force-quit and relaunch (fresh state)
xcrun simctl terminate booted com.yourcompany.yourapp
xcrun simctl launch booted com.yourcompany.yourapp
```

### Step 3 — Navigate to each screen (no computer-use)

```bash
# Via deep link (preferred — navigate directly to any screen)
xcrun simctl openurl booted "yourapp://home"
xcrun simctl openurl booted "yourapp://playlist/featured"
xcrun simctl openurl booted "yourapp://profile/stats"

# Via simctl IO — send touch events (use pixel coordinates)
# Get coordinates by checking your app's layout or using Accessibility Inspector
xcrun simctl io booted sendEvent touchDown 540 1200
xcrun simctl io booted sendEvent touchUp 540 1200

# Scroll: swipe from bottom to top
xcrun simctl io booted sendEvent touchDown 540 1400
xcrun simctl io booted sendEvent touchUp 540 800

# Wait for animation to settle before capturing
sleep 0.8
```

**Deep links are the preferred navigation method.** Ask the user what URL schemes the app supports, or check the `Info.plist` for `CFBundleURLTypes`.

### Step 4 — Clean status bar (ALWAYS do this before any capture)

```bash
xcrun simctl status_bar booted override \
  --time "9:41" \
  --batteryLevel 100 \
  --batteryState charged \
  --wifiBars 3 \
  --cellularBars 4

# Persist until simulator is reset — run once at session start
```

### Step 5 — Capture screenshots

```bash
# Capture current booted simulator screen
xcrun simctl io booted screenshot screenshots-aso/sims/01-home.png

# Capture at 3x resolution (default for iPhone 15/16 Pro)
# Output is already at full resolution — no flag needed

# Capture sequence: navigate, pause, capture, navigate, pause, capture
xcrun simctl openurl booted "yourapp://dashboard"
sleep 1.0
xcrun simctl io booted screenshot screenshots-aso/sims/01-dashboard.png

xcrun simctl openurl booted "yourapp://activity"
sleep 1.0
xcrun simctl io booted screenshot screenshots-aso/sims/02-activity.png
```

### Full iOS capture script

```bash
#!/bin/bash
APP="com.yourcompany.yourapp"
OUT="screenshots-aso/sims"
mkdir -p "$OUT"

# Boot + launch
xcrun simctl boot "iPhone 16 Pro ASO" 2>/dev/null || true
xcrun simctl terminate booted "$APP" 2>/dev/null || true
xcrun simctl launch booted "$APP"
sleep 2

# Clean status bar
xcrun simctl status_bar booted override \
  --time "9:41" --batteryLevel 100 --batteryState charged \
  --wifiBars 3 --cellularBars 4

# Capture each screen
capture() {
  local url="$1" name="$2" delay="${3:-1.2}"
  xcrun simctl openurl booted "$url"
  sleep "$delay"
  xcrun simctl io booted screenshot "$OUT/$name.png"
  echo "✓ $name.png"
}

capture "yourapp://home"        "01-home"
capture "yourapp://stats"       "02-stats"
capture "yourapp://features"    "03-features"
capture "yourapp://profile"     "04-profile"
capture "yourapp://premium"     "05-premium" 2.0

echo "Done — $(ls $OUT/*.png | wc -l | tr -d ' ') screenshots captured"
```

---

## ANDROID EMULATOR (adb)

### Step 1 — Verify emulator is running

```bash
# List connected devices/emulators
adb devices

# If no emulator listed, start one (uses AVD name from Android Studio)
emulator -avd Pixel_8_API34 &
sleep 10
adb wait-for-device
```

### Step 2 — Launch the app

```bash
# By package + activity name
adb shell am start -n com.yourcompany.yourapp/.MainActivity

# Force-stop and relaunch
adb shell am force-stop com.yourcompany.yourapp
adb shell am start -n com.yourcompany.yourapp/.MainActivity
sleep 2
```

### Step 3 — Navigate via intent / deep link (no computer-use)

```bash
# Deep link navigation (preferred)
adb shell am start -W -a android.intent.action.VIEW \
  -d "yourapp://home" \
  com.yourcompany.yourapp

# Tap by coordinates (fallback)
adb shell input tap 540 1200

# Swipe (scroll down)
adb shell input swipe 540 1400 540 700 300

# Press back
adb shell input keyevent 4

# Press home
adb shell input keyevent 3
```

### Step 4 — Demo mode (clean status bar)

```bash
# Enable demo mode
adb shell settings put global sysui_demo_allowed 1

# Set clean status: 9:41, full battery, full signal
adb shell am broadcast -a com.android.systemui.demo -e command enter
adb shell am broadcast -a com.android.systemui.demo -e command clock -e hhmm 0941
adb shell am broadcast -a com.android.systemui.demo -e command battery -e level 100 -e plugged false
adb shell am broadcast -a com.android.systemui.demo -e command network \
  -e wifi show -e level 4 -e mobile show -e level 4 -e datatype none

# Exit demo mode when done
adb shell am broadcast -a com.android.systemui.demo -e command exit
```

### Step 5 — Capture screenshots

```bash
# Capture to local file
adb exec-out screencap -p > screenshots-aso/sims/01-home.png

# Via pull (save on device first, then pull)
adb shell screencap /sdcard/screen.png
adb pull /sdcard/screen.png screenshots-aso/sims/01-home.png
adb shell rm /sdcard/screen.png
```

### Full Android capture script

```bash
#!/bin/bash
PKG="com.yourcompany.yourapp"
ACT=".MainActivity"
OUT="screenshots-aso/sims"
mkdir -p "$OUT"

# Wait for device
adb wait-for-device
adb shell am force-stop "$PKG"
adb shell am start -n "$PKG/$ACT"
sleep 3

# Demo mode — clean status bar
adb shell settings put global sysui_demo_allowed 1
adb shell am broadcast -a com.android.systemui.demo -e command enter
adb shell am broadcast -a com.android.systemui.demo -e command clock -e hhmm 0941
adb shell am broadcast -a com.android.systemui.demo -e command battery -e level 100 -e plugged false
adb shell am broadcast -a com.android.systemui.demo -e command network \
  -e wifi show -e level 4 -e mobile show -e level 4 -e datatype none

# Capture function
capture() {
  local url="$1" name="$2" delay="${3:-1.5}"
  adb shell am start -W -a android.intent.action.VIEW -d "$url" "$PKG"
  sleep "$delay"
  adb exec-out screencap -p > "$OUT/$name.png"
  echo "✓ $name.png"
}

capture "yourapp://home"        "01-home"
capture "yourapp://stats"       "02-stats"
capture "yourapp://features"    "03-features"
capture "yourapp://profile"     "04-profile"

# Clean up demo mode
adb shell am broadcast -a com.android.systemui.demo -e command exit

echo "Done — $(ls $OUT/*.png | wc -l | tr -d ' ') screenshots captured"
```

---

## TIPS FOR GREAT SCREENSHOTS

| Issue | Fix |
|-------|-----|
| Empty / zero data | Seed the app: log in with a demo account or a preloaded account that has activity |
| App not responding to deep links | Check `Info.plist` (iOS) or `AndroidManifest.xml` (Android) for registered URL schemes |
| Status bar not clean | Run the status bar override AFTER the app launches (not before boot) |
| Keyboard visible | Send a tap outside the text field, then `sleep 0.5` before capturing |
| Wrong screen proportions | iOS: confirm simulator is iPhone 16 Pro (not older model). Android: use Pixel 8 1080×2400 |
| Animation still playing | Increase sleep delay before capture to 2.0s |
| Partial render / loading spinner | Use a pre-warmed account — log in, browse around, then quit and relaunch before capturing |

---

## INTEGRATION WITH ASO SCREENSHOTS SKILL

After capturing, the screenshots land in `screenshots-aso/sims/`. The `aso-appstore-screenshots` skill picks them up from there via `--screenshot sims/XX-name.png` and `--collage-shots`.

Name files by screen content, not by position:
```
sims/
  home.png
  dashboard.png
  activity.png
  features.png
  premium.png
```
The ASO skill assigns them to screenshot positions during Phase 2 (pairing).
