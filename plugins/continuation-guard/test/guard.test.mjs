import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {evaluateWork,assessAttempt,eventSessionId,safePermissionCount,sessionMatches,fingerprint} from '../core.mjs';
import {startGuard} from '../runtime.mjs';

const baseScope = () => ({schema:'openworkflow-progress-scope-v1',tasks:[{id:'a',status:'BUILDING'}]});
const status='Task: a\nStatus: in_progress\nHuman gate: none\nPrimary phase: build\nNext execution: runnable';
const config={enabled:true,agent:'Orchestrator',pollMs:20000,minIdleMs:60000,cooldownMs:90000,maxAttemptsWithoutProgress:2,maxIdleAgeHours:8,sessionLeaseHours:36,dryRun:false};

test('scope: no source document requirement, explicit active work only',()=>{
  assert.equal(evaluateWork(baseScope(),status).ok,true);
  assert.equal(evaluateWork(baseScope(),status.replace('in_progress','done')).ok,false);
  assert.equal(evaluateWork(baseScope(),status.replace('Human gate: none','Human gate: permission')).ok,false);
  assert.equal(evaluateWork({schema:baseScope().schema,tasks:[{id:'a',status:'VERIFIED'}]},status).ok,false);
  assert.equal(evaluateWork({schema:baseScope().schema,tasks:[{id:'a',status:'GIBBERISH'}]},status).ok,false);
  assert.equal(evaluateWork(null,status).ok,false);
});
test('event shapes and permission failures fail closed',()=>{
  assert.equal(eventSessionId({type:'session.execution.succeeded',properties:{sessionID:'ses_1'}}),'ses_1');
  assert.equal(safePermissionCount([]),0);
  assert.equal(safePermissionCount({data:[1]}),1);
  assert.equal(safePermissionCount({unexpected:true}),null);
  assert.equal(sessionMatches({id:'ses_1',agent:'Orchestrator',location:{directory:'C:\\Go\\x'}},'ses_1','c:/go/x','Orchestrator'),true);
  assert.equal(sessionMatches({id:'ses_1',parentID:'ses_parent',agent:'Orchestrator'},'ses_1','/x','Orchestrator'),false);
});
test('attempt limit, cooldown, idle and completion dedupe',()=>{
  const now=200000;
  const c={minIdleMs:60000,cooldownMs:90000,maxIdleAgeMs:999999,maxAttemptsWithoutProgress:4};
  const state={armedSessionId:'ses_abc',lastStartedAt:1,lastSucceededAt:100000,signature:'s',attempts:0,lastAttemptAt:0,lastAttemptForEnd:0};
  assert.equal(assessAttempt(state,'s',c,now).ok,true);
  assert.equal(assessAttempt({...state,lastAttemptForEnd:100000},'s',c,now).ok,false);
  assert.equal(assessAttempt({...state,attempts:4},'s',c,now).reason,'no-progress-cap');
  assert.equal(assessAttempt({...state,lastAttemptAt:now-5},'s',c,now).reason,'cooldown');
  assert.equal(assessAttempt({...state,lastSucceededAt:now-100},'s',c,now).reason,'idle-grace');
  assert.equal(assessAttempt({...state,suspended:true},'s',c,now).ok,false);
});

async function harness({statusText=status,scope=baseScope(),permissions=[],startTime=100000,
  waitFn=async()=>{},promptFn=null,listFn=async()=>({items:[],cursor:{next:null}})}={}){
  const repo=await fs.mkdtemp(path.join(os.tmpdir(),'guard-v4-test-'));
  const sub=path.join(repo,'plans');await fs.mkdir(sub);
  await fs.writeFile(path.join(sub,'workflow_state.md'),statusText);
  await fs.writeFile(path.join(sub,'progress_scope.json'),JSON.stringify(scope));
  const pluginDir=path.join(repo,'guard');await fs.mkdir(pluginDir);
  await fs.writeFile(path.join(pluginDir,'config.json'),JSON.stringify(config));
  let now=startTime;
  const hooks={};const store=new Map();const admissions=[];const logs=[];
  const info={id:'ses_root',agent:'Orchestrator',location:{directory:repo}};
  const ctx={location:{directory:repo},storage:{get:async k=>store.get(k),set:async(k,v)=>{store.set(k,JSON.parse(JSON.stringify(v)))},remove:async k=>store.delete(k)},
    session:{get:async()=>info,wait:waitFn,list:listFn,prompt:async v=>{
      admissions.push(v);
      if(!/^msg_[a-f0-9]{24}$/.test(v.id))throw new Error('Expected a string starting with "msg_" at ["id"]');
      if(hooks.prompt)await hooks.prompt({sessionID:v.sessionID,prompt:{text:v.text}});
      if(promptFn)await promptFn(v);return {id:v.id};},
      hook:async(name,fn)=>{hooks[name]=fn;return {dispose:async()=>{delete hooks[name]}}}},
    permission:{list:async()=>permissions},event:{subscribe:async function*(){}},};
  const guard=await startGuard(ctx,{pluginDir,logger:async(e,v)=>{logs.push({e,...v})},now:()=>now,setIntervalFn:()=>0,clearIntervalFn:()=>{}});
  const dispose=await guard.init();
  return {repo,guard,ctx,hooks,logs,admissions,dispose, setTime:t=>{now=t},
    markRunning:async()=>{
      await hooks.context({sessionID:'ses_root',agent:'Orchestrator'});
      await guard.onEvent({type:'session.execution.started',properties:{sessionID:'ses_root'}});
    },
    ended:async()=>guard.onEvent({type:'session.execution.succeeded',properties:{sessionID:'ses_root'}}),
    cleanup:async()=>{await dispose();await fs.rm(repo,{recursive:true,force:true})}
  };
}

