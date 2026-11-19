/**
 * OpenCode V2 continuation guard (v4.5). Opts in implicitly only through a real primary
 * Orchestrator MODEL CONTEXT hook; NEVER parses user text or starts stale sessions.
 * Uses V2 session.execution.* lifecycle, not deprecated session.idle.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { normalizePath, evaluateWork, sessionMatches, eventSessionId, eventReason,
  safePermissionCount, fingerprint, assessAttempt, positiveInt } from './core.mjs';

const RESUME_TEXT = 'Автоматическое продолжение уже активной рабочей сессии после неожиданного завершения хода. Это не новое задание и не разрешение на Human Gate. Сверь фактическое состояние очереди, результаты исполнителей, git и ревью. Не повторяй завершённую работу. Если есть готовый следующий шаг, запусти его до отчёта; если всё завершено или есть настоящий блокер — зафиксируй состояние и не продолжай. Не посылай тестовые сообщения в рабочие чаты. Отчитайся простым русским языком.';
const defaults = Object.freeze({enabled:true,pollMs:20000,minIdleMs:60000,cooldownMs:90000,
  maxAttemptsWithoutProgress:2,maxIdleAgeHours:8,dryRun:false,sessionLeaseHours:36,agent:'Orchestrator'});
const empty = () => ({version:4,armedSessionId:null,agent:null,armedUntil:0,
  lastStartedAt:0,lastSucceededAt:0,lastAttemptForEnd:0,lastAttemptAt:0,
  childSessions:{},childCoverage:false,childCoverageReason:null,
  attempts:0,signature:null,suspended:false,pendingOwnerPrompt:false});

function safeError(e){return String(e?.message ?? e).slice(0,300)}
async function readJson(file){try{return JSON.parse(await fs.readFile(file,'utf8'))}catch{return null}}
async function readText(file){try{return await fs.readFile(file,'utf8')}catch{return ''}}
async function mtime(file){try{return (await fs.stat(file)).mtimeMs}catch{return 0}}
function bounded(promise,ms,label,controller){
  let timer;
  return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>{
    controller?.abort();reject(new Error(`timeout:${label}`));
  },ms)})]).finally(()=>clearTimeout(timer));
}
function eventType(e){return e?.type ?? e?.details?.type ?? ''}

export async function startGuard(ctx,{pluginDir,logger,now=()=>Date.now(),setIntervalFn=setInterval,
  clearIntervalFn=clearInterval}={}){
  const repo=path.resolve(ctx.location?.directory ?? ctx.location?.project?.canonical ?? process.cwd());
  const stateKey=`guard/v4/${crypto.createHash('sha256').update(normalizePath(repo)).digest('hex').slice(0,20)}`;
  const logRoot=path.join(process.env.LOCALAPPDATA||path.join(os.homedir(),'.local','state'),'OpenCode','continuation-guard');
  const log=logger|| (async (event,extra={})=>{try{
    await fs.mkdir(logRoot,{recursive:true});
    const f=path.join(logRoot,'events.log');
    if((await fs.stat(f).catch(()=>({size:0}))).size>700000)
      await fs.rename(f,path.join(logRoot,'events.old.log')).catch(()=>{});
    await fs.appendFile(f,JSON.stringify({at:new Date().toISOString(),event,...extra})+'\n');
  }catch{}});
  let s=empty(),disposed=false,checking=false,ready=false;
  let promptRegistration=null,contextRegistration=null,idleRegistration=null,timer=null;
  let eventController=new AbortController();
  let automaticPromptInFlight=false;
  const runningChildren=new Set();
  let lastConfig=null;
  const repoHash=stateKey.slice(-12);
  const loadedAt=now();
  const logEvent=(event,detail={})=>log(event,{project:repoHash,...detail});
  async function config(){
    const c=await readJson(path.join(pluginDir,'config.json'));
    const v={...defaults,...(c&&typeof c==='object'?c:{})};
    return {enabled:v.enabled===true,pollMs:positiveInt(v.pollMs,defaults.pollMs,10000,300000),
      minIdleMs:positiveInt(v.minIdleMs,defaults.minIdleMs,10000,1800000),
      cooldownMs:positiveInt(v.cooldownMs,defaults.cooldownMs,15000,3600000),
      maxAttemptsWithoutProgress:positiveInt(v.maxAttemptsWithoutProgress,2,1,20),
      maxIdleAgeMs:positiveInt(v.maxIdleAgeHours,8,1,168)*3600000,
      leaseMs:positiveInt(v.sessionLeaseHours,36,1,168)*3600000,
      agent:String(v.agent||'Orchestrator'),dryRun:v.dryRun===true};
  }
  async function save(){await ctx.storage.set(stateKey,s)}
  async function clear(reason){
    if(!s.armedSessionId)return;
    const id=s.armedSessionId;s=empty();runningChildren.clear();await save();
    await logEvent('disarmed',{sessionId:id,reason});
  }
  async function eligibility(){
    const wf=path.join(repo,'plans','workflow_state.md');
    const sc=path.join(repo,'plans','progress_scope.json');
    const [scope,workflow]=await Promise.all([readJson(sc),readText(wf)]);
    return {scope,workflow,decision:evaluateWork(scope,workflow),stamps:{scope:await mtime(sc),workflow:await mtime(wf)}};
  }
  async function modelContext(event){
    if(disposed)return;
    try{
      const cfg=await config();if(!cfg.enabled)return;
      // Only the coordinator's actual PRIMARY model call; child agents never arm.
      if(String(event.agent||'').toLowerCase()!==cfg.agent.toLowerCase())return;
      const id=event.sessionID;if(typeof id!=='string'||!id)return;
      const info=await bounded(ctx.session.get({sessionID:id}),12000,'context-get');
      if(!sessionMatches(info,id,repo,cfg.agent))return;
      const {decision,stamps}=await eligibility();
      if(!decision.ok){if(s.armedSessionId===id)await clear(decision.reason);return}
      const t=now();
      const created=Number(info.time?.created);
      const earliest=Number.isFinite(created)&&created>0 ? created : loadedAt;
      // A previous day's BUILDING flag is not evidence that this fresh session
      // runs that workflow. Require a checkpoint written around/after the
      // session's creation; subsequent model steps can arm after PM updates it.
      if(s.armedSessionId!==id && stamps.workflow < earliest-60000){
        await logEvent('await-fresh-workflow-checkpoint',{sessionId:id});
        return;
      }
      // After manual interruption, a generic conversation is insufficient to
      // resume an old job. Wait until the checkpoint is updated by this run.
      if(s.suspended && s.pendingOwnerPrompt && stamps.workflow<(s.suspendedAt||0))return;
      if(s.armedSessionId && s.armedSessionId!==id)await clear('new-active-session');
      // A fresh actual model dispatch after owner interaction naturally arms.
      // An interrupt can re-arm only after a new EXTERNAL user admission.
      if(s.suspended && !s.pendingOwnerPrompt)return;
      const fresh=s.armedSessionId!==id;
      if(fresh){s=empty();runningChildren.clear();}
      else if(s.childSessions){
        // A new actual primary model turn is the natural handoff boundary.
        // Retire only terminal child observations, never live/unknown work.
        for(const [child,record] of Object.entries(s.childSessions)){
          if(['succeeded','failed','interrupted','quiescent'].includes(record?.state))
            delete s.childSessions[child];
        }
      }
      s.armedSessionId=id;s.agent=cfg.agent;s.armedUntil=t+cfg.leaseMs;
      // An actual primary model call is strong proof that this execution started.
      s.lastStartedAt=t;s.lastSucceededAt=0;
      s.suspended=false;s.pendingOwnerPrompt=false;
      // The started event is authoritative for busy; context may precede it.
      await save();
      if(fresh)await logEvent('armed-by-model-execution',{sessionId:id,available:decision.count});
    }catch(e){await logEvent('model-context-error',{message:safeError(e)})}
  }
  async function promptAdmission(event){
    if(disposed)return;
    const id=event?.sessionID??event?.sessionId;
    if(!id||id!==s.armedSessionId||automaticPromptInFlight)return;
    // Structural observation only: DO NOT read event.prompt.text.
    s.pendingOwnerPrompt=true;
    // A new external prompt supersedes any assumed idle continuation.
    s.lastSucceededAt=0;
    await save();
    await logEvent('external-prompt-admitted',{sessionId:id});
  }
  // Native V2 execution events are evidence of a child turn, not proof that
  // its result was delivered to the parent. Keep child identities in the same
  // durable guard state; a process-local Set alone is lost on desktop restart.
  async function trackChild(event,id,type){
    if(!s.armedSessionId || !id || id===s.armedSessionId)return false;
    const p=event?.properties??event?.data??{};
    let parent=p.info?.parentID??p.parentID??null;
    if(!parent && s.childSessions?.[id])parent=s.armedSessionId;
    if(!parent){
      try{const info=await bounded(ctx.session.get({sessionID:id}),4000,'child-get');
        parent=(info?.data??info)?.parentID??null;}
      catch{return false;}
    }
    if(parent!==s.armedSessionId)return false;
    const children=s.childSessions??(s.childSessions={});
    const prev=children[id]??{state:'observed',at:now()};
    let state=prev.state;
    if(type==='session.execution.started')state='running';
    if(type==='session.execution.succeeded')state='succeeded';
    if(type==='session.execution.failed')state='failed';
    if(type==='session.execution.interrupted')state='interrupted';
    if(type==='session.deleted')state='missing';
    children[id]={...prev,state,at:now()};
    if(state==='running')runningChildren.add(id);
    else if(['succeeded','failed','interrupted','missing'].includes(state))runningChildren.delete(id);
    await save();
    if(['succeeded','failed','interrupted'].includes(state)){
      await logEvent('child-terminal-observed',{sessionId:id,parentID:parent,outcome:state});
      // Reconciliation on the already-installed guard timer is safe and does
      // not inject another completion notification or start a new watcher.
    }
    return true;
  }
  async function onEvent(event){
    if(disposed)return;
    const id=eventSessionId(event);
    const type=eventType(event);
    if(!id||!s.armedSessionId)return;
    if(id!==s.armedSessionId){
      if(['session.created','session.execution.started','session.execution.succeeded',
        'session.execution.failed','session.execution.interrupted','session.deleted'].includes(type))
        await trackChild(event,id,type);
      return;
    }
    const t=now();
    if(type==='session.execution.started'||(type==='session.status'&&event?.properties?.status?.type==='busy')){
      s.lastStartedAt=t;s.lastSucceededAt=0;s.armedUntil=t+(lastConfig?.leaseMs??defaults.sessionLeaseHours*3600000);
      await save();return;
    }
    if(type==='session.execution.succeeded'){
      s.lastSucceededAt=t;await save();return;
    }
    // session.idle/status idle is not an authoritative execution result.
    if(type==='session.execution.interrupted'||type==='session.execution.failed'||type==='session.error'){
      s.suspended=true;s.suspendedAt=t;s.pendingOwnerPrompt=false;s.lastSucceededAt=0;
      await save();
      await logEvent('suspended-after-execution-end',{sessionId:id,type,reason:eventReason(event)});
    }
    if(type==='session.deleted')await clear('session-deleted');
  }
  // Native plugin V2's experimental.idle hook exposes background work at an
  // actual primary idle boundary. It is a snapshot, not a liveness heartbeat.
  // Unknown job identifiers must not silently become 'no children'.
  async function onNativeIdle(event){
    if(disposed||event?.sessionID!==s.armedSessionId)return;
    if(!Array.isArray(event.background))return;
    let complete=true;
    const children=s.childSessions??(s.childSessions={});
    for(const job of event.background){
      // A running background shell/image is also real work: never declare
      // the root quiescent while it exists. Only subagent children can be
      // checked by session.wait; other job types need a fresh native snapshot.
      if(job?.kind!=='subagent'){complete=false;continue;}
      const id=job.childSessionID??job.childID??job.subagentSessionID??
        (job.sessionID!==s.armedSessionId?job.sessionID:null);
      if(typeof id!=='string'||!id){complete=false;continue;}
      children[id]??={state:'observed',at:now()};
    }
    s.childCoverage=complete;
    s.childCoverageReason=complete?'native-idle-background':'unidentified-native-background';
    await save();
  }
  // Optional inventory is useful on hosts exposing session.list, but that API
  // is NOT required by the current V2 Promise Plugin domain. For hosts without
  // it, use tracked child IDs plus a real idle snapshot. After an event gap,
  // unknown work remains UNKNOWN, not completed.
  let lastChildSkip='';
  async function ensureChildrenQuiescent(rootID){
    const children=new Set([...Object.keys(s.childSessions??{}),...runningChildren]);
    let inventoryComplete=false;
    if(typeof ctx.session.list==='function'){
      let cursor=null;const cursors=new Set();
      for(let pageNo=0;pageNo<12;pageNo++){
        const params={directory:repo,parentID:rootID,limit:100};
        if(cursor)params.cursor=cursor;
        let response;
        try{response=await bounded(ctx.session.list(params),10000,'list-children')}
        catch{return {ok:false,reason:'child-inventory-error'}}
        const rows=Array.isArray(response)?response:
          (response?.items??response?.data?.items??response?.sessions??response?.data?.sessions??
          (Array.isArray(response?.data)?response.data:undefined));
        if(!Array.isArray(rows))return {ok:false,reason:'child-inventory-unrecognized'};
        for(const item of rows){
          if(typeof item?.id!=='string'||item.parentID!==rootID)
            return {ok:false,reason:'child-inventory-incomplete-or-mismatch'};
          children.add(item.id);
        }
        const next=response?.cursor?.next??response?.data?.cursor?.next??null;
        if(!next){inventoryComplete=true;break;}
        if(cursors.has(next)||pageNo===11)return {ok:false,reason:'child-inventory-truncated'};
        cursors.add(next);cursor=next;
      }
    }
    if(!inventoryComplete && !s.childCoverage)
      return {ok:false,reason:'child-roster-unverified'};
    const ids=[...children];
    if(typeof ctx.session.wait!=='function')return {ok:false,reason:'child-wait-unavailable'};
    for(let start=0;start<ids.length;start+=12){
      const results=await Promise.all(ids.slice(start,start+12).map(async id=>{
        const ac=new AbortController();
        try{await bounded(ctx.session.wait({sessionID:id},{signal:ac.signal}),1800,'child-wait',ac);
          return {id,ok:true};}
        catch{return {id,ok:false};}
      }));
      const pending=results.find(r=>!r.ok);
      if(pending)return {ok:false,reason:'child-still-running-or-unknown',childID:pending.id};
    }
    return {ok:true,observed:ids.length};
  }
  async function skipForChildren(rootID){
    const status=await ensureChildrenQuiescent(rootID);
    if(status.ok){lastChildSkip='';return status;}
    const key=`${rootID}:${status.reason}:${status.childID||''}`;
    if(lastChildSkip!==key){lastChildSkip=key;
      await logEvent('skip-child-barrier',{sessionId:rootID,reason:status.reason,childID:status.childID});}
    return status;
  }
  async function check(trigger='timer'){
    if(disposed||checking||!ready)return;
    checking=true;
    try{
      const cfg=await config();lastConfig=cfg;if(!cfg.enabled)return;
      const t=now();
      if(!s.armedSessionId)return;
      if(s.armedUntil<t){await clear('lease-expired');return}
      if(s.suspended||s.pendingOwnerPrompt)return;
      // Check executable next transition FIRST. Merely having PENDING tasks or
      // an in_progress checkpoint no longer licenses an extra model turn.
      const data=await eligibility();
      if(!data.decision.ok){await clear(data.decision.reason);return}
      // The event cache alone CANNOT prove no child is active: it may miss a
      // child started before this plugin loaded or during a stream outage.
      const children=await skipForChildren(s.armedSessionId);
      if(!children.ok)return;
      if(data.decision.mode==='awaiting_children' && !children.observed){
        await logEvent('skip-missing-child-evidence',{sessionId:s.armedSessionId});return;
      }
      const sig=fingerprint(data.scope);
      const verdict=assessAttempt(s,sig,cfg,t);
      if(!verdict.ok){if(verdict.reason==='no-progress-cap' && !s.noProgressLogged){
        s.noProgressLogged=true;await save();await logEvent('paused-no-progress',{sessionId:s.armedSessionId,attempts:s.attempts});}
        return;
      }
      const id=s.armedSessionId;
      const ended=s.lastSucceededAt;
      const info=await bounded(ctx.session.get({sessionID:id}),10000,'get');
      if(!sessionMatches(info,id,repo,cfg.agent)){await clear('session-mismatch');return}
      // Barrier: if still working, timeout and defer. Never assume time.updated means idle.
      const ac=new AbortController();
      try{await bounded(ctx.session.wait({sessionID:id},{signal:ac.signal}),3500,'wait',ac)}
      catch(e){if(String(e).includes('timeout:wait'))return;throw e}
      // Users/OS permission requests block, including unknown responses.
      const permissions=await bounded(ctx.permission.list({sessionID:id}),10000,'permission-list');
      const n=safePermissionCount(permissions);
      if(n===null||n>0){await logEvent('skip-permission',{sessionId:id,count:n});return}
      // Recheck state after asynchronous operations: a human may have acted meanwhile.
      if(s.armedSessionId!==id||s.pendingOwnerPrompt||s.suspended||runningChildren.size||s.lastSucceededAt!==ended)return;
      // Final child inventory just before admission catches newly started
      // background agents while the root checks permissions and Git.
      const finalChildren=await skipForChildren(id);
      if(!finalChildren.ok)return;
      if(s.armedSessionId!==id||s.pendingOwnerPrompt||s.suspended||runningChildren.size||s.lastSucceededAt!==ended)return;
      const fresh=await eligibility();if(!fresh.decision.ok){await clear(fresh.decision.reason);return}
      if(fresh.decision.mode==='awaiting_children' && !finalChildren.observed){
        await logEvent('skip-missing-child-evidence',{sessionId:id});return;
      }
      // The coordinator may checkpoint a different transition while we wait.
      if(fingerprint(fresh.scope)!==sig)return;
      if(cfg.dryRun){await logEvent('dry-run-would-resume',{sessionId:id});return}
      s.attempts=verdict.repeat+1;s.signature=sig;s.lastAttemptAt=t;s.lastAttemptForEnd=ended;
      s.noProgressLogged=false;
      await save(); // Reserve *before* admission. Repeated events cannot duplicate.
      automaticPromptInFlight=true;
      // OpenCode Session.Message.ID is constrained to /^msg_/ (the earlier
      // 'inbox_' prefix caused 400 validation errors on every resume).
      // Deterministic per completed turn: safe to retry after a lost ACK.
      const requestID=`msg_${crypto.createHash('sha256').update(`continuation-guard-v4:${id}:${ended}`).digest('hex').slice(0,24)}`;
      try{
        // A stable inbox ID makes a retry idempotent if the first ACK is lost.
        for(let attempt=0;attempt<2;attempt++){
          try{
            await bounded(ctx.session.prompt({sessionID:id,id:requestID,text:RESUME_TEXT,delivery:'queue',resume:true}),20000,'prompt');
            break;
          }catch(e){
            if(attempt===1 || /Expected a string starting with|validation|invalid.*(?:id|input)/i.test(safeError(e)))throw e;
            await logEvent('prompt-admission-retry',{sessionId:id,message:safeError(e)});
          }
        }
      }catch(e){
        const msg=safeError(e);
        // A schema validation rejection is deterministic, not a flaky ACK.
        // Fail closed immediately; never burn four resume attempts on it.
        if (/Expected a string starting with|validation|invalid.*(?:id|input)/i.test(msg)){
          s.suspended=true;
          s.suspendedAt=now();
          s.pendingOwnerPrompt=false;
          await save();
          await logEvent('resume-admission-invalid',{sessionId:id,message:msg});
        }else{
          // An ambiguous transport failure may have admitted the prompt.
          // After cooldown a retry uses the SAME deterministic message ID.
          s.lastAttemptForEnd=0;
          await save();
        }
        throw e;
      }finally{automaticPromptInFlight=false}
      await logEvent('resume-queued',{sessionId:id,attempts:s.attempts,trigger,messageID:requestID});
    }catch(e){await logEvent('guard-error',{trigger,message:safeError(e)})}
    finally{checking=false}
  }
  async function init(){
    const cfg=await config();lastConfig=cfg;
    if(!cfg.enabled){await logEvent('plugin-disabled');return ()=>{}}
    // Persisted v3 trigger-word sessions are NEVER imported.
    const old=await ctx.storage.get(stateKey);
    if(old?.version===4){s={...empty(),...old};
      // Do not reactivate a possibly stale state on startup. Only a newly
      // observed native primary model call can renew its execution lease.
      s.lastSucceededAt=0;s.lastStartedAt=0;s.pendingOwnerPrompt=false;
      s.childCoverage=false;s.childCoverageReason='backend-restarted';
      s.childSessions=(s.childSessions&&typeof s.childSessions==='object'&&!Array.isArray(s.childSessions))?s.childSessions:{};
      await save();
    }
    contextRegistration=await ctx.session.hook('context',modelContext);
    promptRegistration=await ctx.session.hook('prompt',promptAdmission);
    try{idleRegistration=await ctx.session.hook('experimental.idle',onNativeIdle)}
    catch(e){await logEvent('native-idle-hook-unavailable',{message:safeError(e)})}
    ready=true;
    // Use real V2 execution events. Never depend solely on deprecated idle.
    void (async()=>{
      let retries=0;
      while(!disposed){
        try{
          for await(const event of ctx.event.subscribe({signal:eventController.signal})){
            if(disposed)break;
            retries=0;
            await onEvent(event);
          }
          if(!disposed){s.childCoverage=false;s.childCoverageReason='event-stream-ended';
            await save();await logEvent('event-stream-ended');}
        }catch(e){
          if(!disposed){s.childCoverage=false;s.childCoverageReason='event-stream-error';
            await save();await logEvent('event-stream-error',{message:safeError(e)});}
        }
        if(disposed)break;
        // Reconnect without any model calls. A dropped event stream must not
        // leave the guard permanently deaf for the rest of an overnight run.
        retries++;
        const delay=Math.min(30000,1000*2**Math.min(retries,5));
        await new Promise(resolve=>{
          const timer=setTimeout(resolve,delay);
          eventController.signal.addEventListener('abort',()=>{clearTimeout(timer);resolve()},{once:true});
        });
      }
    })();
    timer=setIntervalFn(()=>{void check('timer')},cfg.pollMs);
    await logEvent('plugin-loaded',{version:'4.5',sessionTracking:'model-context-and-execution-events'});
    return async()=>{
      disposed=true;ready=false;
      if(timer!==null)clearIntervalFn(timer);
      eventController.abort();
      await promptRegistration?.dispose?.();await contextRegistration?.dispose?.();
      await idleRegistration?.dispose?.();
    };
  }
  return {init,check,onEvent,modelContext,promptAdmission,getState:()=>({...s})};
}
