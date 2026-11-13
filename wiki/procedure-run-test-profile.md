---
title: Run a test profile (program)
type: procedure
tags: [procedure, program, test profile, intervals, cycles, controller]
sources: [insight app/views/controller.py, insight app/program_library.py, insight app/test_profiles_dialog.py, insight app/services/plc_programs.py, insight app/api/worker_tools.py]
updated: 2026-10-24
---
# Run a test profile (program)

How to run a sequence of temperatures as one PLC program. Profiles and intervals are explained
in [[programs-and-jobs]].

## By hand (Insight, Controller page)

The page works like Deepvac Control's Control page: the experiment in three steps on the left,
the program's profile on the right with its length and when it would end.

1. Select the chamber PLC (top right of the page).
2. Step 1, **Experiment details**: a name or purpose and the operator (required), the DUT and its
   mass and density. They are recorded with the run.
3. Step 2, **Intervals**: **Load from library** opens a saved program (also those shared by
   paired computers; a profile with steps only loads as one "go to and hold" interval per step).
   Or build it: **Add** an interval, open a card to set its label, variable, setpoint, mode
   (full speed then hold, linear ramp, custom ramp rate) and duration; **Duplicate**, **Then
   hold** (after a ramp) and **Delete**. **Cycle → Edit** repeats a range of intervals (or
   forever). **Save to library** keeps it by name, with a label per interval. Every interval
   needs a setpoint.
4. Step 3, **Confirm and start**: press **Start**, or **Delay start** for a later time.
5. While it runs only step 3 is open: **Next interval** ends the current interval and goes on;
   **Stop** ends the job (asks first).
6. When it ends the status becomes *finish*: press **Confirm finish** (step 3) so the chamber
   returns to idle ([[procedure-alarm-and-finish]]).

## Through the agent

"Run the 'Soak 40' program, reason: qualification". The worker checks that the profile exists
and that every temperature is within the limits, then files `request_program`; you approve it in
Agent Requests ([[procedure-agent-requests]]). When the job ends Insight tells the agent, and the
finished job is saved as a run for the analyst.
