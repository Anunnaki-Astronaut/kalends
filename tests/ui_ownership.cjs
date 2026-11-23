// SDK hook seam, not a rendered Desktop test. Settings owner deliberately differs
// from global profile. Every scenario uses synthetic backend responses.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
async function scenario(kind){
 let owner='A',epoch=1,cursor=0,state=[],effects=[],calls=[],timers=[],cleanup=[],dead=false,lateWrites=0;
 let hold=null,reject=null,pending=null;
 let migration='Legacy Read and write allows only Read and Create. Save to migrate.';
 const ctx={rest:async(path,opts)=>{
   const at=owner; calls.push({owner:at,path,body:opts?.body});
   if(pending===path){pending=null;await new Promise((a,b)=>{hold=a;reject=b})}
   if(path==='/status')return {owner_id:at,connected:true,permissions:{},migration_notice:migration};
   if(path==='/discover')return {owner_id:at,calendars:[{id:'https://caldav.icloud.com/shared/',name:'Private '+at,access:'read-write',capabilities:{read:true,create:true,edit:false,delete:false}}],migration_notice:migration};
   if(opts.body.owner_id!==at)throw Error('owner_changed');
   if(path==='/permissions')migration=null;
   return {owner_id:at,migration_notice:migration,permissions:opts.body.permissions,credential_removed:false,message:'Credential removal could not be confirmed.'};
 }};
  const box={Button:'Button',Codicon:'Icon',ErrorState:'Error',GlyphSpinner:'Spinner',gatewayActivationEpoch:()=>epoch,host:{state:{profile:{get:()=> 'GLOBAL'}},notify:()=>{if(dead)lateWrites++}},setInterval:f=>(timers.push(f),timers.length),clearInterval:()=>{},useCallback:f=>f,useEffect:f=>{const i=cursor++;if(!state[i]){state[i]=true;effects.push(f)}},useRef:v=>{const i=cursor++;return state[i]??(state[i]={current:v})},useState:v=>{const i=cursor++;if(!(i in state))state[i]=typeof v==='function'?v():v;return [state[i],x=>{if(dead)lateWrites++;state[i]=typeof x==='function'?x(state[i]):x}]},jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
 const source=fs.readFileSync('desktop/plugin.js','utf8').replace(/^import .*\n/gm,'').replace('export default {','globalThis.plugin = {');vm.createContext(box);vm.runInContext(source,box);
  let renderPage;box.plugin.register({registerSettingsPage:p=>renderPage=p.render});
  const wrapper=renderPage();
  const frame=wrapper.type({...wrapper.props,ctx});
  const inner=frame.props.children.find(n=>typeof n?.type==='function');
  state=[];effects=[];cursor=0;
  const component=inner.type;
  const render=()=>{cursor=0;return component({...inner.props,ctx})};
 const buttons=(n,out=[])=>{if(!n||typeof n!=='object')return out;if(n.type==='Button')out.push(n);for(const x of [n.props?.children].flat(Infinity))buttons(x,out);return out};
 const inputs=(n,out=[])=>{if(!n||typeof n!=='object')return out;if(n.type==='input')out.push(n);for(const x of [n.props?.children].flat(Infinity))inputs(x,out);return out};
 const control=cap=>inputs(render()).find(n=>n.props['aria-label']===cap+' for Private A');
 const toggle=(cap,checked)=>control(cap).props.onChange({target:{checked}});
 const click=label=>buttons(render()).find(n=>n.props.children===label)?.props.onClick();
 const settle=()=>new Promise(r=>setImmediate(r));
 render();for(const f of effects)cleanup.push(f());await settle();
 if(kind.startsWith('pending')||kind==='unmount')pending='/discover';
 const discovery=click('Test and discover calendars');await settle();
 if(kind==='unmount'){dead=true;cleanup.forEach(f=>f?.());hold();await discovery;assert.equal(lateWrites,0);return;}
 if(kind.startsWith('pending')){owner='B';if(kind==='pending-reject')reject(Error('sensitive backend rejection'));else hold();await discovery;await settle();assert(!JSON.stringify(render()).includes('Private A'));return;}
 await discovery;
 const rowControls=()=>inputs(render()).filter(n=>/^(Read|Create|Edit|Delete) for /.test(n.props['aria-label']||''));
  const bulkControls=()=>{
    const walk=(n)=>!n||typeof n!=='object'?[]:[n,...[n.props?.children].flat(Infinity).flatMap(walk)];
    return walk(render()).filter(n=>n.type==='button'&&/^(Select|Clear) all (Read|Create|Edit|Delete)$/.test(n.props['aria-label']||''));
  };
 assert.equal(rowControls().length,4,'four independent capability controls');
  assert.equal(bulkControls().length,8,'two explicit actions in each of four columns');
 assert.equal(control('Read').props.checked,true);
 assert.equal(control('Create').props.checked,true);
 assert.equal(control('Edit').props.checked,false);
 assert.equal(control('Delete').props.checked,false);
 assert(JSON.stringify(render()).includes(migration),'migration explanation is visible');
 toggle('Edit',true);
 assert.equal(control('Delete').props.checked,false,'Edit does not grant Delete');
 assert(JSON.stringify(render()).includes('Unsaved changes.'));
 assert.equal(calls.filter(c=>c.path==='/permissions').length,0,'editing never auto-saves');
 if(kind==='controls'){
   toggle('Read',false);
   for(const cap of ['Read','Create','Edit','Delete'])assert.equal(control(cap).props.checked,false);
   for(const cap of ['Create','Edit','Delete'])assert.equal(control(cap).props.disabled,true);
   toggle('Read',true);toggle('Delete',true);
   assert.equal(control('Create').props.checked,false,'Read does not restore writes');
   await click('Save calendar access');
   const writes=calls.filter(c=>c.path==='/permissions');assert.equal(writes.length,1);
   assert.deepEqual(JSON.parse(JSON.stringify(writes[0].body.permissions)),{'https://caldav.icloud.com/shared/':{read:true,create:false,edit:false,delete:true}});
   assert(JSON.stringify(render()).includes('No unsaved changes.'));
   assert(!JSON.stringify(render()).includes('Legacy Read and write'));
   return;
 }
 const staleSave=buttons(render()).find(n=>n.props.children==='Save calendar access')?.props.onClick;
 if(kind==='epoch'){epoch++;}else owner='B';
 if(kind==='aba'||kind==='stale-aba'){await timers[0]();owner='A';await timers[0]();}
 if(kind==='poll'||kind==='stale-handler'){await timers[0]();await timers[0]();}
 if(kind==='stale-handler'||kind==='stale-aba')await staleSave();else await click('Save calendar access');
 const writes=calls.filter(c=>c.path==='/permissions');assert.equal(writes.length,0,kind+' must not submit stale draft');
 if(kind==='poll'||kind==='aba')assert(!JSON.stringify(render()).includes('Private A'));
}
(async()=>{for(const kind of ['controls','settled','stale-handler','stale-aba','epoch','poll','aba','pending-resolve','pending-reject','unmount'])await scenario(kind);console.log('Capability controls and 9 owner scenarios passed (SDK hook harness).')})().catch(e=>{console.error(e);process.exitCode=1});