test('no trigger phrases: root model context arms, actual success resumes',async()=>{
 const h=await harness();try{
  await h.hooks.context({sessionID:'ses_root',agent:'Builder'});
  assert.equal(h.guard.getState().armedSessionId,null);
  await h.markRunning();assert.equal(h.guard.getState().armedSessionId,'ses_root');
  h.setTime(120000);await h.ended();
  h.setTime(190001);await h.guard.check();
  assert.equal(h.admissions.length,1);assert.equal(h.admissions[0].delivery,'queue');
  assert.equal(h.admissions[0].resume,true);
  assert.match(h.admissions[0].id,/^msg_[a-f0-9]{24}$/);
  await h.guard.check();assert.equal(h.admissions.length,1);
 }finally{await h.cleanup()}
});
test('old task files and BUILDING row alone do not arm',async()=>{
 const h=await harness();try{
  await fs.writeFile(path.join(h.repo,'plans','current_task.md'),'old complete task');
  await fs.writeFile(path.join(h.repo,'plans','runtime_task.md'),'old task');
  h.setTime(999999);await h.guard.check();assert.equal(h.admissions.length,0);
  assert.equal(h.guard.getState().armedSessionId,null);
 }finally{await h.cleanup()}
});
test('manual interruption prevents automatic resume, new owner prompt re-arms structurally',async()=>{
 const h=await harness();try{
  await h.markRunning();await h.guard.onEvent({type:'session.execution.interrupted',properties:{sessionID:'ses_root',reason:'user'}});
  h.setTime(200000);await h.guard.check();assert.equal(h.admissions.length,0);
  await h.hooks.context({sessionID:'ses_root',agent:'Orchestrator'});
  assert.equal(h.guard.getState().suspended,true);
  await h.hooks.prompt({sessionID:'ses_root',prompt:{text:'любая формулировка, не ключевое слово'}});
  await h.hooks.context({sessionID:'ses_root',agent:'Orchestrator'});
  assert.equal(h.guard.getState().suspended,false);
 }finally{await h.cleanup()}
});
test('unresolved permissions block resume',async()=>{
 const h=await harness({permissions:[{id:'p1'}]});try{
  await h.markRunning();h.setTime(120000);await h.ended();h.setTime(195000);
  await h.guard.check();assert.equal(h.admissions.length,0);
 }finally{await h.cleanup()}
});
test('busy session barrier stops resume',async()=>{
 const h=await harness({waitFn:()=>Promise.reject(new Error('timeout:wait'))});try{
  await h.markRunning();h.setTime(120000);await h.ended();h.setTime(195000);
  await h.guard.check();assert.equal(h.admissions.length,0);
 }finally{await h.cleanup()}
});
test('task done blocks resume and clears lease',async()=>{
 const h=await harness();try{
  await h.markRunning();await fs.writeFile(path.join(h.repo,'plans','workflow_state.md'),'Task: a\nStatus: done\nHuman gate: none');
  h.setTime(120000);await h.ended();h.setTime(195000);await h.guard.check();
  assert.equal(h.admissions.length,0);assert.equal(h.guard.getState().armedSessionId,null);
 }finally{await h.cleanup()}
});
test('failure does not trigger recovery',async()=>{
 const h=await harness();try{
  await h.markRunning();h.setTime(120000);
  await h.guard.onEvent({type:'session.execution.failed',properties:{sessionID:'ses_root',error:{type:'provider.auth'}}});
  h.setTime(195000);await h.guard.check();assert.equal(h.admissions.length,0);
  assert.equal(h.guard.getState().suspended,true);
 }finally{await h.cleanup()}
});
test('background child prevents parent wake until child completes',async()=>{
 let childBusy=true;
 const h=await harness({waitFn:async({sessionID})=>{if(sessionID==='ses_child'&&childBusy)throw Error('timeout:child-wait')}});try{
  const get=h.ctx.session.get;
  h.ctx.session.get=async ({sessionID})=>sessionID==='ses_child'?{id:'ses_child',parentID:'ses_root'}:get({sessionID});
  await h.markRunning();await h.guard.onEvent({type:'session.execution.started',properties:{sessionID:'ses_child'}});
  h.setTime(120000);await h.ended();h.setTime(195000);await h.guard.check();
  assert.equal(h.admissions.length,0);
  childBusy=false;await h.guard.onEvent({type:'session.execution.succeeded',properties:{sessionID:'ses_child'}});
  await h.guard.check();assert.equal(h.admissions.length,1);
 }finally{await h.cleanup()}
});
test('v4.3: unobserved active Builder found via session inventory prevents wake',async()=>{
  let busy=true;
  const h=await harness({listFn:async()=>({items:[{id:'ses_builder',parentID:'ses_root'}],cursor:{next:null}}),
    waitFn:async({sessionID})=>{if(sessionID==='ses_builder'&&busy)throw new Error('busy') }});
  try{
    await h.markRunning();h.setTime(120000);await h.ended();h.setTime(195000);
    await h.guard.check();assert.equal(h.admissions.length,0);
    assert.ok(h.logs.some(x=>x.e==='skip-child-barrier'));
    busy=false;await h.guard.check();assert.equal(h.admissions.length,1);
  }finally{await h.cleanup()}
});
test('v4.3: session listing unavailable cannot assert absence of active children',async()=>{
  const h=await harness();try{
    delete h.ctx.session.list;
    await h.markRunning();h.setTime(120000);await h.ended();h.setTime(195000);
    await h.guard.check();assert.equal(h.admissions.length,0);
    assert.ok(h.logs.some(x=>x.reason==='session-list-unavailable'));
  }finally{await h.cleanup()}
});
test('v4.3: inventory failure, bad shape and partial pagination fail closed',async()=>{
  for (const listFn of [async()=>{throw Error('API unavailable')},
    async()=>({data:{unrecognized:true}}),
    async()=>({items:[{id:'ses_unknown'}],cursor:{next:null}}),
    async()=>({items:[],cursor:{next:'a'}})]){
    const h=await harness({listFn});try{
      await h.markRunning();h.setTime(120000);await h.ended();h.setTime(195000);
      await h.guard.check();assert.equal(h.admissions.length,0);
    }finally{await h.cleanup()}
  }
});
test('v4.3: a child launched after first inventory is caught before admission',async()=>{
  let inventories=0;
  const h=await harness({listFn:async()=>({
    items:++inventories<2?[]:[{id:'ses_late_builder',parentID:'ses_root'}],
    cursor:{next:null}
  }),waitFn:async({sessionID})=>{
    if(sessionID==='ses_late_builder')throw new Error('busy')
  }});try{
    await h.markRunning();h.setTime(120000);await h.ended();h.setTime(195000);
    await h.guard.check();assert.equal(h.admissions.length,0);
    assert.ok(h.logs.some(x=>x.e==='skip-child-barrier'));
  }finally{await h.cleanup()}
});
test('v4.3: completed child on next inventory page permits resume',async()=>{
  const h=await harness({listFn:async p=>p.cursor?
    {items:[{id:'ses_child',parentID:'ses_root'}],cursor:{next:null}}:
    {items:[],cursor:{next:'after-first'}}});try{
    await h.markRunning();h.setTime(120000);await h.ended();h.setTime(195000);
    await h.guard.check();assert.equal(h.admissions.length,1);
  }finally{await h.cleanup()}
});
test('two unacknowledged attempts reuse identical idempotency ID',async()=>{
 let tries=0;
 const h=await harness({promptFn:async()=>{if(++tries===1)throw new Error('connection lost')}});try{
  await h.markRunning();h.setTime(120000);await h.ended();h.setTime(200000);await h.guard.check();
  assert.equal(tries,2);assert.equal(h.admissions.length,2);
  assert.equal(h.admissions[0].id,h.admissions[1].id);
 }finally{await h.cleanup()}
});
test('restored session never resumes from stale lease without new execution',async()=>{
 const h=await harness();try{
  await h.markRunning();h.setTime(120000);await h.ended();
  await h.dispose();
  const guard2=await startGuard(h.ctx,{pluginDir:path.join(h.repo,'guard'),
   logger:async()=>{},now:()=>200000,setIntervalFn:()=>0,clearIntervalFn:()=>{}});
  const dispose2=await guard2.init();try{
    await guard2.check();assert.equal(h.admissions.length,0);
    assert.equal(guard2.getState().lastSucceededAt,0);
  }finally{await dispose2()}
 }finally{await fs.rm(h.repo,{recursive:true,force:true})}
});
test('queued external input suppresses auto-resume even if its words are unknown',async()=>{
 const h=await harness();try{
  await h.markRunning();h.setTime(120000);await h.ended();
  await h.hooks.prompt({sessionID:'ses_root',prompt:{text:'Сколько осталось работы?'}});
  h.setTime(200000);await h.guard.check();assert.equal(h.admissions.length,0);
 }finally{await h.cleanup()}
});
test('a true human gate is not bypassed after an active run',async()=>{
 const h=await harness();try{
  await h.markRunning();h.setTime(120000);await h.ended();
  await fs.writeFile(path.join(h.repo,'plans','workflow_state.md'),status.replace('Human gate: none','Human gate: Requires decision'));
  h.setTime(200000);await h.guard.check();assert.equal(h.admissions.length,0);
  assert.equal(h.guard.getState().armedSessionId,null);
 }finally{await h.cleanup()}
});
test('an unrelated fresh session cannot activate an old checkpoint',async()=>{
 const h=await harness();try{
  const old=new Date(10000);
  await fs.utimes(path.join(h.repo,'plans','workflow_state.md'),old,old);
  h.ctx.session.get=async()=>({id:'ses_root',agent:'Orchestrator',location:{directory:h.repo},time:{created:300000}});
  h.setTime(310000);
  await h.hooks.context({sessionID:'ses_root',agent:'Orchestrator'});
  assert.equal(h.guard.getState().armedSessionId,null);
  const fresh=new Date(315000);
  await fs.utimes(path.join(h.repo,'plans','workflow_state.md'),fresh,fresh);
  await h.hooks.context({sessionID:'ses_root',agent:'Orchestrator'});
  assert.equal(h.guard.getState().armedSessionId,'ses_root');
 }finally{await h.cleanup()}
});

