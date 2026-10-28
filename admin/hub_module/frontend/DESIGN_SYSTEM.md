# Waddles Hub design system

Dark theme only. Navy surfaces, gold for the one primary action, sky for links and focus.
Live reference: run `npm run dev` and open `/design-system` (development builds only).

## Layers

| Layer | File | Use |
|---|---|---|
| Tokens | `src/styles/tokens.css` | Semantic CSS variables (RGB triplets). Contrast-checked by `src/styles/__tests__/tokens.test.ts`. |
| Tailwind mapping | `tailwind.config.js` | `bg-surface`, `text-fg-muted`, `border-line`, `ring-focus`, ... Raw `navy/sky/gold/emerald` scales still work. |
| Global defaults | `src/index.css` | `:focus-visible`, `prefers-reduced-motion`, `color-scheme: dark`, and the `.btn` `.input` `.card` `.badge` `.alert` table classes ~180 legacy pages use. |
| Primitives | `src/components/ui` | React components with the accessibility wiring built in. **Import from the barrel (`components/ui`).** |
| Shared library | `@penguintechinc/react-libs` | Login, `SidebarMenu`, `FormModalBuilder` (modals). Do not rebuild these. |

## Rules

- Every form control has a label (`TextField`/`SelectField`/`SwitchField`... take `label` as a required prop).
- Icon-only buttons use `IconButton` (required `label`).
- Anything clickable is a `<button>` or `<a>`. A non-button that must act like one needs `role="button" tabIndex={0} onKeyDown={onActivate(fn)}`.
- A row/card click that duplicates a real in-row button/link is marked `data-click-delegate`.
- Never remove a focus outline without a replacement ring (`focus-visible:ring-*`).
- Every list/query renders loading, error (with retry) and empty states: `DataTable`, `QueryBoundary`, `LoadingState`, `ErrorState`, `EmptyState`.
- Server state goes through TanStack Query; new API modules live in `src/services/<domain>Api.ts` on the typed `apiClient`.
- Status is never colour-only: badges carry text.
- Text on a surface must meet 4.5:1 (use `text-fg`, `text-fg-secondary`, `text-fg-muted`; not `text-navy-500`).

## Guards (all run in `npm test`)

| Guard | What fails it |
|---|---|
| `scripts/__tests__/a11y-budget.test.mjs` | Any unlabeled control, nameless icon button, clickable non-interactive element, outline removal without a replacement, modal overlay without `role="dialog"`, image without `alt`. Budgets are zero. |
| `scripts/__tests__/analyze.test.mjs` | The analyzer going blind (each defect class has a positive fixture). |
| `src/styles/__tests__/tokens.test.ts` | A token change that drops a text pairing below 4.5:1 or a control border below 3:1. |
| axe-core in `src/components/ui/__tests__` | Violations in any primitive state. |

Reports: `npm run audit:ui` (a11y counters + route survey), `npm run audit:api` (UI calls with no hub-api route), or `make webui-audit` from the repo root.

## Known gaps

- Tailwind is v3.4 here (standard is v4). Semantic names map 1:1 to `@theme`; migrate in its own PR with before/after screenshots.
- `@penguintechinc/react-libs` `SidebarMenu`: items are `<button>`s without `aria-current`, `<nav>` has no label, group headers lack `aria-expanded`, no focus trap in the mobile drawer, and its active-item rule is a prefix match (worked around by `layouts/useSidebarNav`). Upstream fixes wanted.
- 36 hand-rolled modals now carry `role="dialog" aria-modal aria-labelledby` but have no focus trap or Escape handling. Migrate to `FormModalBuilder` as pages are touched.
- ~180 pages still fetch with `useEffect` + `useState`; convert to TanStack Query as they are touched.
- `green`/`yellow`/`blue`/`purple`/`orange` Tailwind families are still used for status and platform colours (see Open questions).

## Open questions (need design direction)

1. Brand typeface: currently the system UI stack. A self-hosted face (e.g. Inter) would raise polish.
2. Status palette: collapse green/yellow/blue into emerald/gold/sky, and keep purple (Twitch) / indigo (Discord) only for platform brands?
3. The Super Admin banner (gold-to-green gradient) and the home hero gradient are loud next to the restrained app chrome.
4. Cookie banner takes ~60% of a 320px viewport; shorten copy or switch to a compact bar with a "Customise" sheet.
5. Admin Overview stat tiles use four unrelated icon colours; one accent per tile type would read calmer.
