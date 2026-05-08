import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        serif: ["var(--font-serif)", "ui-serif", "Georgia", "serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      colors: {
        ink: {
          50:  "#fafaf7",
          100: "#f5f5f1",
          200: "#e9e7e1",
          300: "#d6d3cc",
          500: "#78716c",
          700: "#44403c",
          900: "#1c1917",
        },
      },
      boxShadow: {
        card: "0 1px 2px rgba(28, 25, 23, 0.04), 0 1px 3px rgba(28, 25, 23, 0.05)",
        lift: "0 1px 2px rgba(28, 25, 23, 0.04), 0 12px 28px -16px rgba(28, 25, 23, 0.18)",
      },
      borderRadius: {
        xl: "12px",
        "2xl": "16px",
      },
    },
  },
  plugins: [],
};

export default config;
