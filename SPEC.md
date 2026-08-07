# The Agent Estate Specification

**Version 1 · protocol name `ecom` · transport: Model Context Protocol over HTTP**

This document is the project. The server in `server/` is one implementation of
it; write your own if you prefer. A registry is conformant if it exposes the
tool surface in §4 and enforces every law in §2 *in the store*, not in a prompt.

---

## 1. What this is

A registry an autonomous agent writes to and reads from, so that an estate of
software can be run without human workers.

Agents are transient: they have no memory between sessions, they are replaced
by better models, and they lie about what they did unless the record makes
lying inconvenient. The registry is the opposite: it remembers nothing else.
Every design decision below follows from that asymmetry.

The registry does **not** schedule work, execute work, store secrets, or
authorise anything. It records.

---

## 2. The seven laws

A conformant implementation enforces these at write time. If a law is only
described in documentation or a system prompt, the implementation is not
conformant — an agent that has read no prompt must still be unable to break it.

**I. Nothing enters twice.**
Every signal carries a caller-supplied `dedupe_key`, unique per project.
Writing the same key again updates the existing row. Re-ingesting the same
week of data is therefore always safe and never double-counts.

**II. The record is appended to, never overwritten.**
`log_result` inserts a new row per call; a task that failed four times before
succeeding retains five results. `append_env_note` prepends a timestamp and
never replaces prior notes.

**III. Nothing runs on an agent's own say-so.**
A task's plan runs only after `approve_plan`. That tool is the human gate and
its documentation must say so explicitly. Re-submitting a plan voids any
approval it already carried.

**IV. Work cannot wait upon itself.**
`add_task_dependency` rejects any edge that would close a cycle. The dependency
graph is therefore a DAG and the ready set is always computable.

**V. A claim names its evidence.**
Findings carry `evidence_signal_ids`. `severity` (how much is being lost now)
and `confidence` (how directly it was measured) are separate 1–5 fields and are
never conflated. A finding with no evidence is admitted — its confidence is the
confession.

**VI. A refusal is recorded as carefully as an approval.**
`decide_finding` accepts `accepted`, `rejected`, `superseded`, `resolved`.
`rejected` and `resolved` require a `note`. Without the reason, the same bad
idea returns next week, proposed by an agent with no memory of the last one.

**VII. No secret enters the record.**
`env_notes` and signal-source `config` are plain text and documented as plain
text. Addressing only: bundle identifiers, property identifiers, hostnames you
would put on a business card. Credentials live in the process environment or
nowhere.

---

## 3. Data model

Seven tables. SQLite in the reference implementation; nothing depends on that.

### 3.1 `projects`
| column | type | notes |
|---|---|---|
| `id` | integer pk | |
| `name` | text unique | the handle every other tool takes |
| `type` | text | `mobile_app` · `web_app` · `website` · `internal_tool` · `ai_factory` |
| `description` | text null | |
| `github_repo` / `github_branch` | text null | branch defaults to `main` |
| `firebase_project_id` | text null | addressing only |
| `flutterflow_project_id` | text null | addressing only |
| `backend_host` / `backend_path` / `backend_url` | text null | |
| `latest_commit_sha` / `latest_commit_at` | text / timestamp null | |
| `env_notes` | text null | **append-only**, timestamped (law II) |
| `status` | text | `active` · `archived` |
| `created_at` / `updated_at` | timestamp | |

### 3.2 `agents`
| column | type | notes |
|---|---|---|
| `id` | integer pk | |
| `name` | text unique | re-registering the same name updates it |
| `role` | text | free text: `engineer`, `content strategist`, … |
| `model` | text null | the model it runs on |
| `capabilities` | json array | tags: `["infra","devops","security"]` |
| `status` | text | `idle` · `working` · `disabled` |
| `last_seen_at` | timestamp | set by `heartbeat_agent`; distinguishes working from stalled |

### 3.3 `tasks`
| column | type | notes |
|---|---|---|
| `id` | integer pk | |
| `project_id` / `agent_id` | fk | project optional, agent required |
| `title` | text null | |
| `input` | json object | the assignment |
| `plan` | text null | set by `submit_plan` |
| `plan_approved_at` / `plan_approved_by` | timestamp / text null | cleared on re-submit (law III) |
| `status` | text | `pending` · `running` · `succeeded` · `failed` · `cancelled` |
| `created_at` / `started_at` / `completed_at` | timestamp null | |

### 3.4 `task_results`
Append-only (law II). `id`, `task_id`, `status`, `output`, `error`,
`meta` (free-form json, e.g. `{"tokens":1200,"cost":0.03}`), `created_at`.

### 3.5 `task_dependencies`
`task_id`, `blocked_by`, unique pair. Insert is rejected if the edge closes a
cycle (law IV). Removal is idempotent.

