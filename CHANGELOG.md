# Changelog

All notable changes to the SAE Books web frontend will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] - 2026-05-08

### Fixed

- **Dark-mode rendering on OS-dark users who toggled to light.** Dropped the
  Tailwind Play CDN in favour of a built static `tailwind.css` generated at
  image build time by the standalone Tailwind binary. The CDN has a documented
  race that causes some `dark:*` utilities to compile under
  `@media (prefers-color-scheme: dark)` instead of the class strategy, even
  when `darkMode: 'class'` is configured. Both light and dark themes are now
  deterministic and CDN-independent.

### Changed

- `tailwind.config.js` added at repo root; `assets/tailwind.css` is the source
  entry point scanned by the Tailwind compiler.
- All `<style>` blocks from `base.html` (dark-mode overrides, sidebar
  scrollbar, `.icon-btn` variants) moved into `assets/tailwind.css`.
- `StaticFiles` mount added to `saebooks_web/main.py` at `/static`; resolves
  `/app/static` in Docker and `./static` for local dev.
- `scripts/build_css.sh` added for local development CSS builds.
- Docker build gains a `tailwind` stage (Stage 0) that produces the minified
  CSS using the standalone binary; output is copied into `/app/static/` in the
  runtime image.

## [0.1.2] - 2026-05-08

### Fixed

- **Dashboard overdue/due-soon counts.** AR and AP tiles now exclude
  invoices/bills where `amount_paid >= total`, so a fully-paid overdue
  document no longer appears in the red banner.
- **Dashboard "Outstanding" tile.** Replaces the never-populated "Paid
  this month" tile (the API has no PAID enum); shows count of invoices
  with `amount_paid < total`.
- **Header company name.** Shows the active company's legal name (or
  trading name) instead of the literal string "SAE Books".

### Changed

- **Navigation.** Moved Payments link from Customers section into
  Banking section — payments aren't customer-only and the prior
  placement made them invisible when working on bills.

### Build

- `build-images.yml` now logs into Docker Hub on `v*.*.*` tags and
  pushes multi-arch images, parity with the saebooks repo. Branch
  builds still verify the build without polluting the registry.

## [0.1.1] - 2026-05-08

### Added

- **Purchase orders UI.** `/purchase_orders` list with status, date and contact
  filters and HTMX-aware pagination; `/purchase_orders/{id}` detail with
  status-conditional Send / Cancel / Close / Convert-to-bill actions and an
  inline per-line conversion form; `/purchase_orders/new` create form with an
  HTMX-driven add-line button.
- **Prorate calculator.** `/proration` three-tab interactive calculator
  (per-line, first-period sign-up, plan-change). Each tab calls its preview
  endpoint and swaps a result fragment into the page — no DB writes.
- Sidebar: Vendors → Purchase Orders link added above Bills; Assets & Setup →
  Prorate Calculator link added.

### Fixed

- ATO/SBR admin UI now wires to the wizard endpoints correctly.

## [0.1.0] - 2026-05-08

Initial public alpha.