test('v4.1: annotated machine statuses permit actual active workflow, without ignoring genuine gates',()=>{
  const live = 'Task: queue\nStatus: in_progress (engineering continues; owner checks optional)\nHuman gate: none (0 blockers; owner_live_checks are voluntary)\nPrimary phase: select_next\nNext execution: runnable';
  assert.deepEqual(evaluateWork(baseScope(),live),{ok:true,reason:'actionable-work',count:1,mode:'runnable'});
  assert.equal(evaluateWork(baseScope(),live.replace('in_progress','done')).reason,'workflow-not-active');
  assert.equal(evaluateWork(baseScope(),live.replace('none (0 blockers; owner_live_checks are voluntary)','BLOCKED_REAL_HUMAN_GATE (owner approval needed)')).reason,'human-gate');
  assert.equal(evaluateWork(baseScope(),live.replace('in_progress (engineering continues; owner checks optional)','in_progress but paused')).reason,'workflow-not-active');
  assert.equal(evaluateWork(baseScope(),live.replace('none (0 blockers; owner_live_checks are voluntary)','none; approval required')).reason,'human-gate');
  assert.equal(evaluateWork(baseScope(),live.replace('Human gate: none (0 blockers; owner_live_checks are voluntary)','')).reason,'human-gate');
});
test('v4.1 integration: annotated status still auto-resumes after successful primary turn',async()=>{
 const statusText='Task: a\nStatus: in_progress (task underway)\nHuman gate: none (zero blocking questions)\nPrimary phase: select_next\nNext execution: runnable';
 const h=await harness({statusText});try{
   await h.markRunning();
   assert.equal(h.guard.getState().armedSessionId,'ses_root');
   h.setTime(120000);await h.ended();
   h.setTime(200000);await h.guard.check();
   assert.equal(h.admissions.length,1);
 }finally{await h.cleanup()}
});

