/** Deterministic decisions. No user-message parsing, project allowlists or hardcoded paths. */
export const TERMINAL = new Set(['VERIFIED', 'DONE', 'COMPLETED', 'CANCELLED', 'USER_EXCLUDED', 'N/A_WITH_REASON']);
export const PASSIVE = new Set(['WAITING_EXTERNAL', 'BLOCKED_REAL_HUMAN_GATE', 'BLOCKED', 'WAITING_OWNER']);

export function normalizePath(value) {
  const raw = String(value ?? '').trim().replaceAll('\\', '/').replace(/\/+$/, '');
  return /^[a-zA-Z]:\//.test(raw) ? raw.toLowerCase() : raw;
}
export function evaluateWork(scope, workflowText) {
  if (!scope || scope.schema !== 'openworkflow-progress-scope-v1' || !Array.isArray(scope.tasks) || !scope.tasks.length)
    return {ok:false, reason:'missing-or-empty-workflow-scope'};
  if (scope.tasks.some(t => !t || typeof t.id !== 'string' || !t.id.trim() || typeof t.status !== 'string'))
    return {ok:false, reason:'invalid-task-status'};
  // `workflow_state.md` allows parenthesized human explanation after the
  // machine-readable status, e.g. `Status: in_progress (work remains)`.
  // Parse only that narrow grammar. Do NOT accept arbitrary trailing text:
  // ambiguous or malformed statuses must fail closed, not trigger a resume.
  const statusLine = workflowText.match(/^Status:[ \t]*([^\r\n]+)/mi)?.[1];
  const humanLine = workflowText.match(/^Human gate:[ \t]*([^\r\n]+)/mi)?.[1];
  const machineValue = raw => {
    if (typeof raw !== 'string') return null;
    const match = raw.trim().match(/^([a-z][a-z0-9_-]*)(?:[ \t]*\([^()\r\n]*\))?$/i);
    return match ? match[1].toLowerCase() : null;
  };
  const status = machineValue(statusLine);
  if (status !== 'in_progress') return {ok:false,reason:'workflow-not-active'};
  const human = machineValue(humanLine);
  if (human !== 'none') return {ok:false,reason:'human-gate'};
  // Activity is an execution fact, not an inventory fact. A PENDING requirement
  // may be waiting for an external event; it does NOT authorize a new LLM turn.
  // This optional-for-legacy-state field is mandatory for guarded auto-resume.
  // It is machine metadata, not a trigger phrase in the user's text.
  const executionLine = workflowText.match(/^Next execution:[ \t]*([^\r\n]+)/mi)?.[1];
  const nextExecution = machineValue(executionLine);
  if (!['runnable','awaiting_children','none'].includes(nextExecution))
    return {ok:false,reason:'next-execution-missing-or-invalid'};
  if (nextExecution === 'none') return {ok:false,reason:'no-runnable-transition'};
  const statuses = scope.tasks.map(t=>t.status.trim().toUpperCase());
  // Fail closed on unknown statuses: a new workflow contract needs explicit review.
  const actionable = new Set(['PENDING','PLANNED','READY','PREPARING','BUILDING','IN_PROGRESS','REVIEW','REVIEWING','DELIVERY','DEPLOYING','READY_FOR_LIVE','FAILED','NEEDS_FIXES','TO_DO','TODO']);
  if (statuses.some(s => !TERMINAL.has(s) && !PASSIVE.has(s) && !actionable.has(s)))
    return {ok:false,reason:'unknown-task-status'};
  const count = statuses.filter(s=>actionable.has(s)).length;
  if (!count) return {ok:false,reason:'no-actionable-work'};
  return {ok:true,reason:'actionable-work',count,mode:nextExecution};
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
  const tasks = scope.tasks.map(t => `${t.id}:${String(t.status).trim().toUpperCase()}`).sort();
  const inputs = (Array.isArray(scope.owner_inputs) ? scope.owner_inputs : [])
    .map(i => `${i?.id || ''}:${i?.reconciled === true ? 'reconciled' : 'open'}`).sort();
  return JSON.stringify([tasks,inputs]);
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
