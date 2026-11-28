/**
 * OpenCode V2 continuation guard (v5.0.1). Opts in implicitly only through a real primary
 * Orchestrator MODEL CONTEXT hook; NEVER parses user text or starts stale sessions.
 * Uses V2 session.execution.* lifecycle, not deprecated session.idle.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { normalizePath, evaluateWork, sessionMatches, eventSessionId, eventReason,
  safePermissionCount, fingerprint, assessAttempt, positiveInt, certifiedReadyQueueIds } from './core.mjs';
import {createReporter,readReportState,publishedReport,containsReport} from './report.mjs';
import {createTelemetry} from './telemetry.mjs';

const RESUME_TEXT = 'Автопродолжение текущей сессии, не новое поручение и не разрешение. Сверь каноническую очередь и реальные Assignment/child/job; используй уже полученные результаты. Назначь следующую безопасную работу по всей очереди, даже если предыдущий релиз завершён или другая задача ждёт владельца. Не повторяй историческую приёмку без конкретного дефекта. Уточни только мешающие записи. Не обходи реальные разрешения, не дублируй исполнителей, не пиши тестовые сообщения в рабочие чаты. Сначала продвижение работы, затем полный отчёт.';
const defaults = Object.freeze({enabled:false,pollMs:20000,minIdleMs:60000,cooldownMs:90000,
  maxAttemptsWithoutProgress:2,maxIdleAgeHours:8,dryRun:false,sessionLeaseHours:36,agent:'Orchestrator',reportEnabled:true,livenessSilenceMinutes:30});
const empty = () => ({version:4,armedSessionId:null,agent:null,armedUntil:0,
  lastStartedAt:0,lastSucceededAt:0,lastAttemptForEnd:0,lastAttemptAt:0,
  childSessions:{},childCoverage:false,childCoverageReason:null,
  attempts:0,signature:null,suspended:false,pendingOwnerPrompt:false,livenessNotices:{}});

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
  let promptRegistration=null,contextRegistration=null,idleRegistration=null,reportRegistration=null,timer=null;
  let eventController=new AbortController();
  let automaticPromptInFlight=false;
  const runningChildren=new Set();
  // Runtime observations only. No elapsed-time cancellation or invented terminal state.
  let nativeIdleFor=null;
  const terminalStates=new Set(['succeeded','failed','interrupted','quiescent']);
  const incidentKey=(id,r,kind)=>crypto.createHash('sha256').update(JSON.stringify([s.armedSessionId,id,r.startedAt??r.at??0,kind])).digest('hex');
  const liveState=(r)=>r.diagnosis??r.state??'observed';
  function rememberChild(id,extra={}){
    const children=s.childSessions??(s.childSessions={});
    const r=children[id]??(children[id]={state:'observed',at:now(),startedAt:now(),activityAt:now()});
    Object.assign(r,extra);return r;
  }
  function activity(id,type){
    const r=s.childSessions?.[id];if(!r||terminalStates.has(r.state)||r.state==='missing')return;
    if(/^(session\.(step|tool|text|reasoning|compaction)\.|session\.retry\.scheduled)/.test(type)){
      r.activityAt=now();delete r.diagnosis;
    }
  }

  let lastConfig=null;
  const repoHash=stateKey.slice(-12);
  const loadedAt=now();
  const logEvent=(event,detail={})=>log(event,{project:repoHash,...detail});
  // Reporting state is ephemeral output, never a second queue or liveness proof.
  let reportRoot=null,reportCandidate=null,reportModel=null,reportBoundary=null,reportEpoch=0,lastPublishedMessage=null,reportMissedRoot=null;
  const telemetry=createTelemetry(now);
  const reportTexts=new Map();
  let reportVerifyInFlight=null;
  const seenRootStarts=new Set();
  function advanceReportBoundary(){reportEpoch++;reportBoundary=null;reportTexts.clear();}
  const reportObserved=()=>Object.entries(s.childSessions??{}).filter(([,r])=>!r.waitConfirmed||!!r.diagnosis||(r.at??0)>=s.lastStartedAt).map(([id,r])=>({id,state:liveState(r),role:r.role??null,startedAt:r.startedAt??r.at??null}));
  const reporter=createReporter(ctx,{repo,pluginDir,now,log:logEvent,getRoot:()=>reportCandidate,getModel:()=>reportModel,getBoundary:()=>reportEpoch,getObserved:reportObserved});
  const reportInstruction=`REPORT CONTRACT: At EACH primary wait/status/release boundary, after dispatching safe work and updating the checkpoint, call workflow_report(${JSON.stringify({canonical_root:repo})}). It has no action parameter; workflow_intel is unrelated. Publish the entire returned Russian Markdown as the visible assistant reply, including metrics, all table rows, line breaks and receipt. A previous report (even identical revision) never satisfies a new wait. Do not write your own delta instead. If a report is already prepared in this execution and no engineering/state change followed, publish it now; do not call the tool repeatedly. If work changed, refresh once before waiting. A real missing tool/result permits a grounded manual fallback, not an engineering pause. No extra Reporter session or report-only commits.`;
  function injectReportInstruction(event){
    if(!Array.isArray(event.system)||!reportRegistration)return;
    const pending=reportMissedRoot===event.sessionID?' Previous boundary lacked a confirmed publication; on this normal turn prepare and publish the current report before waiting, not the old report.':'';
    const prepared=reporter.expected(event.sessionID,reportEpoch)?' A report is already prepared for this execution. Publish that complete result if no work has changed.':'';
    const text=reportInstruction+pending+prepared;
    const index=event.system.findIndex(p=>p?.type==='text'&&p.text?.startsWith('REPORT CONTRACT:'));
    if(index>=0)event.system[index]={type:'text',text};else event.system.push({type:'text',text});
  }
  async function verifyReportBoundary(id,{allowRepair=false}={}){
    if(!reportRegistration||reportRoot!==id||disposed||s.suspended||s.pendingOwnerPrompt)return;
    const epoch=reportEpoch;
    if(reportBoundary===epoch)return;
    while(reportVerifyInFlight?.epoch===epoch){await reportVerifyInFlight.promise;if(reportBoundary===epoch||!allowRepair)return;}
    if(reportRoot!==id||reportEpoch!==epoch)return;
    const valid=()=>!disposed&&reportRoot===id&&reportEpoch===epoch&&!s.suspended&&!s.pendingOwnerPrompt;
    const job={epoch,promise:null};reportVerifyInFlight=job;
    job.promise=(async()=>{
      try{
        const cfg=lastConfig??await config();if(!cfg.enabled||!cfg.reportEnabled||cfg.dryRun||!valid())return;
        const expected=reporter.expected(id,epoch);
        let published=null;
        // Authoritative final text events, grouped by message and ordinal.
        // No tool, reasoning, synthetic or previous-epoch content accepted.
        if(expected){
          const latest=[...reportTexts.entries()].at(-1);
          if(latest&&containsReport([...latest[1].entries()].sort((a,b)=>a[0]-b[0]).map(x=>x[1]).join('\n'),expected))published=latest[0];
          if(!published&&typeof ctx.session.context==='function'){
            try{const raw=await bounded(ctx.session.context({sessionID:id}),8000,'report-context');published=publishedReport(raw?.data??raw,expected,lastPublishedMessage);}catch{}
          }
        }
        if(!valid())return;
        if(published&&reporter.markPublished(id,epoch)){
          reportBoundary=epoch;lastPublishedMessage=published;reportMissedRoot=null;await logEvent('report-published',{sessionId:id,boundary:epoch});return;
        }
        reportMissedRoot=id;
        await logEvent('report-missing-or-unverified',{sessionId:id,boundary:epoch,prepared:!!expected});
        if(allowRepair)reportBoundary=epoch;
        // Observability must not create an extra model turn.
        if(!allowRepair)return;
        // V4: publishing is mandatory in normal turns, but report repair never
        // starts a second model turn or competes with engineering recovery.
        await logEvent('report-repair-skipped',{sessionId:id,boundary:epoch,reason:'next-normal-turn'});return;

      }catch{await logEvent('report-boundary-unverified',{sessionId:id,boundary:epoch});}
    })();
    try{await job.promise;}finally{if(reportVerifyInFlight===job)reportVerifyInFlight=null;}
  }
  async function config(){
    const c=await readJson(path.join(pluginDir,'config.json'));
    const v={...defaults,...(c&&typeof c==='object'?c:{})};
    return {enabled:v.enabled===true,pollMs:positiveInt(v.pollMs,defaults.pollMs,10000,300000),
      minIdleMs:positiveInt(v.minIdleMs,defaults.minIdleMs,10000,1800000),
      cooldownMs:positiveInt(v.cooldownMs,defaults.cooldownMs,15000,3600000),
      maxAttemptsWithoutProgress:positiveInt(v.maxAttemptsWithoutProgress,2,1,20),
      maxIdleAgeMs:positiveInt(v.maxIdleAgeHours,8,1,168)*3600000,
      leaseMs:positiveInt(v.sessionLeaseHours,36,1,168)*3600000,
      agent:String(v.agent||'Orchestrator'),dryRun:v.dryRun===true,reportEnabled:v.reportEnabled!==false,
      livenessSilenceMs:positiveInt(v.livenessSilenceMinutes,30,5,1440)*60000};
  }
  async function save(){await ctx.storage.set(stateKey,s)}
  async function clear(reason){
    if(!s.armedSessionId)return;
    const id=s.armedSessionId;s=empty();runningChildren.clear();nativeIdleFor=null;await save();
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
      if(reportRoot&&reportRoot!==id){reporter.reset();reportRoot=null;reportMissedRoot=null;advanceReportBoundary();lastPublishedMessage=null;}
      const {decision,stamps,scope,workflow}=await eligibility();
      reportCandidate=scope?.schema==='openworkflow-progress-scope-v1'&&workflow?id:null;
      reportModel=event.model??info.model??null;
      if(!decision.ok){
        // A closing workflow still needs its final report, but is not armed for execution.
        if(reportCandidate===id&&cfg.reportEnabled&&!cfg.dryRun&&!s.suspended&&!s.pendingOwnerPrompt){if(reportRoot!==id){reporter.reset();reportRoot=id;advanceReportBoundary();}injectReportInstruction(event);void reporter.prewarm(id,reportEpoch);}
        if(s.armedSessionId===id)await clear(decision.reason);return;
      }
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
      if(fresh){s=empty();runningChildren.clear();nativeIdleFor=null;}
      if(reportRoot!==id){reporter.reset();reportRoot=id;advanceReportBoundary();lastPublishedMessage=null;}
      if(cfg.reportEnabled&&!cfg.dryRun){injectReportInstruction(event);void reporter.prewarm(id,reportEpoch);}
      // Retain terminal observations for this root: paginated inventory may
      // include old sessions forever. Removing them would turn every old
      // successful wait into a new completion notice.
      s.armedSessionId=id;s.agent=cfg.agent;s.armedUntil=t+cfg.leaseMs;
      // An actual primary model call is strong proof that this execution started.
      s.lastStartedAt=t;s.lastSucceededAt=0;nativeIdleFor=null;
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
    s.pendingOwnerPrompt=true;nativeIdleFor=null;
    advanceReportBoundary();
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
    let parent=p.info?.parentID??p.parentID??null;let childRole=p.info?.agent??p.agent;
    if(!parent && s.childSessions?.[id])parent=s.armedSessionId;
    if(!parent){
      try{const info=await bounded(ctx.session.get({sessionID:id}),4000,'child-get');
        parent=(info?.data??info)?.parentID??null;childRole=(info?.data??info)?.agent??childRole;}
      catch{return false;}
    }
    if(parent!==s.armedSessionId)return false;
    const children=s.childSessions??(s.childSessions={});
    const prev=children[id]??{state:'observed',at:now(),startedAt:now(),activityAt:now()};
    let state=prev.state;
    if(type==='session.execution.started')state='running';
    if(type==='session.execution.succeeded')state='succeeded';
    if(type==='session.execution.failed')state='failed';
    if(type==='session.execution.interrupted')state='interrupted';
    if(type==='session.deleted')state='missing';
    // Duplicate terminal/start events do not reset the incident clock. A new
    // start after a terminal turn is a new observation, not a completion proof.
    if(state!==prev.state){
      children[id]={...prev,state,at:now(),activityAt:now(),waitConfirmed:false};
      delete children[id].diagnosis;
      if(state==='running'){children[id].startedAt=now();children[id].everPending=false;delete children[id].reconciledAt;delete children[id].jobStatus;delete children[id].jobAt;delete children[id].jobID;}
    }else children[id]=prev;
    if(typeof childRole==='string'&&/^(Builder|Reviewer|Architect|PM|DevOps|Memory|Arbiter)$/i.test(childRole))children[id].role=childRole;
    if(state!==prev.state&&reportCandidate===s.armedSessionId&&lastConfig?.reportEnabled&&!lastConfig?.dryRun)void reporter.prewarm(reportCandidate,reportEpoch);
    if(state==='running')runningChildren.add(id);
    else if(['succeeded','failed','interrupted','missing'].includes(state))runningChildren.delete(id);
    await save();
    if(['succeeded','failed','interrupted'].includes(state)&&prev.state!==state){
      
      await logEvent('child-terminal-observed',{sessionId:id,parentID:parent,outcome:state});
      // Keep child identity durable; the existing guard reconciliation checks
      // the authoritative terminal state before any wake/admission.
    }
    return true;
  }
  async function onEvent(event){
    if(disposed)return;
    const type=eventType(event);
    // External MCP events are handled by native OpenCode subscriptions, not this guard.
    if(type==='experimental.mcp.event')return;
    // Job updates are hints only; the plugin API is not the public SDK job API.
    // Never interpret job absence or an unrecognized event as completion.
    if(type==='experimental.job.updated'){
      const p=event?.properties??event?.data??{};const j=p.job??p;
      const jobID=j.jobID??j.id;
      const child=j.childSessionID??j.childID??j.subagentSessionID??(typeof jobID==='string'?Object.keys(s.childSessions??{}).find(id=>s.childSessions[id].jobID===jobID):null);
      if(typeof child==='string'&&s.childSessions?.[child]){
        const r=s.childSessions[child];const status=typeof j.status==='string'?j.status:j.status?.type;
        if(status&&status!==r.jobStatus){r.jobStatus=status;r.jobAt=now();await save();
          await logEvent('child-job-signal',{sessionId:child,status});}
      }
    }
    const id=eventSessionId(event);
    if(!id)return;
    const isObserved=id===reportRoot||id===s.armedSessionId||Object.hasOwn(s.childSessions??{},id);
    if(isObserved){
      const metric=telemetry.observe(type,id,event?.properties??event?.data??{});
      if(metric)await logEvent('execution-telemetry',metric);
    }
    if(id===reportRoot&&type==='session.execution.started'){
      const nativeID=typeof event.id==='string'?event.id:null;
      if(!nativeID||!seenRootStarts.has(nativeID)){
        if(nativeID){seenRootStarts.add(nativeID);while(seenRootStarts.size>256)seenRootStarts.delete(seenRootStarts.values().next().value);}
        advanceReportBoundary();
      }
    }
    if(id===reportRoot&&type==='session.text.ended'){
      const p=event?.properties??event?.data??{};
      if(typeof p.assistantMessageID==='string'&&Number.isSafeInteger(p.ordinal)&&typeof p.text==='string'){
        let parts=reportTexts.get(p.assistantMessageID);if(!parts){parts=new Map();reportTexts.set(p.assistantMessageID,parts);}
        parts.set(p.ordinal,p.text); // replace replayed ordinal, never concatenate a duplicate
        // Only latest messages are needed; do not store entire conversation.
        while(reportTexts.size>8)reportTexts.delete(reportTexts.keys().next().value);
      }
    }
    if(id===reportRoot&&type==='session.execution.succeeded')void verifyReportBoundary(id);
    if(id===reportRoot&&['session.execution.interrupted','session.execution.failed','session.error','session.deleted'].includes(type)){reporter.reset();reportRoot=null;reportCandidate=null;reportMissedRoot=null;advanceReportBoundary();telemetry.reset();}
    if(!s.armedSessionId)return;
    if(id!==s.armedSessionId){
      activity(id,type);
      if(['session.created','session.execution.started','session.execution.succeeded',
        'session.execution.failed','session.execution.interrupted','session.deleted'].includes(type))
        {const tracked=await trackChild(event,id,type);if(tracked&&!isObserved&&type==='session.execution.started')telemetry.observe(type,id,event?.properties??event?.data??{});}
      return;
    }
    const t=now();
    if(type==='session.execution.started'||(type==='session.status'&&event?.properties?.status?.type==='busy')){
      nativeIdleFor=null;s.lastStartedAt=t;s.lastSucceededAt=0;s.armedUntil=t+(lastConfig?.leaseMs??defaults.sessionLeaseHours*3600000);
      await save();return;
    }
    if(type==='session.execution.succeeded'){
      s.lastSucceededAt=t;await save();return;
    }
    // session.idle/status idle is not an authoritative execution result.
    if(type==='session.execution.interrupted'||type==='session.execution.failed'||type==='session.error'){
      nativeIdleFor=null;s.suspended=true;s.suspendedAt=t;s.pendingOwnerPrompt=false;s.lastSucceededAt=0;
      await save();
      await logEvent('suspended-after-execution-end',{sessionId:id,type,reason:eventReason(event)});
    }
    if(type==='session.deleted')await clear('session-deleted');
  }
  // Native plugin V2's experimental.idle hook exposes background work at an
  // actual primary idle boundary. It is a snapshot, not a liveness heartbeat.
  // Unknown job identifiers must not silently become 'no children'.
  async function onNativeIdle(event){
    if(disposed)return;
    if(event?.sessionID!==s.armedSessionId){if(event?.sessionID===reportRoot)await verifyReportBoundary(reportRoot);return;}
    if(!Array.isArray(event.background))return;
    nativeIdleFor={root:event.sessionID,started:s.lastStartedAt};
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
      const record=rememberChild(id);if(typeof job.jobID==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(job.jobID))record.jobID=job.jobID;
    }
    s.nativeOtherBackground=!complete;
    s.childCoverage=complete;
    s.childCoverageReason=complete?'native-idle-background':'unidentified-native-background';
    await save();
    if(lastConfig?.reportEnabled&&!lastConfig?.dryRun)void reporter.prewarm(event.sessionID,reportEpoch);
    await verifyReportBoundary(event.sessionID,{allowRepair:true});
  }
  // Optional inventory is useful on hosts exposing session.list, but that API
  // is NOT required by the current V2 Promise Plugin domain. For hosts without
  // it, use tracked child IDs plus a real idle snapshot. After an event gap,
  // unknown work remains UNKNOWN, not completed.
  let lastChildSkip='';
  async function ensureChildrenQuiescent(rootID){
    const lease=s;const rootStart=s.lastStartedAt;
    const children=new Set([...Object.keys(s.childSessions??{}),...runningChildren]);
    let inventoryComplete=false;
    if(typeof ctx.session.list==='function'){
      let cursor=null;const cursors=new Set();
      for(let pageNo=0;pageNo<12;pageNo++){
        const params=cursor?{cursor,limit:100}:{directory:repo,parentID:rootID,limit:100};
        let response;
        try{response=await bounded(ctx.session.list(params),10000,'list-children')}
        catch{return {ok:false,reason:'child-inventory-error'}}
        if(s!==lease||s.armedSessionId!==rootID||s.lastStartedAt!==rootStart)return {ok:false,reason:'child-probe-superseded'};
        const rows=Array.isArray(response)?response:
          (response?.items??response?.data?.items??response?.sessions??response?.data?.sessions??
          (Array.isArray(response?.data)?response.data:undefined));
        if(!Array.isArray(rows))return {ok:false,reason:'child-inventory-unrecognized'};
        for(const item of rows){
          if(typeof item?.id!=='string'||item.parentID!==rootID)
            return {ok:false,reason:'child-inventory-incomplete-or-mismatch'};
          children.add(item.id);const row=rememberChild(item.id);if(typeof item.agent==='string'&&/^(Builder|Reviewer|Architect|PM|DevOps|Memory|Arbiter)$/i.test(item.agent))row.role=item.agent;
        }
        const next=response?.cursor?.next??response?.data?.cursor?.next??null;
        if(!next){inventoryComplete=true;break;}
        if(cursors.has(next)||pageNo===11)return {ok:false,reason:'child-inventory-truncated'};
        cursors.add(next);cursor=next;
      }
    }
    const rosterUnverified=(!inventoryComplete&&!s.childCoverage)||s.nativeOtherBackground;
    const ids=[...children];
    if(rosterUnverified&&!ids.length)return {ok:false,reason:'child-roster-unverified'};
    if(typeof ctx.session.wait!=='function')return {ok:false,reason:'child-wait-unavailable'};
    const pending=[];const signals=[];let changed=false;
    for(let start=0;start<ids.length;start+=12){
      const results=await Promise.all(ids.slice(start,start+12).map(async id=>{
        const ac=new AbortController();const r=rememberChild(id);const state=r.state;const observedAt=r.at;
        try{await bounded(ctx.session.wait({sessionID:id},{signal:ac.signal}),1800,'child-wait',ac);
          return {id,ok:true,r,state,observedAt};}
        catch{return {id,ok:false,r,state,observedAt};}
      }));
      if(s!==lease||s.armedSessionId!==rootID||s.lastStartedAt!==rootStart)return {ok:false,reason:'child-probe-superseded'};
      for(const probe of results){
        const {id,r,state,observedAt}=probe;
        if(s.childSessions[id]!==r||r.state!==state||r.at!==observedAt){pending.push(id);continue;}
        if(probe.ok){
          if(!r.waitConfirmed||r.diagnosis||runningChildren.has(id)){
            const hadWork=state==='running'||r.everPending===true||!!r.diagnosis;
            r.waitConfirmed=true;delete r.diagnosis;runningChildren.delete(id);
            if(!terminalStates.has(state)&&state!=='missing'){r.state='quiescent';r.at=now();}
            if(hadWork){r.reconciledAt=now();await logEvent('child-quiescence-reconciled',{sessionId:id,parentID:rootID});}
            changed=true;
          }
          if((r.reconciledAt||['succeeded','failed','interrupted'].includes(r.state))&&r.state!=='missing'&&(Math.max(r.at??0,r.reconciledAt??0))>=s.lastStartedAt)signals.push({id,r,kind:'result-ready'});
          continue;
        }
        pending.push(id);
        if(!r.everPending){r.everPending=true;changed=true;}
        r.waitConfirmed=false;
        // Permission is a real owner dependency; unknown permission status is
        // never authority to cancel, fence or reassign this worker.
        const quiet=now()-(r.activityAt??r.at??now());
        const jobEnded=(r.jobAt??0)>=(r.startedAt??r.at??0)&&['completed','succeeded','failed','cancelled','canceled','interrupted'].includes(r.jobStatus);
        let diagnosis=(terminalStates.has(state)||jobEnded)?'terminal-unreconciled':quiet>=(lastConfig?.livenessSilenceMs??1800000)?'silence-unverified':null;
        if(diagnosis){
          try{const perms=await bounded(ctx.permission.list({sessionID:id}),3000,'child-permission');
            const n=safePermissionCount(perms);if(n===null)diagnosis='permission-unknown';else if(n>0)diagnosis='permission-wait';}
          catch{diagnosis='permission-unknown';}
          if(s!==lease||s.lastStartedAt!==rootStart||s.childSessions[id]!==r||r.state!==state||r.at!==observedAt)return {ok:false,reason:'child-probe-superseded'};
          if(diagnosis!==r.diagnosis){r.diagnosis=diagnosis;changed=true;await logEvent('child-liveness-anomaly',{sessionId:id,parentID:rootID,diagnosis,quietMs:Math.max(0,quiet)});}
          if(['terminal-unreconciled','silence-unverified'].includes(diagnosis))signals.push({id,r,kind:diagnosis});
        }
      }
    }
    if(changed){await save();if(reportCandidate===rootID&&lastConfig?.reportEnabled&&!lastConfig?.dryRun)void reporter.prewarm(rootID,reportEpoch);}
    if(rosterUnverified)return {ok:false,reason:'child-roster-unverified',childID:pending[0],signals};
    if(pending.length)return {ok:false,reason:'child-still-running-or-unknown',childID:pending[0],signals};
    return {ok:true,observed:ids.length,signals};
  }
  // Separate diagnostic handoff, not a second engineering scheduler. It may
  // notify an already active root of a result/anomaly while other lanes live.
  // It never edits plans, cancels a job or authorizes conflicting work.
  async function notifyLiveness(status,cfg,data,{stale=false}={}){
    const id=s.armedSessionId,ended=s.lastSucceededAt,started=s.lastStartedAt,lease=s;
    if(!id||!ended||ended<started||s.suspended||s.pendingOwnerPrompt||cfg.dryRun||typeof ctx.session.synthetic!=='function')return;
    if(now()-ended<cfg.minIdleMs||automaticPromptInFlight)return;
    const valid=()=>!disposed&&s===lease&&s.armedSessionId===id&&s.lastStartedAt===started&&s.lastSucceededAt===ended&&!s.suspended&&!s.pendingOwnerPrompt&&s.armedUntil>=now();
    const signals=(status.signals??[]).filter(x=>!s.livenessNotices?.[incidentKey(x.id,x.r,x.kind)]);
    // Wake once to reconcile independent ready work even if another child
    // remains live. This is not a dispatch or permission to touch its scope.
    const readyIds=certifiedReadyQueueIds(data.scope);
    const readyKey=readyIds.length&&status.reason==='child-still-running-or-unknown'
      ?crypto.createHash('sha256').update(JSON.stringify([id,'ready',readyIds,fingerprint(data.scope)])).digest('hex'):null;
    const readyNotice=readyKey&&!s.livenessNotices?.[readyKey];
    if(!signals.length&&!readyNotice)return;
    // A live sibling does not invalidate a completed sibling's result. But a
    // root still executing is never woken by diagnostics. Native idle is a
    // snapshot for this exact turn; otherwise require an independent wait.
    if(nativeIdleFor?.root!==id||nativeIdleFor.started!==started){
      const ac=new AbortController();try{await bounded(ctx.session.wait({sessionID:id},{signal:ac.signal}),3500,'liveness-root-wait',ac);}catch{return;}
    }
    if(!valid())return;
    const info=await bounded(ctx.session.get({sessionID:id}),10000,'liveness-root-get');
    if(!sessionMatches(info,id,repo,cfg.agent)||!valid())return;
    const permissions=await bounded(ctx.permission.list({sessionID:id}),10000,'liveness-root-permissions');
    if(safePermissionCount(permissions)!==0||!valid())return;
    const fresh=await eligibility();if(!fresh.decision.ok||fingerprint(fresh.scope)!==fingerprint(data.scope)||!valid())return;
    const stillCurrent=x=>s.childSessions[x.id]===x.r&&(x.kind==='result-ready'?x.r.waitConfirmed&&!x.r.diagnosis:x.r.diagnosis===x.kind);
    const current=signals.filter(stillCurrent);
    if(!current.length&&!readyNotice)return;
    // Recheck permissions of affected children immediately before admission.
    for(const x of current){try{if(safePermissionCount(await bounded(ctx.permission.list({sessionID:x.id}),3000,'liveness-child-permission'))!==0)return;}catch{return;}}
    if(!valid()||!current.every(stillCurrent))return;
    const latest=await eligibility();if(!latest.decision.ok||fingerprint(latest.scope)!==fingerprint(data.scope)||!valid()||!current.every(stillCurrent))return;
    const keys=[...current.map(x=>incidentKey(x.id,x.r,x.kind)),...(readyNotice?[readyKey]:[])].sort();
    s.livenessNotices??={};for(const k of keys)s.livenessNotices[k]=now();
    // Existing durable state, one notification per observed incident, not a
    // timer-driven stream of prompts. Keep notices across backend restarts.
    await save();if(!valid()||!current.every(stillCurrent))return;
    const requestID=`msg_${crypto.createHash('sha256').update('liveness:'+keys.join(':')).digest('hex').slice(0,24)}`;
    const details=[...current.map(x=>`${x.id}: ${x.kind}${typeof x.r.jobID==='string'?` (native job ${x.r.jobID})`:''}`),...(readyNotice?['В очереди есть независимые готовые записи; проверь реальные границы записи и разрешения перед назначением.']:[])].join('; ');
    try{
      await bounded(ctx.session.synthetic({sessionID:id,id:requestID,delivery:'queue',resume:true,text:`Служебная сверка состояния исполнителей (не поручение владельца): ${details}. Проверь нативное состояние сессии/фонового задания, последний результат и существующий Assignment. result-ready означает только завершение/покой исполнения, не VERIFIED и не подтверждение доставки. terminal-unreconciled — зафиксировано завершение хода или фонового задания, но wait не подтверждает покой; возможны фоновые инструменты. silence-unverified — нет наблюдаемой активности, это не доказательство зависания. Не отменяй по времени, не запускай дубликаты, не удаляй worktree. Если результат уже есть, используй его и обычное ревью. Если доказано, что конкретное фоновое задание осиротело и больше не выполняет никаких действий, используй только реально доступный нативный job API для закрытия; иначе сохрани Assignment и явно сообщи причину. Продолжи независимые готовые задачи только при доказанной непересекающейся области записи и соблюдении действующих разрешений и обязательной технической приёмки. Не жди неизвестного исполнителя, если независимая работа безопасна. Опубликуй обычный полный отчёт.`}),10000,'liveness-synthetic');
      await logEvent('child-liveness-notice-admitted',{sessionId:id,children:current.map(x=>x.id),stale});
    }catch(e){await logEvent('child-liveness-notice-unconfirmed',{sessionId:id,message:safeError(e)});}
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
      if(s.suspended||s.pendingOwnerPrompt||!s.lastSucceededAt||s.lastSucceededAt<s.lastStartedAt)return;
      // Check executable next transition FIRST. Merely having PENDING tasks or
      // an in_progress checkpoint no longer licenses an extra model turn.
      const data=await eligibility();
      if(!data.decision.ok){await clear(data.decision.reason);return}
      // The event cache alone CANNOT prove no child is active: it may miss a
      // child started before this plugin loaded or during a stream outage.
      const children=await skipForChildren(s.armedSessionId);
      if(!children.ok){await notifyLiveness(children,cfg,data);return;}
      if(data.decision.mode==='awaiting_children' && !children.observed){
        await logEvent('skip-missing-child-evidence',{sessionId:s.armedSessionId});return;
      }
      const sig=fingerprint(data.scope);
      const verdict=assessAttempt(s,sig,cfg,t);
      if(!verdict.ok){if(verdict.reason==='stale-idle'){await notifyLiveness(children,cfg,data,{stale:true});return;}if(verdict.reason==='no-progress-cap' && !s.noProgressLogged){
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
      s.childCoverage=false;s.childCoverageReason='backend-restarted';s.nativeOtherBackground=false;nativeIdleFor=null;
      s.childSessions=(s.childSessions&&typeof s.childSessions==='object'&&!Array.isArray(s.childSessions))?s.childSessions:{};
      s.livenessNotices=(s.livenessNotices&&typeof s.livenessNotices==='object'&&!Array.isArray(s.livenessNotices))?s.livenessNotices:{};
      delete s.mcpEventsSeen; delete s.reportRepair;
      await save();
    }
    if(cfg.reportEnabled&&typeof ctx.tool?.transform==='function'){
      try{reportRegistration=await ctx.tool.transform(editor=>editor.add({
        name:'workflow_report',description:'Read-only one-shot Reporter. Use after engineering dispatch and checkpoint, before every primary status/wait/release. Returns complete current Russian text immediately without waiting for model translation; publish the entire result as the only visible status reply, preserving every line break, metric and Markdown table row including receipt, at EVERY boundary, even for same revision. No separate Reporter session.',
        input:{type:'object',properties:{canonical_root:{type:'string',description:'Verified absolute canonical planning root, not a release/worker checkout'}},required:['canonical_root'],additionalProperties:false},options:{codemode:true},
        execute:async(input,toolContext)=>{
          if(typeof input.canonical_root!=='string'||!path.isAbsolute(input.canonical_root)||normalizePath(path.resolve(input.canonical_root))!==normalizePath(repo))throw Error('Canonical planning root differs from this session location; use the explicit verified canonical plan for a degraded/manual report, do not certify this checkout.');
          const cfg=await config();if(disposed||!cfg.enabled||!cfg.reportEnabled||cfg.dryRun)throw Error('Reporter unavailable');
          if(toolContext.sessionID!==reportCandidate||toolContext.sessionID!==reportRoot||s.suspended)throw Error('Reporter is restricted to verified primary session');
          const text=await reporter.generate(toolContext.sessionID,toolContext.signal,reportEpoch);
          return {content:[{type:'text',text}]};
        }
      }));}catch{await logEvent('report-tool-unavailable');}
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
          if(!disposed){telemetry.markGap();nativeIdleFor=null;s.childCoverage=false;s.childCoverageReason='event-stream-ended';
            await save();await logEvent('event-stream-ended');}
        }catch(e){
          if(!disposed){telemetry.markGap();nativeIdleFor=null;s.childCoverage=false;s.childCoverageReason='event-stream-error';
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
    await logEvent('plugin-loaded',{version:'5.0.1',sessionTracking:'model-context-and-execution-events'});
    return async()=>{
      disposed=true;ready=false;
      if(timer!==null)clearIntervalFn(timer);
      eventController.abort();
      await promptRegistration?.dispose?.();await contextRegistration?.dispose?.();
      await idleRegistration?.dispose?.();await reportRegistration?.dispose?.();reporter.reset();telemetry.reset();
    };
  }
  return {init,check,onEvent,modelContext,promptAdmission,getState:()=>({...s})};
}
