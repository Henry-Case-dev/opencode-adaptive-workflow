# Acceptance and delivery

Two substantive checkpoints: independent approval of actual candidate, then actual authorized delivery (or completed non-deployment result). For material risk use necessary extra technical controls, not more bureaucracy. Existing explicit project-required release/CI/safety checks remain binding. `requirements_gate.py check --stage plan|review|deploy|delivery|release` and old whole-map receipts remain compatible for explicit mapped-contract diagnosis or project-required gates, not routine global process. Do not create new maps or receipts merely to satisfy history. Review one cohesive feature, re-review corrections/affected risk; do not invalidate old proof due to unrelated HEAD or document formatting. Verify current candidate/contract/environment applicability before reuse; no rebinding SHA to conceal mismatch.

Record one compact actual evidence artifact (existing review/deploy notes/logs may be reused), with exact candidate, contract scope, independent verdict, checks/results and actual deployed identity or delivered artifact. DevOps handles required immediate smoke/rollback; no DONE for failed deploy, disabled required feature, real defect, partial epic or inaccessible essential safety/correctness proof. If safe functionality is delivered and only independently approved nonblocking future external proof remains, record exact remaining IDs and reason separately. Optional quality observation is not acceptance. Use authorized sandbox; no human routine QA. Keep owner policy, risk and authorization intact.

For new DONE entries Orchestrator records the existing task row's `delivery` object:
```
{
  "kind": "deployment",
  "candidate": "<full reviewed release Git SHA>",
  "deployed_commit": "<same full SHA observed on target>",
  "review_ref": "<full Git SHA>:<existing review evidence path>",
  "delivery_ref": "<full Git SHA>:<existing delivery evidence path>",
  "result": "<plain actual user-visible result>",
  "deferred_req_ids": []
}
```
`kind: non_deployment` uses a real reviewed commit/artifact result; deployed_commit null/omitted, no fake server. Evidence refs point to immutable Git blobs, not changing working copies. For non-Git external artifacts preserve actual evidence in the existing project Git record; do not commit secrets. Use `requirements_gate.py delivery-record --repo <root> --candidate <sha> --kind deployment --deployed-commit <observed sha> --review-ref <ref:path> --delivery-ref <ref:path> --result <summary>` optionally to resolve and structurally validate refs. It does NOT prove independent approval/runtime semantics: verify actual artifacts and live delivery. `--deferred-req-id` only for explicitly independent nonblocking proof recorded in real evidence; no mandatory extra report or command if refs already verified. Record DONE only after both checkpoints cover whole task. If task remains partly delivered use existing open row and compact incremental evidence, not DONE. `workflow_progress` and Guard read recorded history; no Git/network revalidation on every report. A concrete regression/rollback/false claim reopens affected scope; formatting, version bump or unrelated release does not. Historical VERIFIED/COMPLETED remains historical, not freshly certified; never generate new receipts just to raise percentage.
