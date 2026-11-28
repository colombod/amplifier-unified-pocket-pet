import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {createServer} from "node:http";
import {build} from "esbuild";
import {chromium} from "playwright-core";
import {md5} from "@noble/hashes/legacy";
import {bytesToHex} from "@noble/hashes/utils";
import {fileURLToPath} from "node:url";
process.chdir(fileURLToPath(new URL(".",import.meta.url)));
const testBundle = (await build({stdin:{contents:'export * from "./flash.js";',resolveDir:process.cwd()},
  bundle:true,format:"esm",write:false})).outputFiles[0].contents;
const allowed = new Set(["app.js","index.html","pet.png","style.css","firmware.bin","notices.txt"]);
const server = createServer(async (req,res)=>{
  const name=req.url.slice(1)||"index.html";
  if (name==="tests.js") {res.setHeader("Content-Type","text/javascript"); return res.end(testBundle);}
  if (!allowed.has(name)) {res.writeHead(404);return res.end();}
  res.setHeader("Content-Type",({"js":"text/javascript","html":"text/html","css":"text/css","png":"image/png"})[name.split(".").pop()]||"application/octet-stream");
  res.end(await readFile("dist/"+name));
});
await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
let browser;
try {
  browser=await chromium.launch({executablePath:process.env.CHROME_PATH||"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",headless:true});
  const page=await browser.newPage({viewport:{width:390,height:844}});
  const errors=[];page.on("pageerror",e=>errors.push(e.message));
  await page.addInitScript(()=>{
    let controller;
    window.framesReceived=[];
    const port={async open(){
      this.readable=new ReadableStream({start(c){controller=c;}});
      this.writable=new WritableStream({write(data){
        const m=new TextDecoder().decode(data).trim().match(/^PS1 (\w+) (\S+) (\w+)(?: (.*))?$/);
        if(!m)return;
        window.framesReceived.push({op:m[3],body:m[4]?JSON.parse(m[4]):null});
        const ok=m[2]==="test-pet" && !window.dropReply;
        const msg=m[3]==="status"?{firmware_version:"setup-test",ssid:"pet Wi-Fi",message:"Ready"}:
           m[4]?.includes("unified_target")?{unified_target_saved:true}:{};
        if(!window.dropReply)controller.enqueue(new TextEncoder().encode("\nPS1 "+m[1]+(ok?" OK "+JSON.stringify(msg):" ERR Pairing refused")+"\n"));
      }});
    },async close(){}};
    Object.defineProperty(navigator,"serial",{value:{requestPort:async()=>port,addEventListener(){}}});
  });
  await page.goto("http://127.0.0.1:"+server.address().port+"/");
  assert.equal(await page.locator("#phone").isVisible(),true);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.getByRole("button",{name:"USB settings",exact:true}).click();
  await page.fill("#pair","wrong-pin");await page.click("#connect");await page.waitForFunction(()=>document.querySelector("#usbStatus").textContent.includes("Pairing refused"));
  await page.fill("#pair","test-pet");await page.click("#connect");await page.waitForSelector("#settings:enabled");
  assert.equal(await page.inputValue('[name=ssid]'),"pet Wi-Fi");
  await page.fill('[name=ssid]',"Café");await page.click("#wifi button");await page.waitForFunction(()=>document.querySelector("#usbStatus").textContent.includes("join requested"));
  assert.equal((await page.evaluate(()=>framesReceived.at(-1))).body.ssid,"Café");
  await page.getByText("Unified peer and Tailscale",{exact:true}).click();
  await page.fill("#peer [name=host]","example.test"); await page.click("#peer button");
  await page.waitForFunction(()=>document.querySelector("#usbStatus").textContent.includes("peer saved"));
  await page.evaluate(()=>{window.dropReply=true;});await page.click("#peer button");
  await page.waitForFunction(()=>document.querySelector("#usbStatus").textContent.includes("before retrying"));
  assert.equal(await page.evaluate(()=>framesReceived.filter(f=>f.body?.action==="unified_target").length),2);
  await page.click("#disconnect");assert.equal(await page.inputValue("#pair"),"");
  await page.getByRole("button",{name:"Firmware",exact:true}).click();assert.equal(await page.isDisabled("#flash"),true);
  // The simulated flash contains only public release bytes, a public partition table, and generated canaries.
  const original=await readFile(process.env.PET_PRIOR_APP);
  const table=await readFile(process.env.PET_PARTITION_TABLE||"../apps/unified-pet/build/partition_table/partition-table.bin");
  const flash=new Uint8Array(0x1000000).fill(0xa5);flash.set(table,0x8000);flash.set(original,0x10000);
  let writes=0,corrupt=false;
  await page.exposeFunction("flashRead",(a,n)=>Buffer.from(flash.subarray(a,a+n)).toString("base64"));
  await page.exposeFunction("flashMd5",(a,n)=>bytesToHex(md5(flash.subarray(a,a+n))));
  await page.exposeFunction("program",async(a,n)=>{assert.equal(a,65536);const b=await readFile("dist/firmware.bin");assert.equal(n,b.length);flash.set(b,a);writes++;});
  await page.exposeFunction("alter",()=>{corrupt=!corrupt;flash[0x8000]^=1;});
  const result=await page.evaluate(async()=>{
    const {prepareUpdate,writeUpdate,checkImage}=await import("./tests.js");
    const raw=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));const hex=s=>Uint8Array.from(s.match(/../g),v=>parseInt(v,16));
    let last, file=new Blob(), lost=false;
    const handle={getFile:async()=>file,createWritable:async()=>({write:async data=>{file=new Blob([data]);},close:async()=>{},abort:async()=>{}})};
    const loader={chip:{readMac:async()=>"test-board"},transport:{read:async()=>hex(await flashMd5(...last))},
      readFlash:async(a,n)=>{if(lost)throw Error("USB interrupted");last=[a,n];return raw(await flashRead(a,n));},
      flashMd5sum:flashMd5,writeFlash:async opts=>{if(opts.eraseAll||opts.fileArray.length!==1)throw Error("unsafe write");await program(opts.fileArray[0].address,opts.fileArray[0].data.length);}};
    const device={loader,revision:2,mac:"test-board"};
    const expectFailure=async f=>{try{await f();return false;}catch{return true;}};
    const bad=await expectFailure(()=>checkImage(new Uint8Array(256),2,true));
    lost=true;const interrupted=await expectFailure(()=>prepareUpdate(device,handle,()=>{}));lost=false;
    await alter();const tableRefused=await expectFailure(()=>prepareUpdate(device,handle,()=>{}));await alter();
    const broken={...handle,getFile:async()=>new Blob()};
    const diskRefused=await expectFailure(()=>prepareUpdate(device,broken,()=>{}));
    file=new Blob();
    const plan=await prepareUpdate(device,handle,()=>{});await writeUpdate(device,plan,()=>{});
    const replayRefused=await expectFailure(()=>writeUpdate(device,plan,()=>{}));
    return [bad,interrupted,tableRefused,diskRefused,file.size,plan.installed.version,replayRefused];
  });
  assert.deepEqual(result,[true,true,true,true,16777216,"0.3.21-amplifier",true]);assert.equal(writes,1);assert.equal(corrupt,false);
  assert.deepEqual(errors,[]);console.log("PASS phone navigation, mobile layout, USB pairing, Unicode settings, saved peer, lost acknowledgement, signed-off backup and app-only update checks with synthetic flash.");
} finally {await browser?.close();server.close();}
