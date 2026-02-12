/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: [
    "./index.html",
    "./**/*.{js,ts,jsx,tsx}",
    "!./node_modules/**",
  ],
  theme: {
    extend: {
      colors: {
        "primary": "#ffffff",
        "primary-dim": "#a1a1aa",
        "background-light": "#f5f6f8",
        "background-dark": "#09090b",
        "panel-dark": "#0c0c0e",
        "border-dark": "#27272a",
        "accent-blue": "#3b82f6",
      },
      fontFamily: {
        "display": ["Inter", "sans-serif"],
        "mono": ["JetBrains Mono", "monospace"],
        "serif": ["Playfair Display", "serif"],
      },
    },
  },
  plugins: [
    require("@tailwindcss/forms"),
    require("@tailwindcss/container-queries"),
  ],
}