### 3.6 `signals`
| column | type | notes |
|---|---|---|
| `id` | integer pk | |
| `project_id` | fk | |
| `kind` | text | `release` · `revenue` · `store_review` · `cost` · `quality` · `capacity` · `deployment` · `workflow` · … open vocabulary |
| `metric` | text null | |
| `value` | text null | text, so `"REJECTED"` and `"0.394"` live in one column |
| `payload` | json null | everything the source gave you |
| `dedupe_key` | text | unique per project (law I) |
| `window_start` / `window_end` / `observed_at` | timestamp | |

`signal_sources`: `project_id`, `kind` (`crashlytics` · `analytics` ·
`store_reviews` · `backend_logs` · `manual`), `config` json (**addressing
only**, law VII), `enabled`.

### 3.7 `findings`
| column | type | notes |
|---|---|---|
| `id` | integer pk | |
| `project_id` | fk | |
| `kind` | text | `design` · `technical` · `product` |
| `area` | text null | free text |
| `title` / `detail` / `proposal` | text | |
| `severity` / `confidence` | int 1–5 | separate axes (law V) |
| `evidence_signal_ids` | json array | |
| `status` | text | `open` · `accepted` · `rejected` · `superseded` · `resolved` |
| `decided_by` / `decided_at` / `decision_note` | text / ts / text null | note required for `rejected` and `resolved` (law VI) |
| `superseded_by_id` | fk null | |

---

## 4. Tool surface

Thirty-one tools. There is no other way into the store — no REST side door, no
direct SQL for agents.

### Estates — 6
| tool | purpose |
|---|---|
| `list_projects` | filter by `status` or `type` |
| `get_project` | one holding, all registry fields |
| `create_project` | register a holding |
| `update_project` | amend registry fields |
| `append_env_note` | timestamped note, never overwrites (law II) |
| `set_latest_commit` | record the last known commit sha |

### Agents — 4
| tool | purpose |
|---|---|
| `register_agent` | create or update by name |
| `list_agents` | filter by status |
| `set_agent_status` | `idle` · `working` · `disabled` |
| `heartbeat_agent` | prove aliveness; updates `last_seen_at` |

### Tasks — 10
| tool | purpose |
|---|---|
| `assign_task` | agent + json input + optional project and title |
| `get_task` | one task with the full history of result attempts |
| `list_tasks` | filter by status, project, agent |
| `ready_tasks` | pending tasks whose blockers have all succeeded |
| `submit_plan` | attach a plan; voids prior approval (law III) |
| `approve_plan` | **the human gate**; call on a person's instruction only |
| `log_result` | append an outcome and move the task to that status |
| `add_task_dependency` | rejected if it closes a cycle (law IV) |
| `remove_task_dependency` | idempotent |
| `task_blockers` | tasks still standing in the way |

### Signals — 4
| tool | purpose |
|---|---|
| `record_signal` | one observation; `dedupe_key` required (law I) |
| `list_signals` | newest first, filter by kind or project |
| `register_signal_source` | declare where signals come from |
| `list_signal_sources` | per project or all |

### Findings — 5
| tool | purpose |
|---|---|
| `create_finding` | severity, confidence, proposal, evidence ids |
| `get_finding` | one finding with its evidence |
| `list_findings` | ranked worst-first by severity then confidence |
| `add_finding_evidence` | link an existing signal to an existing finding |
| `decide_finding` | accept, reject, supersede, resolve (law VI) |
| | |

### The estate itself — 2
| tool | purpose |
|---|---|
| `system_status` | counts by status across every chamber |
| `create_backup` | immediate snapshot; push to a git remote if configured |

---

## 5. Transport and authentication

MCP over HTTP. One static bearer token per estate.

```json
{
  "mcpServers": {
    "ecom": {
      "type": "http",
      "url": "https://<your-host>/ecom/mcp/",
      "headers": { "Authorization": "Bearer <your token>" }
    }
  }
}
```

The token is the whole authorisation model: **one token, one estate, no roles**.
This is a deliberate limit, not an unfinished feature — see `SECURITY.md`.
Rotation has an ordering constraint: change it on the server, restart, then
change it in the client. An agent that loses the connection mid-session cannot
record what it did.

---

## 6. The cycle

The chambers are one chain, and the chain is the point:

```
  measured        read              decided           planned      gated      run
observation  →  signal  →  finding  →  accepted  →  task  →  approve_plan  →  result
                    ↖______ evidence_signal_ids ______↙                          │
                                                                     appended, never replaced
```

Each link is enforced rather than encouraged: an evidence-free finding must
confess in its confidence field; a task whose blockers have not succeeded is
simply not returned by `ready_tasks`; a rejection without a note is refused.

---

## 7. Conformance

An implementation may claim conformance with **Agent Estate v1** if:

1. All 31 tools in §4 exist with the stated semantics.
2. Each of the seven laws is enforced by the store, verifiably: writing a
   duplicate `dedupe_key` produces one row; a cyclic dependency is rejected;
   `decide_finding(status="rejected")` without a note fails.
3. No tool returns credentials, and no documented field is intended to hold one.
4. `system_status` answers without arguments.

Extensions are welcome. Removing a law is a fork, not a version.
