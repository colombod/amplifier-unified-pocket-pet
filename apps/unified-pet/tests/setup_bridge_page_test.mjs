import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../main/setup.html',import.meta.url),'utf8');
const nodes=new Map();
const node=id=>{
 if(!nodes.has(id))nodes.set(id,{value:'',checked:false,disabled:false,required:false,selectedOptions:[{dataset:{}}],classList:{toggle(n,v){this[n]=v;},add(n){this[n]=true;}},replaceChildren(){},append(){}});
 return nodes.get(id);
};
let status={token:'DUMMY',bridge_valid:true,bridge_enabled:true,bridge_token_saved:true,bridge_ip:'192.168.1.50',bridge_port:9443,bridge_fqdn:'pet.example.net',key_saved:false,connected:true,settings_saved:true};
let posts=[],mode='ok',resolvePost;
const reply=(v,ok=true)=>({ok,status:ok?200:500,json:async()=>v,text:async()=>String(v)});
const context=vm.createContext({document:{getElementById:node,createElement:()=>node('option')},TextEncoder,AbortSignal,Date,setTimeout(){},Option:class{},DOMParser:class{parseFromString(s){return{body:{textContent:s}};}},fetch:async(path,opts)=>{
 if(path==='/status')return reply(status);
 if(path==='/networks')return reply([]);
 if(path==='/finish'){posts.push({path});return reply({});}
 posts.push(JSON.parse(opts.body));
 if(mode==='deferred')return new Promise(r=>resolvePost=r);
 if(mode==='lost')throw new Error('lost');
 if(mode==='error')return reply('Bridge save refused',false);
 return reply({bridge_saved:true,bridge_token_saved:!posts.at(-1).clear_token});
}});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
await new Promise(setImmediate);await new Promise(setImmediate);
assert.equal(node('bridge-ip').value,'192.168.1.50');
assert.equal(node('bridge-fqdn').value,'pet.example.net');
assert.equal(node('bridge-enabled').checked,true);
assert.equal(node('bridge-token').value,'');
assert.equal(node('key').required,false);
assert.match(html,/<input id="bridge-token"[^>]*type="password"/);
assert.doesNotMatch(html,/<input id="bridge-token"[^>]*value=/);
node('bridge-ip').value='10.0.0.2';node('bridge-ip').oninput();
await vm.runInContext('poll()',context);
assert.equal(node('bridge-ip').value,'10.0.0.2');
assert.equal(node('done').classList.hidden,true);
mode='deferred';
const save=vm.runInContext("$('bridge-setup').onsubmit({preventDefault(){}})",context);
assert.deepEqual(posts.at(-1),{action:'bridge_gateway',enabled:true,ip:'10.0.0.2',port:9443,fqdn:'pet.example.net',token:'',clear_token:false});
const n=posts.length;
await vm.runInContext("$('bridge-setup').onsubmit({preventDefault(){}})",context);
assert.equal(posts.length,n);
node('bridge-ip').value='10.0.0.3';node('bridge-ip').oninput();
node('bridge-token').value='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';node('bridge-token').oninput();
resolvePost(reply({bridge_saved:true,bridge_token_saved:true}));await save;
assert.equal(node('bridge-ip').value,'10.0.0.3');
assert.equal(node('bridge-token').value,'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
assert.match(node('bridge-notice').textContent,/newer edit.*not saved/i);
for(const m of ['error','lost']){
 assert.equal(status.bridge_token_saved,true); // an earlier, possibly different token exists
 mode=m;await vm.runInContext("$('bridge-setup').onsubmit({preventDefault(){}})",context);
 assert.equal(posts.at(-1).token,'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
 const posted=posts.length;
 assert.equal(node('bridge-token').value,'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
 assert.match(node('bridge-notice').textContent,/refused|not confirmed/i);
 await vm.runInContext('poll()',context);
 assert.equal(posts.length,posted); // saved=true cannot acknowledge this replacement
 assert.equal(node('bridge-token').value,'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
 assert.equal(node('done').classList.hidden,true);
}
mode='ok';
await vm.runInContext("$('bridge-setup').onsubmit({preventDefault(){}})",context);
assert.equal(node('bridge-token').value,'');
assert.equal(node('password').value,'');assert.equal(node('key').value,'');
const before=posts.length;
node('bridge-token').value='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB';node('bridge-token').oninput();
await vm.runInContext("$('bridge-setup').onsubmit({preventDefault(){}})",context);
assert.equal(posts.length,before);
node('bridge-token').value='';node('bridge-clear-token').checked=true;node('bridge-clear-token').onchange();
await vm.runInContext("$('bridge-setup').onsubmit({preventDefault(){}})",context);
assert.equal(posts.length,before); // cannot enable without a token
node('bridge-enabled').checked=false;node('bridge-enabled').onchange();
await vm.runInContext("$('bridge-setup').onsubmit({preventDefault(){}})",context);
assert.equal(posts.at(-1).clear_token,true);
assert.equal(posts.at(-1).enabled,false);
assert.equal(node('bridge-clear-token').checked,false);
status={...status,bridge_enabled:false,bridge_token_saved:false};
await vm.runInContext('poll()',context);
assert.equal(node('key').required,true);
assert.equal(node('bridge-token').value,'');
console.log('PASS: production bridge page, nonsecret prefill, no secret echo, independent saves, stale-edit/lost-ACK preservation, readiness and explicit clear');
