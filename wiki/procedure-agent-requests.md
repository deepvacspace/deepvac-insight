---
title: Approve or reject an agent's request
type: procedure
tags: [procedure, approval, agent requests, worker, human in the loop]
sources: [insight app/agent_requests_dialog.py, insight app/services/agent_requests_service.py, insight app/api/executor.py, insight app/agent_chat.py]
updated: 2026-10-24
---
# Approve or reject an agent's request

How chamber actions asked for by the AI agents reach the chamber. Nothing the agents ask for is
sent until a person approves it ([[limits-and-safety]]).

## The steps

1. The worker files a request (set a temperature, run a program, stop) with its reason. It
   appears in **Agent Requests** (gear menu; the dialog opens by itself when one arrives).
2. Read the action and the reason; check the chamber (Controller page, Live tab).
3. **Approve** (Insight asks to confirm: "Send this to the chamber now?") or **Reject**. You can
   write why; the agent sees it.
4. An approved request is sent to the PLC within a second; its status becomes *executed* (or
   *failed*, with the reason). For a setpoint or program, Insight then follows the job until it
   ends (finished, stopped, alarm or replaced).
5. Undecided requests expire after 30 minutes.

## What the agent sees

The worker waits up to 2 minutes for your decision. If you decide later, or when the job ends,
Insight posts an update into the same chat and the Professor tells the user what came of it.

## When to reject

The chamber is busy with something else, the reason doesn't hold, the values look wrong for
the DUT, or nobody is around to watch the chamber.
