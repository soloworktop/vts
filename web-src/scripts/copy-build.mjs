import { cpSync, readdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// 把 Vite 构建产物（web-src/dist）复制到 FastAPI 托管的 web/static。
// 保留 static/ 下非构建产物（user-guide.html / README.md），其余全部由构建产物覆盖。
const here = path.dirname(fileURLToPath(import.meta.url)); // web-src/scripts
const dist = path.join(here, "..", "dist");
const staticDir = path.resolve(here, "../../src/video_to_summary/web/static");

const KEEP = new Set(["user-guide.html", "README.md"]);
for (const name of readdirSync(staticDir)) {
  if (!KEEP.has(name)) {
    rmSync(path.join(staticDir, name), { recursive: true, force: true });
  }
}
for (const name of readdirSync(dist)) {
  cpSync(path.join(dist, name), path.join(staticDir, name), { recursive: true });
}
console.log("[build] web-src/dist → src/video_to_summary/web/static (kept: user-guide.html, README.md)");
