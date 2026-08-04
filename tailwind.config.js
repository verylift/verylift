/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./**/templates/**/*.html",
    "./**/forms.py",
  ],
  // Remove Tailwind's default color palette; only brand-approved tokens are allowed.
  theme: {
    colors: {
      transparent: "transparent",
      current: "currentColor",
      white: "#ffffff",
      black: "#000000",
      // --- Brand palette (emerald — design system "Emerald-light") ---
      brand: {
        50: "#e7f6ee",
        100: "#c8ecd8",
        200: "#93dcb4",
        300: "#5fca91",
        400: "#34b87d",
        500: "#1f9d63", // primary
        600: "#178052",
        700: "#136843",
        800: "#0f5236",
        900: "#0a3a26",
      },
      neutral: {
        50: "#f9fafb",
        100: "#f3f4f6",
        200: "#e5e7eb",
        300: "#d1d5db",
        400: "#9ca3af",
        500: "#6b7280",
        600: "#4b5563",
        700: "#374151",
        800: "#1f2937",
        900: "#111827",
      },
      success: {
        light: "#dcfce7",
        DEFAULT: "#16a34a",
        dark: "#166534",
      },
      warning: {
        light: "#fef9c3",
        DEFAULT: "#ca8a04",
        dark: "#713f12",
      },
      danger: {
        light: "#fee2e2",
        DEFAULT: "#dc2626",
        dark: "#7f1d1d",
      },
      // --- Theme-aware semantic tokens (flip under [data-theme="dark"]) ---
      surface: {
        DEFAULT: "var(--surface-page)",
        page: "var(--surface-page)",
        card: "var(--surface-card)",
        raised: "var(--surface-raised)",
        sunken: "var(--surface-sunken)",
        hover: "var(--surface-hover)",
      },
      content: {
        strong: "var(--text-strong)",
        body: "var(--text-body)",
        muted: "var(--text-muted)",
        subtle: "var(--text-subtle)",
        inverse: "var(--text-inverse)",
      },
      line: {
        DEFAULT: "var(--border-default)",
        subtle: "var(--border-subtle)",
        strong: "var(--border-strong)",
      },
      accent: {
        DEFAULT: "var(--accent)",
        hover: "var(--accent-hover)",
        active: "var(--accent-active)",
        subtle: "var(--accent-subtle)",
        on: "var(--text-on-brand)",
      },
    },
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
    },
  },
  plugins: [],
};
