import { resolve } from "node:path";

export default {
  publicDir: false,
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  resolve: {
    alias: {
      react: resolve(__dirname, "../../../../web/node_modules/react"),
      "react-dom": resolve(__dirname, "../../../../web/node_modules/react-dom"),
      "@omnigent/extension-sdk": resolve(
        __dirname,
        "../../../../sdks/web-extension/src/index.ts",
      ),
    },
  },
  build: {
    outDir: resolve(__dirname, "../src/omnigent_linear_extension/dist"),
    emptyOutDir: true,
    lib: {
      entry: resolve(__dirname, "src/main.ts"),
      name: "OmnigentLinearExtension",
      formats: ["iife"],
      fileName: () => "extension.js",
    },
    rollupOptions: {
      output: {
        assetFileNames: (asset) =>
          asset.names.some((name) => name.endsWith(".css"))
            ? "extension.css"
            : "[name][extname]",
      },
    },
  },
};
