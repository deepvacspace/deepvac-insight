---
title: Run a test profile (program)
type: procedure
tags: [procedure, program, test profile, intervals, cycles, controller]
sources: [insight app/plc_control.py, insight app/test_profiles_dialog.py, insight app/services/plc_programs.py, insight app/api/worker_tools.py]
updated: 2026-10-24
---
# Run a test profile (program)

How to run a sequence of temperatures as one PLC program. Profiles and intervals are explained
in [[programs-and-jobs]].

## By hand (Insight, Controller page)

1. Create or edit the profile with **Manage Test Profiles** (steps of setpoint and duration).
2. Select the chamber PLC and, under **Program**, the profile. Insight lists its intervals
   (mode: **Ramp to** or **Go to and hold**, temperature, duration), the cycles and the total
   length. Profiles with a step that has no temperature can't run on the PLC.
3. Press **Start Program**. If a job is running Insight asks before replacing it.
4. While it runs: **Next Interval** ends the current interval and goes on; **Stop** ends the
   job (asks first).
5. When it ends the status becomes *finish*: press **Confirm Finish** so the chamber returns to
   idle ([[procedure-alarm-and-finish]]).

## Through the crew

"Run the 'Soak 40' program, reason: qualification". The worker checks that the profile exists
and that every temperature is within the limits, then files `request_program`; you approve it in
Agent Requests ([[procedure-agent-requests]]). When the job ends Insight tells the crew, and the
finished job is saved as a run for the analyst.
