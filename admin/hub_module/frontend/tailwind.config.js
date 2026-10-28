/**
 * Waddles Hub design tokens -> Tailwind.
 *
 * Two layers:
 *  1. Raw brand scales (navy / sky / gold / emerald) -- unchanged, so every
 *     existing `bg-navy-900` / `text-gold-400` class keeps working.
 *  2. Semantic tokens (surface / fg / line / focus / status) that read CSS
 *     variables from `src/styles/tokens.css`. New and refactored UI should use
 *     these: `bg-surface`, `text-fg-muted`, `border-line`, `ring-focus`.
 *
 * Semantic names map 1:1 onto a Tailwind v4 `@theme` block (`--color-surface`,
 * `--color-fg`, ...), so the pending v3 -> v4 migration is a rename, not a
 * redesign. Every text/background pair is contrast-checked in
 * `src/styles/__tests__/tokens.test.ts`.
 *
 * @type {import('tailwindcss').Config}
 */

/** Wraps a space-separated RGB CSS variable so `/opacity` modifiers keep working. */
const token = (name) => `rgb(var(${name}) / <alpha-value>)`;

const sky = {
  50: '#f0f9ff',
  100: '#e0f2fe',
  200: '#bae6fd',
  300: '#7dd3fc',
  400: '#38bdf8',
  500: '#0ea5e9',
  600: '#0284c7',
  700: '#0369a1',
  800: '#075985',
  900: '#0c4a6e',
};

export default {
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
    // @penguintechinc/react-libs ships Tailwind class names inside its compiled
    // JS (SidebarMenu: `lg:flex`, `inset-y-0`, `-translate-x-full`; FormModalBuilder:
    // `sm:*` modal sizing). Tailwind only emits classes it can see, so without
    // this entry the shared sidebar never appears at desktop widths.
    './node_modules/@penguintechinc/react-libs/dist/**/*.js',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Navy - primary dark backgrounds
        navy: {
          50: '#f0f4f8',
          100: '#d9e2ec',
          200: '#bcccdc',
          300: '#9fb3c8',
          400: '#829ab1',
          500: '#627d98',
          600: '#486581',
          700: '#334e68',
          800: '#243b53',
          900: '#102a43',
          950: '#0a1929',
        },
        // Light blue - interactive accents
        sky,
        // `primary` is the name @penguintechinc/react-libs components use for
        // their accent (e.g. the active SidebarMenu item). Alias to sky so the
        // shared components pick up the Waddles palette instead of rendering
        // unstyled.
        primary: sky,
        // Gold - highlights, CTAs
        gold: {
          50: '#fffbeb',
          100: '#fef3c7',
          200: '#fde68a',
          300: '#fcd34d',
          400: '#fbbf24',
          500: '#f59e0b',
          600: '#d97706',
          700: '#b45309',
          800: '#92400e',
          900: '#78350f',
        },
        // Green - success
        emerald: {
          50: '#ecfdf5',
          100: '#d1fae5',
          200: '#a7f3d0',
          300: '#6ee7b7',
          400: '#34d399',
          500: '#10b981',
          600: '#059669',
          700: '#047857',
          800: '#065f46',
          900: '#064e3b',
        },
        // Waddles brand colors (legacy aliases)
        waddle: {
          navy: '#102a43',
          lightBlue: '#38bdf8',
          gold: '#fbbf24',
          green: '#10b981',
          dark: '#0a1929',
          light: '#e0f2fe',
        },

        // ---- Semantic tokens (see src/styles/tokens.css) ----
        app: token('--c-app'),
        surface: {
          DEFAULT: token('--c-surface'),
          raised: token('--c-raised'),
          field: token('--c-field'),
        },
        fg: {
          DEFAULT: token('--c-fg'),
          secondary: token('--c-fg-secondary'),
          muted: token('--c-fg-muted'),
          accent: token('--c-accent-fg'),
          inverse: token('--c-fg-inverse'),
        },
        line: {
          DEFAULT: token('--c-line'),
          strong: token('--c-line-strong'),
          input: token('--c-line-input'),
        },
        focus: token('--c-focus'),
        interactive: {
          DEFAULT: token('--c-interactive'),
          hover: token('--c-interactive-hover'),
        },
        accent: {
          DEFAULT: token('--c-accent'),
          hover: token('--c-accent-hover'),
        },
        success: {
          DEFAULT: token('--c-success'),
          fg: token('--c-success-fg'),
        },
        warning: {
          DEFAULT: token('--c-warning'),
          fg: token('--c-warning-fg'),
        },
        danger: {
          DEFAULT: token('--c-danger'),
          hover: token('--c-danger-hover'),
          fg: token('--c-danger-fg'),
        },
        info: {
          DEFAULT: token('--c-info'),
          fg: token('--c-info-fg'),
        },
      },
      // Legacy dark-theme aliases (kept for existing pages)
      backgroundColor: {
        dark: '#0a1929',
        'dark-card': '#102a43',
        'dark-hover': '#243b53',
      },
      textColor: {
        'dark-primary': '#e0f2fe',
        'dark-secondary': '#9fb3c8',
        'dark-muted': '#627d98',
      },
      borderColor: {
        'dark-border': '#334e68',
      },
      fontFamily: {
        // System UI stack: native, zero network cost, no layout shift. A brand
        // typeface is a design decision -- see DESIGN_SYSTEM.md "Open questions".
        sans: [
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'BlinkMacSystemFont',
          '"Segoe UI"',
          'Roboto',
          '"Helvetica Neue"',
          'Arial',
          'sans-serif',
        ],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      fontSize: {
        // Replaces ad-hoc `text-[10px]` / `text-[11px]` usage.
        '2xs': ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.02em' }],
      },
      borderRadius: {
        control: 'var(--radius-control)',
        card: 'var(--radius-card)',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        raised: 'var(--shadow-raised)',
        overlay: 'var(--shadow-overlay)',
      },
      ringColor: {
        DEFAULT: token('--c-focus'),
      },
      ringOffsetColor: {
        DEFAULT: token('--c-app'),
      },
      minHeight: {
        // WCAG 2.5.8 target size: 24px minimum, 40px for primary controls.
        control: '2.5rem',
      },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'slide-up': {
          from: { opacity: '0', transform: 'translateY(0.5rem)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 150ms ease-out',
        'slide-up': 'slide-up 200ms ease-out',
      },
    },
  },
  plugins: [],
};
