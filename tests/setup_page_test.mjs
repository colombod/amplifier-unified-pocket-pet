import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../main/setup.html',import.meta.url),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes=new Map();
function element(){return {value:'',required:false,disabled:false,textContent:'',hidden:false,selectedOptions:[{dataset:{open:'false'}}],classList:{toggle(name,hidden){this[name]=hidden;},add(name){this[name]=true;}},replaceChildren(){},append(){}};}
const node=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
let status={connected:false,joining:false,key_saved:false,settings_saved:false,message:'Waiting',token:'fixture-token',firmware_version:'0.1.3-test',configure_generation:0};
let postMode='success';
const response=(body,ok=true)=>({ok,status:ok?200:400,json:async()=>body,text:async()=>body});
const context=vm.createContext({document:{getElementById:node,createElement:element},TextEncoder,AbortSignal,Date,setTimeout(){},Option:class{},DOMParser:class{parseFromString(s){return {body:{textContent:s.replace(/<[^>]*>/g,'')}};}},fetch:async(path,opts)=>{
 if(path==='/status')return response(status);
 if(path==='/networks')return response([]);
 if(postMode==='error')return response('<p>Required API key is missing</p>',false);
 if(postMode==='disconnect')throw new TypeError('network interrupted');
 return response({});
}});
vm.runInContext(source,context);await new Promise(setImmediate);await new Promise(setImmediate);
assert.equal(node('firmware').textContent,'Firmware 0.1.3-test');
node('network').value='Home';node('password').value='dummy-password';node('key').value='sk-fixture-not-a-real-api-key';
postMode='error';await vm.runInContext("$('setup').onsubmit({preventDefault(){}})",context);
assert.match(node('notice').textContent,/Required API key is missing/);
assert.equal(node('password').value,'dummy-password');assert.ok(node('key').value);
postMode='disconnect';await vm.runInContext("$('setup').onsubmit({preventDefault(){}})",context);
assert.match(node('notice').textContent,/may still be connecting/);assert.equal(node('connect').disabled,true);assert.ok(node('key').value);
status={...status,connected:true,key_saved:true,settings_saved:false,configure_generation:1,message:'Saving failed'};
await vm.runInContext('poll()',context);assert.ok(node('key').value);assert.equal(node('done').classList.hidden,true);
status={...status,settings_saved:true,message:'Saved'};
await vm.runInContext('poll()',context);assert.equal(node('key').value,'');assert.equal(node('password').value,'');assert.equal(node('done').classList.hidden,false);
// Editing an already configured device must survive repeated saved-status polls.
node('password').value='new-dummy-password';node('key').value='sk-fixture-replacement-not-real';
await vm.runInContext("$('password').oninput()",context);
await vm.runInContext('poll()',context);await vm.runInContext('poll()',context);
assert.equal(node('password').value,'new-dummy-password');assert.ok(node('key').value);assert.equal(node('done').classList.hidden,true);
// Even an uncertain POST must not mistake the previous save for this submission.
postMode='disconnect';await vm.runInContext("$('setup').onsubmit({preventDefault(){}})",context);
await vm.runInContext('poll()',context);assert.equal(node('password').value,'new-dummy-password');
status={...status,configure_generation:2,settings_saved:true};await vm.runInContext('poll()',context);
assert.equal(node('password').value,'');assert.equal(node('key').value,'');
console.log('PASS: detailed rejection, lost ACK, retained entries, unsaved-state gating and confirmed-save generation and edit retention');
