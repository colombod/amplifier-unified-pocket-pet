import { ESPLoader, Transport } from "esptool-js";
import { md5 } from "@noble/hashes/legacy";
import { bytesToHex } from "@noble/hashes/utils";
import manifest from "./firmware.json";

const APP = 0x10000, SIZE = 0x1000000, CAP = 0xe00000;
const tableDigest = "5a349f86ef3dc2e5421cfc191e2f70030b67eff9eb81fad7b1d0219e57657302";
const releasedDigest = "337b6ff93d67527f675f0d2ac1e88b7f3660e819ad30ca3dcbfcc94bde9ff4a4";
const checksum = bytes => bytesToHex(md5(bytes));
export const sha = async bytes => bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)));
function requireCheck(ok, message) { if (!ok) throw new Error(message); }

export async function checkImage(bytes, revision, installed = false) {
  const data = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const text = (start, size) => new TextDecoder().decode(bytes.subarray(start, start + size)).replace(/\0+$/, "");
  requireCheck(bytes.length >= 256 && bytes[0] === 0xe9 && bytes[1] > 0 && bytes[1] <= 16 &&
    bytes[23] === 1 && data.getUint16(12, true) === 9 && data.getUint32(32, true) === 0xabcd5432 &&
    text(80, 32) === "unified_pet", "The selected board or image does not contain Pocket firmware.");
  const low = data.getUint16(15, true), high = data.getUint16(17, true);
  const compatible = high === 0 || (high === 65535 && !low && bytes[14]) ?
    Math.floor(revision / 100) >= bytes[14] : revision >= low && revision <= high;
  requireCheck(compatible, "Unsupported ESP32-S3 chip revision.");
  let cursor = 24;
  for (let i = 0; i < bytes[1]; i++) {
    requireCheck(cursor + 8 <= bytes.length, "Incomplete ESP segment header.");
    cursor += 8 + data.getUint32(cursor + 4, true);
  }
  const end = Math.ceil((cursor + 1) / 16) * 16 + 32;
  requireCheck(end <= CAP && (installed ? end <= bytes.length : end === bytes.length), "Unexpected application length.");
  const digest = await sha(bytes.subarray(0, end));
  requireCheck(digest === manifest.sha256 || (installed && digest === releasedDigest), "Unrecognized firmware digest. No update is available for this image.");
  return { version: text(48, 32), digest };
}
export async function openUpdater(port) {
  const transport = new Transport(port, false);
  const terminal = { clean() {}, writeLine() {}, write() {} };
  const loader = new ESPLoader({ transport, terminal, baudrate: 115200, romBaudrate: 115200 });
  try {
    await loader.detectChip("default_reset");
    const security = await loader.getSecurityInfo();
    requireCheck(loader.chip.CHIP_NAME === "ESP32-S3" && security.chipId === 9 &&
      !security.parsedFlags.SECURE_BOOT_EN && !security.parsedFlags.SECURE_DOWNLOAD_ENABLE &&
      security.flashCryptCnt === 0,
      "This updater requires an unencrypted 16 MB ESP32-S3 Pocket Pet.");
    await loader.chip.postConnect(loader);
    await loader.runStub();
    requireCheck(loader.IS_STUB && await loader.detectFlashSize() === "16MB", "Expected a 16 MB Pocket Pet.");
    const revision = 100 * await loader.chip.getMajorChipVersion(loader) + await loader.chip.getMinorChipVersion(loader);
    return { loader, revision, mac: await loader.chip.readMac(loader), transport };
  } catch (e) { try { await loader.after("hard_reset"); } catch {} await transport.disconnect().catch(() => {}); throw e; }
}
async function readChecked(loader, address, size, progress) {
  const output = new Uint8Array(size);
  for (let pos = 0; pos < size;) {
    const count = Math.min(256 * 1024, size - pos);
    const chunk = await loader.readFlash(address + pos, count);
    // The stub appends one binary MD5 packet after each complete read.
    const digest = await loader.transport.read(loader.FLASH_READ_TIMEOUT);
    requireCheck(chunk.length === count && bytesToHex(digest) === checksum(chunk), "USB flash read failed verification.");
    output.set(chunk, pos); pos += count; progress(pos / size);
  }
  return output;
}
export async function prepareUpdate(device, handle, progress) {
  requireCheck((await handle.getFile()).size === 0, "Choose a new empty backup file.");
  const { loader, revision } = device;
  const response = await fetch("./firmware.bin", {cache:"no-store"});
  requireCheck(response.ok, "Firmware download failed.");
  const candidate = new Uint8Array(await response.arrayBuffer());
  await checkImage(candidate, revision);
  const backup = await readChecked(loader, 0, SIZE, progress);
  try {
    requireCheck(await sha(backup.subarray(0x8000, 0x8c00)) === tableDigest, "The partition layout does not match this board.");
    const installed = await checkImage(backup.subarray(APP, APP + CAP), revision, true);
    const digest = await sha(backup);
    const file = await handle.createWritable();
    try { await file.write(backup); await file.close(); } catch (e) { await file.abort().catch(() => {}); throw e; }
    requireCheck(await sha(await (await handle.getFile()).arrayBuffer()) === digest, "Saved backup failed SHA-256 readback.");
    const end = APP + Math.ceil(candidate.length / 4096) * 4096;
    return { candidate, installed, end, before: checksum(backup.subarray(0, APP)), after: checksum(backup.subarray(end)),
      original: checksum(backup), mac: device.mac, current: installed.digest === manifest.sha256 };
  } finally { backup.fill(0); }
}
export async function writeUpdate(device, plan, progress) {
  const { loader } = device;
  requireCheck(!plan.used && !plan.current && await loader.chip.readMac(loader) === plan.mac, "Reconnect and make a fresh backup.");
  plan.used = true;
  const flashHash = async (start, length) => (await loader.flashMd5sum(start, length)).toLowerCase();
  requireCheck(await flashHash(0, SIZE) === plan.original, "Flash changed after backup. Reconnect and back up again.");
  loader.WRITE_BLOCK_ATTEMPTS = 1;
  await loader.writeFlash({ fileArray: [{ address: APP, data: plan.candidate }], flashSize:"keep",
    flashMode:"keep", flashFreq:"keep", eraseAll:false, compress:true,
    reportProgress: (_index, done, total) => progress(done / total) });
  requireCheck(await sha(await readChecked(loader, APP, plan.candidate.length, progress)) === manifest.sha256 &&
    await flashHash(0, APP) === plan.before && await flashHash(plan.end, SIZE - plan.end) === plan.after,
    "Verification failed. Keep your backup and use the project recovery instructions.");
}
