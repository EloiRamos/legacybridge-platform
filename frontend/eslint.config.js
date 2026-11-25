import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "playwright-report", "test-results", "src/api/schema.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // XSS boundary: React escaping only — raw HTML injection is forbidden.
      "no-restricted-syntax": ["error", {
        selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
        message: "dangerouslySetInnerHTML is forbidden",
      }],
      "no-restricted-globals": ["error", { name: "localStorage", message: "no tokens or state in storage" },
        { name: "sessionStorage", message: "no tokens or state in storage" }],
    },
  },
);
