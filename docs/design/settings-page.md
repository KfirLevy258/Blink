# Settings page: design spec

The approved design for the BLINK settings page (2026-11-06). The reference
mock is `settings-page.html` beside this file; this spec is enough to build
from without it open. Example data only.

**The shipped page loads nothing from the network.** It is served by the
daemon and must work offline: no Google Fonts, no CDN. Bundle IBM Plex
Sans, IBM Plex Mono and Montserrat with the page, or fall back to the system
stacks given below.

## 1. Layout

**The board is the hero and never moves.** A drawing of the real case sits
dead centre of the viewport in every state. Opening, closing or switching a
panel never changes its position or size.

- **Top bar:** the BLINK mark and wordmark, then a muted "Settings", left
  aligned with the page edge. It is the only `h1`.
- **Hub (centre column):** the board, then the board's name, then one status
  line, then the three controls **Look / Connect / Board**.
- **Grid:** three columns, `minmax(320px, 1fr) minmax(460px, 600px)
  minmax(320px, 1fr)`, with a 28px gap. At 1680px and wider the centre
  column is 660px and the gap 40px. Maximum page width is 1720px. The grid
  is vertically centred in the viewport.
- **One control open at a time.** Pressing the open control again closes it,
  and so does Escape, which returns focus to the control.

**Panel docking.** A panel is positioned absolutely inside its side column.
Its top edge sits on the case's top edge (34px below the hub top), so a
panel's height never changes the row and never moves the board.

| Control | Left of the board | Right of the board |
|---|---|---|
| Look | Look: Themes / Logo | Customise: Colours / Screen / Try it |
| Connect | (empty) | Connect |
| Board | (empty) | Board: This board / This computer |

- **Look takes both sides,** so the board is the live preview between them.
- **Width:** Look and Board panels are at most 452px wide, Connect at most
  372px. A panel fills its column up to that width; the left panel hugs the
  board side (`right: 0`), the right one does too (`left: 0`).
- **Height:** at most `max(360px, 100dvh - 210px)`. Over that, the panel
  scrolls inside itself (`overscroll-behavior: contain`).
- **Sub-tabs:** when a panel would grow past that height, split it into
  sub-tabs. Do not shrink the controls.

**What the board shows** follows what is being edited:

- **Logo tab open:** the start-up screen.
- **Board panel with "Show it on the board" pressed:** the board's own
  Settings screen.
- **Just after a WiFi join succeeds:** the connection screen for 4.2 s.
- **Otherwise:** the usage screen in the chosen view.

**The studio.** The page reads as a product on a desk under one lamp.

- **Light pool:** a soft radial gradient behind the case (`--stage` fading
  into `--bg`).
- **Contact shadow:** an elliptical shadow under the feet (`--floor`).
- **Desk objects:** the computer on the left and the router on the right
  stand on the same desk line, joined to the case's side ears by thin link
  lines.
- **Router before WiFi exists:** drawn ghosted (dashed, 55% opacity) and
  labelled "WiFi / Not set up". Its label text keeps full contrast.

