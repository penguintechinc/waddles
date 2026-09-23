import js from "@eslint/js";
import tseslint from "typescript-eslint";
import globals from "globals";

export default tseslint.config(
  {
    ignores: ["dist/**", "dist-tests/**", "src/generated/**", "node_modules/**"],
  },
  js.configs.recommended,
  {
    files: ["src/**/*.ts", "tests/**/*.ts"],
    extends: [...tseslint.configs.strictTypeChecked],
    languageOptions: {
      parserOptions: {
        project: ["./tsconfig.test.json"],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Public-surface `any` is banned per SDK standards; explicit escape
      // hatches (rare) should use `unknown` + a narrowing type guard instead.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  {
    // `node:test`'s `test()` returns a Promise that is idiomatically never
    // awaited at the top level (the test runner awaits it internally);
    // requiring `void test(...)` on every call adds noise without value.
    files: ["tests/**/*.ts"],
    rules: {
      "@typescript-eslint/no-floating-promises": "off",
    },
  },
  {
    // Plain JS build tooling -- not part of any tsconfig project, no
    // type-checked linting (build-shim.mjs is intentionally dependency-free
    // plain JS; this config file is ESLint's own entry point).
    files: ["*.mjs", "src/*.mjs"],
    languageOptions: {
      sourceType: "module",
      globals: globals.node,
    },
  },
);
