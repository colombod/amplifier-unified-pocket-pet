import { SettingsPort, pickPet } from "./port.js";
import { openUpdater, prepareUpdate, writeUpdate } from "./flash.js";
const $ = id => document.getElementById(id);
const tabs = [...document.querySelectorAll("nav button")];
let settings, updater, plan, busy = false, view = "phone";
const usbAvailable = !!navigator.serial && window.isSecureContext;
function render() {
  for (const tab of tabs) {
    tab.disabled = busy || (!!settings && tab.dataset.view !== "usb") || (!!updater && tab.dataset.view !== "update");
    tab.setAttribute("aria-pressed", String(tab.dataset.view === view));
    $(tab.dataset.view).hidden = tab.dataset.view !== view;
  }
  $("connect").disabled = !usbAvailable || busy || !!settings;
  $("pair").disabled = busy || !!settings;
  $("settings").disabled = busy || !settings;
  $("settings").hidden = !settings;
  $("disconnect").disabled = busy;
  $("disconnect").hidden = !settings;
  $("identify").disabled = !usbAvailable || !window.showSaveFilePicker || busy || !!updater || !$("v2").checked;
  $("v2").disabled = busy || !!updater;
  $("backup").disabled = busy || !updater || !!plan;
  $("flash").disabled = busy || !plan || plan.used || plan.current;
  $("restart").textContent = plan?.used ? "Disconnect without restarting" : "Restart and disconnect";
  $("restart").hidden = !updater; $("restart").disabled = busy;
}
for (const tab of tabs) tab.onclick = () => { view = tab.dataset.view; render(); };
$("v2").onchange = render;
async function action(status, fn) {
  if (busy) return;
  busy = true; render();
  try { await fn(); }
  catch (error) { $(status).textContent = error.name === "AbortError" || error.name === "NotFoundError" ? "Operation cancelled." : error.message; }
  finally { busy = false; render(); }
}
const query = (op, data) => settings.request($("pair").value, op, data);
const form = id => $(id).elements;
function showStatus(s) {
  $("usbStatus").textContent = [s.firmware_version,s.message,s.ssid].filter(Boolean).join(" · ");
}
async function closeSettings() {
  const old = settings; settings = null;
  await old?.close();
  $("pair").value = "";
  for (const id of ["wifi","bridge","peer","tail"]) $(id).reset();
}
$("connect").onclick = () => action("usbStatus", async () => {
  if (!$("pair").value) throw new Error("Enter the password shown on the pet first.");
  const device = new SettingsPort(await pickPet());
  try {
    await device.open(); settings = device;
    const s = await query("status");
    if (typeof s.firmware_version !== "string") throw new Error("Unrecognized setup response.");
    form("wifi").ssid.value = s.ssid || ""; form("bridge").ip.value = s.bridge_ip || "";
    form("bridge").fqdn.value = s.bridge_fqdn || ""; form("bridge").port.value = s.bridge_port || 9443;
    form("bridge").enabled.checked = s.bridge_enabled === true;
    form("peer").host.value = s.unified_host || ""; form("peer").port.value = s.unified_port || 8941;
    showStatus(s);
  } catch (e) { settings = null; await device.close().catch(() => {}); throw e; }
});
$("disconnect").onclick = () => action("usbStatus", async () => { await closeSettings(); $("usbStatus").textContent="Disconnected"; });
$("refresh").onclick = () => action("usbStatus", async () => showStatus(await query("status")));
const saves = {
  wifi: f => [{ssid:f.ssid.value, password:f.password.value, key:f.key.value}, null,
    "Wi-Fi join requested. Refresh connection status to confirm that Wi-Fi connected and the settings were saved."],
  bridge: f => [{action:"bridge_gateway", ip:f.ip.value, fqdn:f.fqdn.value, port:Number(f.port.value),
    enabled:f.enabled.checked, token:f.token.value, clear_token:f.clear.checked}, "bridge_saved", "Gateway settings saved."],
  peer: f => [{action:"unified_target", host:f.host.value, port:Number(f.port.value)}, "unified_target_saved", "Unified peer saved."],
  tail: f => [{action:"tailscale_key", tailscale_auth_key:f.key.value}, "tailscale_key_saved", "Tailscale key settings confirmed."]
};
for (const [id, payload] of Object.entries(saves)) $(id).onsubmit = event => {
  event.preventDefault();
  const [body, flag, message] = payload(form(id));
  action("usbStatus", async () => {
    const reply = await query("save", body);
    if (flag && reply[flag] !== true && !(id === "tail" && !body.tailscale_auth_key && reply[flag] === false))
      throw new Error("The pet did not confirm this save. Check the settings before retrying.");
    for (const input of $(id).querySelectorAll('input[type="password"]')) input.value="";
    if (id === "bridge") form(id).clear.checked = false;
    $("usbStatus").textContent = message;
  });
};
const progress = fraction => { $("progress").hidden=false; $("progress").value=fraction; };
async function disconnectUpdater(reboot) {
  const device = updater; updater = null; plan = null;
  if (!device) return;
  try { if (reboot) await device.loader.after("hard_reset"); }
  finally { await device.transport.disconnect(); }
}
$("identify").onclick = () => action("flashStatus", async () => {
  updater = await openUpdater(await pickPet());
  $("flashStatus").textContent="Connected to ESP32-S3 " + updater.mac + ". Choose a new local file for the 16 MB backup.";
});
$("backup").onclick = () => action("flashStatus", async () => {
  const handle = await showSaveFilePicker({suggestedName:"pocket-pet-" + updater.mac.replaceAll(":","") + "-" + Date.now() + ".bin"});
  $("flashStatus").textContent="Reading all flash and verifying the saved backup. Keep the USB cable connected.";
  plan = await prepareUpdate(updater, handle, progress);
  $("flashStatus").textContent=plan.current ? "Backup verified. The preview is already installed." :
    "Backup verified. Installed " + plan.installed.version + ". Flash firmware is ready.";
});
$("flash").onclick = () => action("flashStatus", async () => {
  $("flashStatus").textContent="Writing and verifying the application. Keep the cable connected.";
  try { await writeUpdate(updater,plan,progress); }
  catch (e) { if(plan)plan.used=true; throw new Error("Update not confirmed. Keep the backup. " + e.message); }
  await disconnectUpdater(true);
  $("flashStatus").textContent="Application verified. Settings partitions matched the backup. The pet was restarted.";
});
$("restart").onclick = () => action("flashStatus", async () => {
  const reboot = !plan?.used;
  await disconnectUpdater(reboot); $("flashStatus").textContent=reboot ? "Restarted and disconnected." : "Disconnected. Keep your backup for recovery."; $("progress").hidden=true;
});
navigator.serial?.addEventListener("disconnect", async event => {
  if (settings && event.target === settings.port) { await closeSettings(); $("usbStatus").textContent="USB disconnected. Reconnect to check saved settings."; }
  if (updater && event.target === updater.transport.device) { plan=null; await disconnectUpdater(false).catch(()=>{}); $("flashStatus").textContent="USB disconnected. Reconnect and verify before continuing."; }
  render();
});
if (!usbAvailable) {
  $("usbStatus").textContent="This browser does not offer Web Serial. Use phone setup or desktop Chrome or Edge.";
  $("flashStatus").textContent=$("usbStatus").textContent;
} else if (!window.showSaveFilePicker) $("flashStatus").textContent="Choose desktop Chrome or Edge with local file saving enabled.";
render();