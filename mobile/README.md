# UNICORN — mobile app (Capacitor)

This wraps the exact same frontend already live on Railway in a native iOS/
Android shell, using [Capacitor](https://capacitorjs.com/). It does **not**
duplicate the app's code — `mobile/www/` is a copy of `frontend/` with two
deliberate differences, both explained inline in `mobile/www/index.html`:

1. `window.UNICORN_API_BASE` is set to the live Railway URL before `app.js`
   loads, since a bundled-in-the-app page has no same-origin API to call
   with a plain relative path like `/api/markets`.
2. The `#support-banner` (PayPal "help fund real-money licensing" link) is
   left out, to avoid unnecessary App Store review scrutiny on anything
   payment-adjacent for a feature that isn't core to the app.

**Whenever `frontend/app.js` or `frontend/styles.css` change, copy the new
versions into `mobile/www/` too** (`cp frontend/app.js frontend/styles.css
mobile/www/`) — they're plain copies, not symlinks, so they don't update
themselves. `mobile/www/index.html` and `mobile/www/mobile.css` are mobile-
only and don't need to change when the web frontend does, unless the header/
nav markup itself changes.

## Why this couldn't be finished in the sandbox that built it

This was built in a cloud sandbox with no access to the npm registry (a
network policy on that environment, not a Capacitor limitation) and no
Xcode/macOS. Everything that doesn't require either of those is done and
tested — the `www/` bundle, the config, the icon/splash source images, and
(critically) CORS support added to the Flask backend so the app can even
talk to the API cross-origin. What's left needs a real machine with normal
internet access, and for iOS specifically, either a Mac or a macOS cloud
build service.

## What you need before you start

- **Node.js** (any recent LTS) on the machine you run these commands from.
- **An Apple Developer account** ($99/year) to submit to the App Store —
  create this yourself at developer.apple.com; nothing here can do that for
  you. You don't need it yet to build/test locally, only to actually submit.
- **For iOS specifically**: a Mac with Xcode, OR a macOS cloud build
  service if you don't own a Mac — e.g. [Ionic
  Appflow](https://ionic.io/appflow) (built for exactly this, Capacitor-
  native), or a generic CI with macOS runners (GitHub Actions' `macos-
  latest`, Codemagic, Bitrise) running `xcodebuild` + a signing step. Pick
  whichever you're already comfortable with — Appflow is the least setup if
  you're starting from zero.
- **For Android**: nothing extra — Android Studio runs fine on Windows/
  Linux/Mac, no cloud service needed.

## First-time setup

```bash
cd mobile
npm install
```

**Before going any further, open `capacitor.config.json` and change
`appId`** from the placeholder `com.sanders.unicorn` to a reverse-domain ID
you actually own (e.g. `com.yourname.unicorn`). This becomes permanent the
moment you first publish — it can't be changed later without publishing as
a brand-new app and losing all reviews/ranking/history.

```bash
npx cap add ios       # scaffolds ios/ — needs to run somewhere with Xcode
npx cap add android   # scaffolds android/ — works anywhere
npx capacitor-assets generate   # generates every icon/splash size from
                                 # mobile/assets/icon.png + splash.png
npx cap sync
```

`mobile/assets/icon.png` (1024×1024) and `mobile/assets/splash.png`
(2732×2732) are already in the repo — a gold compass-rose mark on black,
matching the web app's brand, rendered from the same SVG the site's favicon
uses. Swap them for something else first if you want a different look;
`capacitor-assets generate` reads whatever's there.

## Building

**Android:**
```bash
npx cap open android
```
Opens Android Studio. Build → Generate Signed Bundle/APK, then upload the
`.aab` to Google Play Console. (A Play Console account is a one-time $25
fee, separate from Apple's.)

**iOS, with a Mac:**
```bash
npx cap open ios
```
Opens Xcode. Set your Team under Signing & Capabilities, then Product →
Archive, then use the Organizer window to distribute to App Store Connect.

**iOS, without a Mac (Ionic Appflow or similar CI):**
Push this repo (or just the `mobile/` + `frontend/` folders) to the CI
service of your choice, point its iOS build step at `mobile/ios` after a
`cap sync`, and follow that service's own docs for connecting your Apple
Developer account and certificates — this part is genuinely
service-specific enough that it's worth following their onboarding rather
than generic steps here.

## Before you submit: the one real content-policy risk

Apple's guidelines explicitly disallow apps that "facilitate binary options
trading," and UNICORN's YES/NO share mechanic is structurally close to
that — even though it's play money. This is navigable (Manifold Markets is
a real, live example of a play-money prediction market on the App Store
today) but not automatic. What makes the difference in review:

- Be explicit, in the App Store listing copy and ideally somewhere in the
  app itself, that the play-money balance has **no cash value and can
  never be redeemed, cashed out, withdrawn, or exchanged for anything
  real** — not "not yet," just never, full stop, for this build.
- The existing `#banner` ("DEMO — play money only...") already says this
  in-app, which helps. The `#support-banner` you're intentionally not
  shipping in this build is the one thing that could read as "paying for
  something" — leaving it out was the right call for this reason as much
  as the review-friction reason above.
- Expect at least one rejection-and-resubmit round to be normal for a
  first-time app in this category, not a sign something's wrong.

## Screenshots for the App Store listing

The desktop/mobile screenshots already generated earlier this session
(markets list, market detail, portfolio, leaderboard) work for the App
Store listing's screenshot requirements too, though Apple wants specific
device-frame dimensions per device size — check the current requirements
at App Store Connect when you get there, since these change occasionally.
