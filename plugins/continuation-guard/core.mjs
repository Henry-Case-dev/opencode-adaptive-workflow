/** Deterministic decisions. No user-message parsing, project allowlists or hardcoded paths. */
export const TERMINAL = new Set(['VERIFIED', 'DONE', 'COMPLETED', 'CANCELLED', 'USER_EXCLUDED', 'N/A_WITH_REASON']);
export const PASSIVE = new Set(['WAITING_EXTERNAL', 'BLOCKED_REAL_HUMAN_GATE', 'BLOCKED', 'WAITING_OWNER']);

export function normalizePath(value) {
  const raw = String(value ?? '').trim().replaceAll('\\', '/').replace(/\/+$/, '');
  return /^[a-zA-Z]:\//.test(raw) ? raw.toLowerCase() : raw;
}
/** Machine fields are taken from the current compact header, never append-only history. */
export function readMachineHeader(workflowText) {
  if (typeof workflowText !== 'string') return {ok:false,reason:'checkpoint-unavailable'};
  // The compact header precedes the first second-level section; historical
  // notes below it are not machine state. An older flat checkpoint still works.
  const header=workflowText.split(/\r?\n(?=##\s)/,1)[0];
  const fields={};
  for (const line of header.split(/\r?\n/)) {
    const match=line.match(/^(Status|Human gate|Next execution):[ \t]*([^\r\n]*)$/i);
    if(!match)continue;
    const key=match[1].toLowerCase();
    (fields[key]??=[]).push(match[2].trim());
  }
  const machineValue = raw => {
    if (typeof raw !== 'string') return null;
    const match=raw.match(/^([a-z][a-z0-9_-]*)(?:[ \t]*\([^()\r\n]*\))?$/i);
    return match?match[1].toLowerCase():null;
  };
  // A second Human gate is never safe to reconcile automatically; even if
  // both say 'none', competing writers violated the checkpoint contract.
  if((fields['human gate']?.length??0)!==1)
    return {ok:false,reason:(fields['human gate']?.length??0)>1?'human-gate-duplicate':'human-gate'};
  const human=machineValue(fields['human gate'][0]);
  const scoped=fields['human gate'][0].match(/^task\s*\(([^()\r\n]+)\)$/i);
  if(human!=='none'&&!scoped)return {ok:false,reason:'human-gate'};
  const statuses=(fields.status??[]).map(machineValue);
  const executions=(fields['next execution']??[]).map(machineValue);
  const knownStatuses=new Set(['in_progress','done','waiting_external','blocked']);
  const knownExecutions=new Set(['runnable','awaiting_children','none']);
  // A valid ready task cannot authorize guesses about missing or malformed
  // machine fields. Duplicate *known* values are a repairable old snapshot.
  if(!statuses.length||statuses.some(x=>!knownStatuses.has(x)))
    return {ok:false,reason:'workflow-not-active'};
  if(!executions.length||executions.some(x=>!knownExecutions.has(x)))
    return {ok:false,reason:'next-execution-missing-or-invalid'};
  return {ok:true,human,gateTask:scoped?.[1].trim()??null,status:statuses.length===1?statuses[0]:null,
    nextExecution:executions.length===1?executions[0]:null,
    inconsistent:statuses.length!==1||executions.length!==1,
    duplicates:{status:statuses.length,execution:executions.length}};
}

const ACTIONABLE=new Set(['PENDING','PLANNED','READY','PREPARING','BUILDING','IN_PROGRESS',
  'REVIEW','REVIEWING','DELIVERY','DEPLOYING','READY_FOR_LIVE','READY_TO_SHIP','FAILED',
  'NEEDS_FIXES','TO_DO','TODO']);
const NEW_READY=new Set(['PENDING','PLANNED','READY','PREPARING','READY_TO_SHIP','NEEDS_FIXES','TO_DO','TODO']);
const LEGACY_CLOSED=new Set(['VERIFIED','COMPLETED']);
const excluded=new Set(['CANCELLED','USER_EXCLUDED','N/A_WITH_REASON']);
const statusOf=r=>String(r?.status??'').trim().toUpperCase();
const nonempty=v=>typeof v==='string'&&!!v.trim();
// Compact closure is a recorded historical delivery, not a current runtime probe.
// Evidence is validated at close; neither current HEAD nor mutable plan hashes are inputs.
export function deliveryRecorded(row){
  const d=row?.delivery;
  if(!d||typeof d!=='object'||Array.isArray(d)||!['deployment','non_deployment'].includes(d.kind))return false;
  if(typeof d.candidate!=='string'||!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(d.candidate))return false;
  const ref=x=>typeof x==='string'&&/^(?:[0-9a-f]{40}|[0-9a-f]{64}):[^\r\n]+$/i.test(x);
  if(!ref(d.review_ref)||!ref(d.delivery_ref)||!nonempty(d.result))return false;
  if(d.kind==='deployment' ? d.deployed_commit!==d.candidate : d.deployed_commit!=null)return false;
  const deferred=Object.hasOwn(d,'deferred_req_ids')?d.deferred_req_ids:[];
  return Array.isArray(deferred)&&deferred.every(nonempty)&&new Set(deferred).size===deferred.length;
}
export function closed(row){return LEGACY_CLOSED.has(statusOf(row))||(statusOf(row)==='DONE'&&deliveryRecorded(row));}
const priorityOf=v=>typeof v==='string'&&/^P[0-9]$/i.test(v)?Number(v[1]):Number.isInteger(v)&&v>=0&&v<=9?v:null;
const posOf=v=>Number.isSafeInteger(v)&&v>0?v:Number.MAX_SAFE_INTEGER;
export function taskIndex(scope){
  const ids=new Map(),duplicates=new Set();
  for(const row of Array.isArray(scope?.tasks)?scope.tasks:[]){
    if(!row||!nonempty(row.id))continue;
    if(ids.has(row.id))duplicates.add(row.id);else ids.set(row.id,row);
  }
  for(const id of duplicates)ids.delete(id);
  return ids;
}
function intakeTargets(scope){
  const inputs=Array.isArray(scope?.owner_inputs)?scope.owner_inputs:[];
  return new Set(inputs.filter(x=>x?.reconciled===false&&Array.isArray(x.target_task_ids)).flatMap(x=>x.target_task_ids));
}
/** Local quarantine: malformed records/edges never license dispatch of that task.
 * Position is ordering metadata, not a permission or global execution gate. */
export function certifiedReadyQueueIds(scope){
  if(scope?.schema!=='openworkflow-progress-scope-v1'||!Array.isArray(scope.tasks))return [];
  const ids=taskIndex(scope),pending=intakeTargets(scope),ready=[];
  for(const row of ids.values()){
    const status=statusOf(row),priority=priorityOf(row.priority);
    if(!NEW_READY.has(status)||priority===null||pending.has(row.id))continue;
    if(!Array.isArray(row.blocked_by)||new Set(row.blocked_by).size!==row.blocked_by.length)continue;
    if(row.blocked_by.some(id=>!nonempty(id)||pending.has(id)||!closed(ids.get(id))))continue;
    if(['delivered_awaiting_evidence','waiting_external','blocked','running','assigned'].includes(String(row.execution_state??'').toLowerCase()))continue;
    ready.push({id:row.id,priority,position:posOf(row.queue_position)});
  }
  ready.sort((a,b)=>a.priority-b.priority||a.position-b.position||(a.id<b.id?-1:a.id>b.id?1:0));
  return ready.map(x=>x.id);
}
export function evaluateWork(scope, workflowText) {
  if (!scope || scope.schema !== 'openworkflow-progress-scope-v1' || !Array.isArray(scope.tasks))
    return {ok:false,reason:'missing-or-empty-workflow-scope'};
  const header=readMachineHeader(workflowText);
  if(!header.ok)return {ok:false,reason:header.reason};
  const ids=taskIndex(scope),certifiedReady=certifiedReadyQueueIds(scope);
  const pending=(Array.isArray(scope.owner_inputs)?scope.owner_inputs:[]).some(x=>x?.reconciled===false&&nonempty(x.id));
  if(header.gateTask){
    const gateRow=ids.get(header.gateTask);
    if(!gateRow||!PASSIVE.has(statusOf(gateRow))||!certifiedReady.length||certifiedReady.includes(header.gateTask))return {ok:false,reason:'human-gate'};
  }
  const rescueHandoff=(certifiedReady.length>0||pending&&!header.gateTask)&&
    (header.inconsistent||header.status!=='in_progress'||header.nextExecution!=='runnable'||!!header.gateTask);
  if(header.inconsistent&&!rescueHandoff)return {ok:false,reason:'checkpoint-ambiguous-no-certified-ready-work'};
  if(header.status==='done'&&!rescueHandoff)return {ok:false,reason:'workflow-not-active'};
  if(header.nextExecution==='none'&&!rescueHandoff)return {ok:false,reason:'no-runnable-transition'};
  const count=[...ids.values()].filter(r=>ACTIONABLE.has(statusOf(r))||statusOf(r)==='DONE'&&!closed(r)).length;
  if(!count&&!pending)return {ok:false,reason:'no-actionable-work'};
  return {ok:true,reason:rescueHandoff?'queue-handoff-needed':'actionable-work',count,
    mode:rescueHandoff?'runnable':header.nextExecution,
    ...(rescueHandoff?{certifiedReady}:{}),...(header.gateTask?{gateTask:header.gateTask}:{}),
    ...(header.inconsistent?{checkpointInconsistent:true}:{})};
}
export function sessionMatches(info,id,repo,agentId) {
  if (!info || info.id !== id || info.parentID) return false;
  if (typeof info.agent === 'string' && info.agent.toLowerCase() !== agentId.toLowerCase()) return false;
  const loc = info.directory ?? info.location?.directory;
  return !loc || normalizePath(loc) === normalizePath(repo);
}
export function eventSessionId(event) {
  const p = event?.properties ?? event?.data ?? {};
  return p.sessionID ?? p.sessionId ?? p.info?.id ?? event?.sessionID ?? null;
}
export function eventReason(event) {
  const p = event?.properties ?? event?.data ?? {};
  return p.reason ?? p.error?.type ?? null;
}
export function safePermissionCount(data) {
  if (Array.isArray(data)) return data.length;
  if (Array.isArray(data?.data)) return data.data.length;
  if (Array.isArray(data?.items)) return data.items.length;
  return null;
}
export function fingerprint(scope) {
  // Deliberately exclude workflow_state.md, Git HEAD, timestamps and reports:
  // prose checkpoints and documentation commits are NOT proof that real work
  // advanced. Only actual task/intake transitions reset the retry budget.
  const tasks = [...taskIndex(scope).values()].map(t => [t.id,statusOf(t),closed(t),t.execution_state??null,t.delivery?.candidate??null,t.candidate_commit??null,Array.isArray(t.blocked_by)?[...t.blocked_by].sort():null]).sort((a,b)=>a[0]<b[0]?-1:a[0]>b[0]?1:0);
  const admitted = certifiedReadyQueueIds(scope).sort();
  const inputs = (Array.isArray(scope.owner_inputs) ? scope.owner_inputs : [])
    .map(i => `${i?.id || ''}:${i?.reconciled === true ? 'reconciled' : 'open'}`).sort();
  return JSON.stringify([tasks,inputs,admitted]);
}
export function assessAttempt(state, sig, cfg, now) {
  if (!state?.armedSessionId || state.suspended || !state.lastSucceededAt || !state.lastStartedAt)
    return {ok:false, reason:'unarmed-or-not-succeeded'};
  if (state.lastSucceededAt < state.lastStartedAt) return {ok:false,reason:'execution-running'};
  if (state.lastSucceededAt > now+5000 || now-state.lastSucceededAt > cfg.maxIdleAgeMs)
    return {ok:false,reason:'stale-idle'};
  if (now-state.lastSucceededAt < cfg.minIdleMs) return {ok:false,reason:'idle-grace'};
  if (state.lastAttemptForEnd === state.lastSucceededAt) return {ok:false,reason:'same-completion-already-resumed'};
  if (now-(state.lastAttemptAt||0) < cfg.cooldownMs) return {ok:false,reason:'cooldown'};
  const repeat = state.signature === sig ? Number(state.attempts)||0 : 0;
  if (repeat >= cfg.maxAttemptsWithoutProgress) return {ok:false,reason:'no-progress-cap'};
  return {ok:true,repeat};
}
export function positiveInt(value, fallback,min,max) {
  return Number.isFinite(Number(value)) ? Math.max(min, Math.min(max,Math.round(Number(value)))) : fallback;
}
