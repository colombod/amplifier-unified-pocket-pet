import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../main/setup.html',import.meta.url),'utf8');
const nodes=new Map();
const node=id=>{
 if(!nodes.has(id))nodes.set(id,{value:'',disabled:false,selectedOptions:[{dataset:{}}],
  classList:{toggle(n,v){this[n]=v;},add(n){this[n]=true;}},replaceChildren(){},append(){}});
 return nodes.get(id);
};
let status={token:'DUMMY',unified_host:'node.example.ts.net',unified_port:8941},post,resolvePost;
let mode='ok';
const reply=(v,ok=true)=>({ok,status:ok?200:500,json:async()=>v,text:async()=>String(v)});
const context=vm.createContext({document:{getElementById:node,createElement:()=>node('option')},
 TextEncoder,AbortSignal,Date,setTimeout(){},Option:class{},DOMParser:class{parseFromString(s){return {body:{textContent:s}};}},
 fetch:async(path,options)=>{
  if(path==='/status')return reply(status);
  if(path==='/networks')return reply([]);
  post=JSON.parse(options.body);
  if(mode==='deferred')return new Promise(r=>resolvePost=r);
  if(mode==='error')return reply('Write failed',false);
  if(mode==='lost')throw new Error('lost');
  return reply({unified_target_saved:true});
 }});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
await new Promise(setImmediate);await new Promise(setImmediate);
assert.equal(node('unified-host').value,'node.example.ts.net');
assert.equal(node('unified-port').value,8941);
node('unified-host').value='new.example.ts.net';
vm.runInContext("$('unified-host').oninput()",context);
await vm.runInContext('poll()',context);
assert.equal(node('unified-host').value,'new.example.ts.net');
mode='deferred';
const saving=vm.runInContext("$('unified-setup').onsubmit({preventDefault(){}})",context);
assert.deepEqual(post,{action:'unified_target',host:'new.example.ts.net',port:8941});
node('unified-host').value='later.example.ts.net';
vm.runInContext("$('unified-host').oninput()",context);
resolvePost(reply({unified_target_saved:true}));await saving;
assert.equal(node('unified-host').value,'later.example.ts.net');
assert.match(node('unified-notice').textContent,/newer edit.*not saved/i);
for(const m of ['error','lost']) {
 mode=m;await vm.runInContext("$('unified-setup').onsubmit({preventDefault(){}})",context);
 assert.equal(node('unified-host').value,'later.example.ts.net');
 assert.match(node('unified-notice').textContent,/failed|not confirmed/i);
}
mode='ok';node('unified-host').value='';
vm.runInContext("$('unified-host').oninput()",context);
await vm.runInContext("$('unified-setup').onsubmit({preventDefault(){}})",context);
assert.equal(post.host,'');
assert.equal(node('key').value,'');
console.log('PASS production phone Unified prefill, independent save, late-edit retention, storage failure/lost ACK and blank target');