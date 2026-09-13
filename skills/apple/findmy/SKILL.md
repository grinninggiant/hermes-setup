---
name: findmy
description: "Track Apple devices/AirTags via FindMy.app on macOS."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [FindMy, AirTag, location, tracking, macOS, Apple]
---

# Find My: inspect owned devices and items

Use for a user's own device or AirTag location shown by Apple's Find My app. Respect privacy: only track devices/items the user owns. An item lookup is not permission for ongoing location logging, sharing, Lost Mode, sound playback or erasure.

## Inspect the current view

1. Resolve the device/item and use the available macOS computer-use workflow. Verify screen recording/accessibility permissions rather than changing security settings. Load the relevant computer-use guidance before controlling the UI.
2. Prefer the existing Find My window. Inspect current controls and locale before clicking; example button names and element IDs are not live evidence. Do not assume `FindMy` is the exact process or bundle name on every OS version.
3. Capture the intended window using the available noninteractive screenshot method. `screencapture -w` invokes interactive window selection and is not an unattended capture method. Check the installed capture tool's schema rather than using a guessed element ID or a blind sleep loop.
4. Read the authorized screenshot with `vision_analyze`. Report the exact item identity, displayed location, freshness/last-seen time and uncertainty. Coordinates not shown cannot be invented from a map label.
5. If the screen says Offline or No location found, distinguish a last-known position from a current location. AirTag is not a continuous GPS tracker. Find My network reports depend on nearby participating devices; keeping the app foreground is not a guarantee of a new observation.

## Optional ongoing monitoring

Only if explicitly requested, define duration, cadence, recipient and private retention before scheduling. Use a supported, stoppable scheduler and verify its exact job; do not start an endless capture loop. Repeated screenshots of the same last-seen value are not new location observations. Keep location evidence out of shared repositories and knowledge stores.

## Boundaries and verification

Apple documents Find My on Apple devices and iCloud.com for supported devices; AirTags and Find My accessories require a supported Find My/Find Items app. Do not infer that every product has no possible API merely because this skill uses the UI. Never relaunch or kill the user's app, alter account/security settings, mark lost, share a location or erase a device under a read-only lookup request. Require separate applicable authorization for side effects.

Verify the actual rendered result, not only successful AppleScript execution. State unavailable access or stale location honestly; do not turn a screenshot into proof that the item is physically there now.

Reference: https://support.apple.com/en-us/104978
