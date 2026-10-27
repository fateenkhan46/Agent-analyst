import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: "#0F1419", soft: "#1A212B", line: "#263040" },
        paper: { DEFAULT: "#FFFFFF", soft: "#F6F8FB", line: "#E3E8F0" },
        brand: { DEFAULT: "#2E6BE6", dark: "#1F4FB8", light: "#E8F0FE" },
        ok: "#17A398",
        warn: "#F2A541",
        bad: "#D1495B",
        muted: "#64748B",
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
} satisfies Config;
