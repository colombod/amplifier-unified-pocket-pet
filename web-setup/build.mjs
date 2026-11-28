import { build } from "esbuild";
import { mkdir, readFile, writeFile, copyFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
process.chdir(fileURLToPath(new URL(".", import.meta.url)));
const image = await readFile(process.argv[2] || "../apps/unified-pet/build/unified_pet.bin");
const manifest = JSON.parse(await readFile("firmware.json"));
if (createHash("sha256").update(image).digest("hex") !== manifest.sha256) throw Error("Build does not match the pinned preview artifact");
await mkdir("dist",{recursive:true});
await build({entryPoints:["app.js"],outfile:"dist/app.js",bundle:true,format:"esm",target:"chrome110",minify:true});
for (const file of ["index.html","style.css","pet.png"]) await copyFile(file,"dist/"+file);
await writeFile("dist/firmware.bin",image);
const licenses = ["../LICENSE","firmware-notices.txt","node_modules/esptool-js/LICENSE","node_modules/@noble/hashes/LICENSE",
  "node_modules/pako/LICENSE","node_modules/atob-lite/LICENSE.md"];
const notices = [];
for (const file of licenses) notices.push(file+"\n"+await readFile(file,"utf8"));
await writeFile("dist/notices.txt",notices.join("\n\n"));
