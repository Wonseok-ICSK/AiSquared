// Test-only loader: lets `node --test` import the app's extensionless TypeScript modules
// ("../src/lib/tripForm" -> tripForm.ts). Node 24 strips the types of .ts files itself; .tsx
// modules (e.g. lib/i18n.tsx) are transpiled with rolldown's JSX transform (a Vite dependency).
// Only modules without DOM access at import time can be tested this way.
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";
import { transformSync } from "rolldown/utils";

const CANDIDATES = [".ts", ".tsx", "/index.ts"];

registerHooks({
  resolve(specifier, context, nextResolve) {
    const relative = specifier.startsWith("./") || specifier.startsWith("../");
    if (!relative || /\.[cm]?[jt]sx?$/.test(specifier)) return nextResolve(specifier, context);
    for (const ext of CANDIDATES) {
      try {
        return nextResolve(specifier + ext, context);
      } catch {
        // try the next candidate
      }
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (!url.startsWith("file:") || !url.endsWith(".tsx")) return nextLoad(url, context);
    const path = fileURLToPath(url);
    const result = transformSync(path, readFileSync(path, "utf8"), { jsx: { runtime: "automatic" } });
    if (result.errors.length) throw new Error(`${path}: ${result.errors.map((e) => e.message).join("; ")}`);
    return { format: "module", source: result.code, shortCircuit: true };
  },
});
