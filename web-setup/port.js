const encoder = new TextEncoder();
export const pickPet = () => navigator.serial.requestPort({filters:[{usbVendorId:0x303a, usbProductId:0x1001}]});

export class SettingsPort {
  constructor(port) { this.port = port; }
  async open() {
    await this.port.open({baudRate:115200,bufferSize:8192});
    this.reader = this.port.readable.getReader();
    this.writer = this.port.writable.getWriter();
    this.reading = this.receive();
  }
  async receive() {
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const {value, done} = await this.reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream:true});
        const lines = buffer.split("\n"); buffer = lines.pop().slice(-4096);
        for (const line of lines) {
          const m = /^PS1 ([a-f0-9]{8}) (OK|ERR) (.+)$/.exec(line.trimEnd());
          if (!m || m[1] !== this.pending?.id) continue;
          if (m[2] === "ERR") this.pending.reject(new Error(m[3]));
          else {
            try { this.pending.resolve(JSON.parse(m[3])); }
            catch { this.pending.reject(new Error("Incomplete response from the pet.")); }
          }
        }
      }
    } catch {} finally {
      this.ended = true;
      this.pending?.reject(new Error("USB disconnected. Reconnect to check saved settings."));
      this.reader.releaseLock();
    }
  }
  async request(password, op, data) {
    if (this.ended || this.pending) throw new Error("USB unavailable or another request is in progress.");
    if (!/^[!-~]{8,16}$/.test(password)) throw new Error("Enter the password shown on the pet.");
    const id = crypto.getRandomValues(new Uint32Array(1))[0].toString(16).padStart(8, "0");
    const json = data === undefined ? "" : JSON.stringify(data).replace(/[^\x20-\x7e]/g,
      c => "\\u" + c.charCodeAt(0).toString(16).padStart(4,"0"));
    if (json.length > 1600) throw new Error("Settings exceed the device limit.");
    let timer;
    const reply = new Promise((resolve,reject) => {
      this.pending = {id,resolve,reject};
      timer = setTimeout(() => reject(new Error("No setup reply. Check the firmware version and setup screen. Check status before retrying a save.")),6500);
    });
    const bytes = encoder.encode("\nPS1 " + id + " " + password + " " + op + (json ? " " + json : "") + "\n");
    try {
      const [result] = await Promise.all([reply, this.writer.write(bytes)]);
      return result;
    } finally { clearTimeout(timer); this.pending = null; bytes.fill(0); }
  }
  async close() {
    await this.reader?.cancel().catch(() => {});
    await this.reading;
    this.writer?.releaseLock();
    await this.port.close().catch(() => {});
  }
}