test('v4.2: server-like message ID validation does not waste retries',async()=>{
 const h=await harness();try{
  await h.markRunning();h.setTime(120000);await h.ended();h.setTime(200000);
  await h.guard.check();
  assert.equal(h.admissions.length,1);
  assert.match(h.admissions[0].id,/^msg_[a-f0-9]{24}$/);
  assert.equal(h.logs.some(l=>l.e==='prompt-admission-retry'),false);
  assert.equal(h.logs.some(l=>l.e==='resume-queued'),true);
 }finally{await h.cleanup()}
});

test('v4.2: deterministic server schema rejection suspends immediately',async()=>{
 const h=await harness({promptFn:async()=>{throw new Error('Expected a string starting with "msg_" at ["id"]')}});try{
  await h.markRunning();h.setTime(120000);await h.ended();h.setTime(200000);
  await h.guard.check();
  assert.equal(h.guard.getState().suspended,true);
  assert.equal(h.admissions.length,1); // reject without retry
  assert.equal(h.logs.some(l=>l.e==='resume-admission-invalid'),true);
  h.setTime(450000);await h.guard.check();
  assert.equal(h.admissions.length,1); // no more attempts after permanent rejection
 }finally{await h.cleanup()}
});


test('v4.4: PENDING is inventory, never permission to wake when no runnable action',()=>{
  const pending = baseScope();
  assert.equal(evaluateWork(pending,status.replace('Next execution: runnable','Next execution: none')).reason,'no-runnable-transition');
  assert.equal(evaluateWork(pending,status.replace('Next execution: runnable','Next execution: unknown')).reason,'next-execution-missing-or-invalid');
  assert.equal(evaluateWork(pending,status.replace('Next execution: runnable','')).reason,'next-execution-missing-or-invalid');
  assert.equal(evaluateWork(pending,status.replace('Next execution: runnable','Next execution: awaiting_children')).mode,'awaiting_children');
});
test('v4.4: root is disarmed without new prompt on a quiescent-but-unverified scope',async()=>{
  const h=await harness();try{
    await h.markRunning();
    await fs.writeFile(path.join(h.repo,'plans','workflow_state.md'),status.replace('Next execution: runnable','Next execution: none'));
    h.setTime(120000);await h.ended();h.setTime(200000);await h.guard.check();
    assert.equal(h.admissions.length,0);
    assert.equal(h.guard.getState().armedSessionId,null);
    assert.ok(h.logs.some(l=>l.e==='disarmed'&&l.reason==='no-runnable-transition'));
  }finally{await h.cleanup()}
});
test('v4.4: awaiting_children without actual children never injects',async()=>{
  const h=await harness({statusText:status.replace('Next execution: runnable','Next execution: awaiting_children')});try{
    await h.markRunning();h.setTime(120000);await h.ended();h.setTime(200000);
    await h.guard.check(); assert.equal(h.admissions.length,0);
    assert.ok(h.logs.some(l=>l.e==='skip-missing-child-evidence'));
  }finally{await h.cleanup()}
});
test('v4.4: waiting on real finished child legitimately wakes coordinator',async()=>{
  let active=true;
  const h=await harness({statusText:status.replace('Next execution: runnable','Next execution: awaiting_children'),
    listFn:async()=>({items:[{id:'ses_child',parentID:'ses_root'}],cursor:{next:null}}),
    waitFn:async({sessionID})=>{if(sessionID==='ses_child'&&active)throw new Error('busy')}});
  try{
    await h.markRunning();h.setTime(120000);await h.ended();h.setTime(200000);
    await h.guard.check();assert.equal(h.admissions.length,0);
    active=false;await h.guard.check();assert.equal(h.admissions.length,1);
  }finally{await h.cleanup()}
});
test('v4.4: report/phase/Git edits do not reset stalled attempt budget',()=>{
 const scope=baseScope();
 assert.equal(fingerprint(scope,status,'old-head'),fingerprint(scope,status.replace('build','report'),'new-head'));
 const modified={...scope,tasks:[{id:'a',status:'VERIFIED'}]};
 assert.notEqual(fingerprint(scope),fingerprint(modified));
});
test('v4.4: at most two continuation prompts if there is no changed task state',async()=>{
  const h=await harness();try{
    await h.markRunning();h.setTime(120000);await h.ended();
    h.setTime(200000);await h.guard.check();assert.equal(h.admissions.length,1);
    h.setTime(210000);await h.hooks.context({sessionID:'ses_root',agent:'Orchestrator'});
    h.setTime(220000);await h.ended();h.setTime(320000);await h.guard.check();
    assert.equal(h.admissions.length,2);
    h.setTime(330000);await h.hooks.context({sessionID:'ses_root',agent:'Orchestrator'});
    h.setTime(340000);await h.ended();h.setTime(440000);await h.guard.check();
    assert.equal(h.admissions.length,2);
    assert.ok(h.logs.some(l=>l.e==='paused-no-progress'));
  }finally{await h.cleanup()}
});