**The case** is drawn front-on as matte PLA (#ff6a13).

- **Lighting:** a vertical gradient from #ff8a45 to #ee600f, a faint side
  sheen, and FDM layer lines every 3px at 9% opacity.
- **Screen surround:** a recessed screen opening with a shaded inner wall,
  then black glass, then the 320×240 active area.
- **Ears and feet:** the side ears and the four feet sit behind the shell,
  in darker oranges.

**The board's screen** draws only what the firmware can draw: arcs, rects,
lines, text in Montserrat, and the logo bitmap. No blur, no gradients, no
shadows inside the screen.

## 2. Tokens

### Colour

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | #e6e5e2 | #121110 | page |
| `--stage` | #f7f6f4 | #262421 | the light pool's centre |
| `--panel` | #fbfbfa | #1b1a18 | panels |
| `--raise` | #ffffff | #2a2825 | selected segment or tab |
| `--ink` | #1c1b1a | #eceae6 | text |
| `--muted` | #5b5853 | #a29e97 | secondary text (≥ 5.6:1 on every surface) |
| `--line` | #dcd9d4 | #2f2d2a | rules, outlines |
| `--field` | #efeeeb | #23211f | inputs, rows, trays |
| `--accent` | #ff6a13 | #ff6a13 | the case colour: primary fill, selection rings, focus |
| `--accent-ink` | #a8400a | #ff9152 | orange as text or icon (≥ 6:1) |
| `--on-accent` | #1f0b00 | #1f0b00 | text on the accent (6.6:1) |
| `--ok` | #1b7642 | #5fd394 | live, done, up to date |
| `--danger` | #b1261d | #ff8a7d | errors, destructive |
| `--floor` | rgba(60,40,20,.30) | rgba(0,0,0,.55) | contact shadow |

- **Panel shadow, light:** `0 1px 2px rgba(50,35,20,.06), 0 18px 40px -16px
  rgba(50,35,20,.22)`, plus a 1px outline in `--line` at 60%.
- **Panel shadow, dark:** `inset 0 1px 0 rgba(255,255,255,.04), 0 22px 48px
  -16px rgba(0,0,0,.7)`, plus the same outline.
- **Colour scheme:** follow the system via `prefers-color-scheme`.
  `data-theme="light|dark"` on `:root` overrides it. Set `color-scheme`
  accordingly.

### Type

- **UI face:** IBM Plex Sans, the face the wordmark is drawn from. Fallback:
  `system-ui, -apple-system, "Segoe UI", sans-serif`.
- **Values:** IBM Plex Mono for hex codes, percentages and addresses, with
  `tabular-nums`. Fallback: `ui-monospace, Menlo, Consolas, monospace`.
- **Board screen:** Montserrat, which is LVGL's font.

| Role | Size / weight |
|---|---|
| Wordmark | 15 / 600, tracking .14em |
| Board name (under the board) | 20 / 500 (17 below 1200px) |
| Panel title (`h2`) | 17 / 600 |
| Section heading | 14 / 600 |
| Body, controls | 14 / 400–500, line-height 1.5 |
| Labels, hints, notes | 13 / 400–500 |
| Meta, foot, "New" tag | 12 (tag 11 / 600) |
| Slider readout | Mono 20 / 500 |

- **Case:** sentence case everywhere.
- **Labels:** no all-caps labels on the page. The board's own "SESSION 5h"
  is firmware text and stays as the board draws it.

### Space, radii, sizes

- **Spacing:** steps of 4: 4, 6, 8, 12, 14, 16, 20, 24. Panel padding is 24
  (20 at 420px and below); the gap between sections is 20, with a 1px
  `--line` rule between sections.
- **Radii:** panel 18, row and tray 12, button and input 10, segment 8–9,
  theme miniature 8, pill 999.
- **Targets:** every interactive target is at least 44×44.

### Motion

| Element | Duration | Easing |
|---|---|---|
| Hover, press, colour changes | 200ms | ease |
| Panel enter | 320ms, opacity plus 14px slide out from the board (10px up below 1200px) | `cubic-bezier(.2,.7,.2,1)` (`--out`) |
| Chevrons, ticks, switch knob | 250ms | `--out` |
| Desk link lines and router changes | 400ms | ease |
| Connect pulse | 1.3s loop, one 10% dash travelling the link line, only while connecting | `--out` |
| Sweep (arcs 0 to 100% and back) | 2.2s | smoothstep |

- **No ambient motion.** Nothing moves while the page is idle. The board's
  connection screen has one dashed-line drift, which the firmware also does.
- **Reduced motion:** with `prefers-reduced-motion: reduce`, set every
  animation and transition to none. The connect pulse becomes a solid line.
  The sweep jumps to 95% for 1.6s and back.

## 3. Components

**Status line.**

- **Content:** a 7px dot with a 3px tinted halo, then one sentence:
  - "Live over USB from Studio PC"
  - "Live over WiFi, Home-2G, encrypted"
  - "Board not found, check the USB cable", with a danger dot.
- **While connecting:** the handshake progress replaces it.

**The three controls.**

- **Tray:** a segmented group of three buttons with 16px icons, each at
  least 104×44.
- **Selected:** `--raise` with an outline, and the icon in `--accent-ink`.
- **ARIA:** `aria-pressed`, and `aria-controls` naming the panel or panels.
- **Below 1200px:** full width.

**Sub-tabs.**

- **Look:** a tray like the controls, `role="tablist"`, with tabs
  `role="tab"`, `aria-selected` and a roving `tabindex`.
- **Keyboard:** Left and Right move between tabs.
- **Panels:** each is `role="tabpanel"`.

**Theme miniatures.**

- **Grid:** 3 columns with a 16/12 gap. Each tile is a 4:3 drawing of that
  theme's real screen: header, two gauges at 38% and 74% in the theme's
  low and mid colours, the ball if the theme has one, and the pill. The
  theme name sits underneath.
- **Selected:** a 2px `--panel` gap, then a 2px `--accent` ring, a 20px
  orange tick top-right, and the name at 600.
- **Hover:** lifts 2px.
- **Preview:** pointing at a tile (mouse) or reaching it by keyboard focus
  shows that theme on the big board; leaving restores the current look.
  Clicking commits.
- **Hint:** "Point at a theme to preview it on the board."; on
  `hover: none`, "Tap a theme to put it on the board."

**Colour rows (Colours tab).**

- **Rows:** four, in a `--field` tray each, as
  `48px swatch | name + hint | 96px hex field`:
  - Background, "Behind everything"
  - Low, "Under 60%"
  - Middle, "60% to 90%"
  - High, "90% and above"
- **Swatch:** a native colour input, clipped to a 48×44 rounded square.
- **Hex field:** Mono, uppercase. It accepts `#RRGGBB` or `RRGGBB`. Invalid
  input gets a danger outline and `aria-invalid`; on blur it reverts to the
  current value.
- **Editing:** any change makes the theme "Custom", and the header says
  "**Custom**, changed from Dark". A "Reset to Dark" link appears in the
  Colours section header.

**Arc thickness.**

- **Control:** three segments, Thin, Medium and Bold (theme widths 5, 12
  and 22).
- **Segment:** each draws a small arc at its stroke width above its label;
  the selected one's arc is in the accent.

**Main screen views (Screen tab).**

- **Options:** four cards in a row, each with a mini drawing of the layout:
  Two gauges, Session big, Weekly big, Burn view. Selected: an accent
  outline and a tint.
- **Note under the cards:**
  - Two gauges: "Both windows, side by side."
  - Session big: "Session big, weekly as a line."
  - Weekly big: "Weekly big, session as a line."
  - Burn view: "The board shows this by itself when an account has no
    five-hour window. Picking it here only previews it."
- **Main provider:** Claude / Codex, with the note "Its gauges come first
  when the board shows both."

**Sliders (Try it tab).**

- **Session and weekly:** "Try session at" and "Try weekly at", each with a
  Mono readout on the right.
- **Track:** 8px, painted in the theme's own low, mid and high colours, in
  zones 0–60, 60–90 and 90–100.
- **Thumb:** 26px, white.
- **Marks:** 0, 60% and 90% under the track, aligned to the thumb centre.
- **Sweep:** a round 44px play button, "Sweep both arcs from 0 to 100%".
- **Scope:** only the preview changes.

**Logo (Look, Logo tab).**

- **Drop zone:** a dashed 150px zone, "Drop a logo here / or choose a file.
  PNG, SVG or JPEG.". The file is read locally.
- **Once loaded:**
  - **File row:** thumbnail, name, "About N KB of 512 KB on the board", and
    Replace.
  - **Size on the screen:** Fill / Large / With margin (scale 1, .8, .6).
  - **Background:** colour, "From the picture's corner" by default, with
    Auto to return to it.
  - **Stays up for:** 3, 5 or 10 seconds (the board shows a logo for at
    most 15 seconds in all, play and hold together).
  - **Actions:** "Send to board" (primary) and "Remove" (danger). Remove
    confirms in-page once the logo is on the board.
- **Facts line:** "Fitted inside 320 by 240 pixels, 65,536 colours, up to
  512 KB." and "Shown after the BLINK animation, for up to 15 seconds."
- **Too big:** sending is disabled, with "Too big for the board. Pick a
  plainer background or a smaller size."

**Network list (Connect).**

- **Rows:** radio rows at least 54px tall: name, "WPA2/WPA3, strong signal",
  and signal bars.
- **Unsupported networks:** disabled with a dashed radio, in danger text
  ("802.1X, not supported").
- **Note:** "2.4 GHz networks only".

**Password field.**

- **Field:** Mono input with a Show/Hide toggle (`aria-pressed`,
  `aria-label`).
- **Note:** with a lock icon, "Sent to the board over USB only, never saved
  on this computer.".
- **Errors:** an inline danger outline and message. Enter submits.

**Handshake progress.**

- **Placement:** replaces the status line while connecting.
- **Ticks:** four 18×3 ticks. Grey is pending, accent is active, ok is done,
  danger is failed.
- **Step text:** "Pairing over the cable", "Board joins the network",
  "Both prove their keys over WiFi", "Encrypting the link". Each tick is a
  real event the app reports: the password acknowledged over the cable, the
  board on the network, the app's one-shot verification handshake accepted
  by the board over WiFi, and its encrypted reply opened. "Connected. You
  can unplug the cable." appears only after all four.
- **Link lines:** the pulse runs on the line in use for each step. On
  success a lock in `--ok` appears on the WiFi line.

**In-page confirm.**

- **No `confirm()`.** The destructive button is replaced by a tinted box:
  question, solid danger action, and a quiet "Keep".
- **Focus:** goes to "Keep".
- **Used for:** "Forget WiFi" and "Remove logo".

**Switch rows (This computer).**

- **Row:** name plus a one-line explanation on the left; on the right a
  `role="switch"` button (44×26 track) with `aria-checked`.

**"New" tag.** A pill, 11/600, accent at 14% with `--accent-ink` text. It
marks a capability the board or app does not have yet.

## 4. Panel contents and copy

**Look, Themes tab.**

- **Themes:** the 12 built-ins from `pc/theme_builtins.py`, in that order.

**Look, Logo tab.** As in Components.

**Customise header.**

- **Default:** "Starting from **Dark**".
- **After an edit:** "**Custom**, changed from Dark".

**Connect.**

- **Status card:** "Over USB / Studio PC / Live", or "Over WiFi / Home-2G /
  Encrypted".
- **Action:** "Move to WiFi", or "Change network" once on WiFi.
- **Picker:** "Networks the board can hear".
- **Password label:** "Password for Home-2G".
- **Buttons:** "Connect" (while running, "Connecting…"), and "Cancel".
- **Success:** "Connected. You can unplug the cable."
- **Failure:** "The board could not join Home-2G. Check the password and try
  again."
- **Validation:**
  - "Enter the password for Home-2G."
  - "A WiFi password has at least 8 characters."
- **Unreachable:** "Can't reach the board" / "Check the USB cable, or that
  the board is switched on. BLINK keeps trying." / "Try again".

**Board, This board tab.**

- **Fields:** Name; Brightness 20/40/60/80/100% (the levels the board
  stores; the board dims live); "Firmware 1.4.1" with "Up to date".
- **On the board** (New): "The board's Settings screen swaps its Theme
  button for Open settings, which opens this page on your computer." with
  the button "Show it on the board" / "Show the usage screen".
- **Details:** Address ("USB, COM5" or the WiFi address), Saved WiFi, and
  "Forget WiFi", which confirms with "Forget Home-2G? The board deletes the
  password and its key, and goes back to USB only." / "Forget it" / "Keep".

**Board, This computer tab.**

- **Usage comes from:** This Windows PC / WSL: Ubuntu.
- **Service incidents:** "Show outages from status.claude.com and
  status.openai.com on the board".
- **Update the app by itself:** "New versions install when the board is
  idle".
- **Keep figures fresh** (New): "Runs Claude Code's own /usage now and
  then. Sends no messages."
- **Doze when nobody's around** (New): "Dims the board after 4 hours with
  nothing running".

**Board screens drawn in the preview.**

- **Usage, two gauges:** as the firmware draws it.
- **Usage, Focus:** a 156px ring, the figure in the hollow, the caption and
  countdown in the open quarter, and the other window as a dim line at
  y 184.
- **Burn view:** follows feat/ultra-burn `usage_layout.h` BURN_*.
  - **Bars:** 20 bars, 5px on a 6px pitch from x 20, on a baseline at
    y 148; the last four "hot".
  - **Axis and text:** hour ticks and numbers, the total and arrow above,
    "BURN 5h", and "67k/h • rising".
  - **Inks:** from `theme_burn_inks()`.
- **Status-bar link mark:**
  - **USB:** a USB-C cable end lying flat.
  - **WiFi:** two arcs and a dot, at the same weight.
  - Not the USB trident, which reads as a fork at this size, and never a
    plug standing up, which reads as the "Encrypted" padlock.
- **Settings screen** (`ui_settings.c`): Back, "Settings", four 144×71
  tiles (Brightness, **Open settings / On your computer**, Focus, Update),
  and the footer "Blink 1.4.1 | App 1.4.1" / "Keep the cable connected".
- **Start-up screen:** the logo fitted on its background, or the BLINK mark
  with "No logo yet".

## 5. Responsive (below 1200px, checked at 400px)

- **Single column, max 600px:** the board, name, status and the three
  controls stick to the top (`position: sticky`, `--bg` behind them) and the
  open panel scrolls beneath.
- **Board size:** the board crops to the case and its shadow, at most
  `min(34vh, 300px)` tall.
- **Hidden:** the desk objects and link lines.
- **Look:** shows Themes first, then Customise below it.
- **Panels:** full width, no height cap. They enter 10px upward.
- **Scroll padding:** `html { scroll-padding-top }` equals the stuck hub's
  height, so a focused field is never hidden under the board.
- **Side gutters:** `max(16px, env(safe-area-inset-left/right))`.
- **No horizontal scroll at 400px.** Theme tiles stay 3 across, view cards
  4 across, and colour rows go to `44 | 1fr | 92`.

## 6. Accessibility

- **Contrast:** 4.5:1 minimum for all text in both schemes (measured lowest
  5.46:1). Non-text indicators are at least 3:1.
- **Focus:** a visible 2px `--accent` outline with a 2px offset on
  everything. Inputs that draw their own box use a 1.5px inset accent ring
  instead.
- **Labels:** every input has a visible label. Icon-only buttons carry an
  `aria-label`. Decorative SVGs are `aria-hidden`.
- **Live regions:** the status line, the handshake step, the result
  messages and the theme name are `aria-live="polite"` or `role="status"`.
- **The board:** `role="img"`, with a title that says what it shows
  ("Board preview: Claude, session 17%, resets in 3h 12m; weekly 24%…").
- **Targets:** 44px minimum. `touch-action: manipulation` on buttons.
- **Motion:** honour `prefers-reduced-motion` (section 2).
- **No browser dialogs:** no `alert`, `confirm` or `prompt`; use the
  in-page confirm.
- **Brand:** `translate="no"` on BLINK and the board name.

## 7. Mock-only (do not ship as-is)

- **Example data:** every name, network, address, percentage, token count
  and version.
- **Simulated flows:**
  - **WiFi join:** the timings, and `wrong` as the failing password.
  - **Logo send:** the progress bar.
  - **Unreachable state:** `#unreachable` in the address.
- **Footer note:** "Example data. This page is served by your computer…".
- **Logo size estimate:** "About N KB" is a rough figure (about three bytes
  per run of one colour along a row). The real encoder
  (`tools/encode_logo.py`) gives the true size.
- **Burn view and Focus "Weekly big":** previews only. The burn view is
  automatic on the board.
- **"New" items need firmware or daemon work first:**
  - sending a logo over USB (today the logo is factory-only, esptool);
  - the board's Open settings tile;
  - the doze switch;
  - usage refresh, until feat/usage-refresh ships.
- **Google Fonts:** the mock loads them. The shipped page must not.
