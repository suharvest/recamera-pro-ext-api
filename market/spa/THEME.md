# reCamera Pro — App Center Theme Spec

Visual regime extracted from the **official dashboard** (闭源编译 React) served from the
device at `/oem/usr/www/` — `static/css/main.c1e7aa83.css` (195 KB). The App Center SPA
(`market/spa/index.html`, vanilla, no build) mirrors these tokens so the two consoles read
as one product.

## Fonts (reused, not re-hosted)

Official `@font-face` declarations point at same-origin assets under `/static/media/`.
Our SPA declares the identical `@font-face` rules against those same paths — **no CDN, no
copy** (the woff2 already ship on the device: 4× Montserrat, 3× Source Han Sans SC).

| family | weights | files (hashed) |
|---|---|---|
| Montserrat | 400/500/600/700 | `Montserrat-{Regular,Medium,SemiBold,Bold}.<hash>.woff2` |
| Source Han Sans SC | 400/500/700 | `SourceHanSansSC-{Regular,Medium,Bold}.<hash>.woff2` |

- `--font-ui: "Montserrat","Source Han Sans SC","Noto Sans SC",sans-serif`
- `--font-cjk: "Source Han Sans SC","Noto Sans SC",sans-serif`
- `--font-mono: "Consolas","Monaco","Courier New",monospace`

## Brand color

`#8fc31f` — the reCamera lime-green. Used for sidebar-active text/indicator, progress fill,
selected state, primary action buttons, and the "running/live" status glow.

## Theme tokens (both themes shipped; toggled via `body[data-theme=dark|light]`)

| token | dark (default) | light |
|---|---|---|
| `--brand` | `#8fc31f` | `#8fc31f` |
| `--sidebar-bg` | `#1f1f1f` | `#f7f9f9` |
| `--sidebar-text` | `#ffffff8c` | `#000` |
| `--sidebar-active-bg` | `#3a5700` | `#ecf4d9` |
| `--sidebar-active-text` | `#fff` | `#8fc31f` |
| `--sidebar-icon` | `#ffffffdb` | `#000` |
| `--bg-primary` (page) | `#1a1a1a` | `#f5f5f5` |
| `--bg-secondary` | `#2d2d2d` | `#fff` |
| `--bg-tertiary` | `#242424` | `#fafafa` |
| `--card-bg` | `#1f1f1f` | `#f7f9f9` |
| `--card-header-border` | `#383838` | `#e7e7e7` |
| `--card-header-title` | `#ffffff8c` | `#3d3d3d` |
| `--content-text` | `#fff` | `#000` |
| `--border-color` | `#404040` | `#ecf0f1` |
| `--border-color-secondary` | `#505050` | `#ddd` |
| `--text-primary` | `#e0e0e0` | `#2c3e50` |
| `--text-secondary` | `#a0a0a0` | `#0006` |
| `--button-primary` (blue) | `#3498db` | `#3498db` |
| `--button-danger` | `#e74c3c` | `#e74c3c` |
| `--button-success` | `#2ecc71` | `#2ecc71` |
| `--button-warning` | `#f39c12` | `#f39c12` |
| `--progress-fill` | `#8fc31f` | `#8fc31f` |
| `--progress-bg` | `#404040` | `#ecf0f1` |
| `--card-shadow` | `#00000080` | `#0000001a` |
| `--terminal-bg` | `#141414` | `#fff` |

## Component specs

- **Layout** — `.app-container` flex; fixed left `.sidebar` `width: clamp(180px,14vw,260px)`;
  main content padded region over `--bg-primary`.
- **Sidebar item** — `margin:3px 10px; padding:12px 16px; gap:16px; font:14px var(--font-ui)`,
  icon 24px. Active: `background:var(--sidebar-active-bg); color:var(--sidebar-active-text);
  font-weight:700`.
- **Sidebar controls** (bottom) — round 34px buttons; theme toggle lives here (official
  places it bottom-left).
- **Card** — `background:var(--card-bg); border-radius:12px; box-shadow:0 2px 8px #00000014`;
  header `padding:1.25rem 1.5rem; border-bottom:1px solid var(--border-color); title 1.25rem/600`;
  body `padding:1.5rem`.
- **Button** — `border:2px solid; border-radius:8px; font-weight:500; padding:.375rem .75rem (sm)`.
  Primary action = brand green (`background:var(--brand); color:#1a2b00`); danger = outlined red.
- **Switch** — 38×22 track, checked = `var(--brand)`.
- **Status indicator** — 10px round dot, `box-shadow:0 0 5px currentColor`; green = running/live.
- **Status badge** — `border-radius:99px; padding:4px 8px; font:14px/22px var(--font-ui)`.
- **Radii** — 8px (buttons/inputs), 12px (cards), 99px (badges/pills).

## Notes

- Default theme = **dark** (matches official first-paint); toggle persists in
  `localStorage.appcenter_theme`.
- All API/WS/video/auth paths are unchanged — this is a skin + layout shell only.
</content>
