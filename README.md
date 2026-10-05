# Tablekeeper — Dark Factory Submission

This repository contains the completed output of an autonomous software factory built in Band Desktop for the WeAreDevelopers x BAND Dark Factory hackathon.

## Track

**Tablekeeper**

The factory completed **Stage 1** and **Stage 2** of the track.

## Factory

The factory uses three distinct coding-agent seats:

- **Coordinator** — decomposes the stage specification, dispatches work, coordinates handoffs, and manages the review loop.
- **Implementer** — implements the assigned specification, commits the resulting revision, and addresses review findings.
- **Reviewer** — independently verifies the implementation against the complete handoff specification and reports PASS/FAIL findings.

The agents communicate through a shared Band Desktop room.

## Submission Contents

- `FACTORY.md` — description of the factory architecture and workflow
- `mandates/` — generic seat mandates
- `room.json` — recorded Band room session
- `stage-1/` — completed Stage 1 service
- `stage-2/` — completed Stage 2 service

## Development Process

Each stage is dispatched through the coordinator to the implementer with the complete stage specification. The implementer produces and commits the implementation, then hands the complete revision to the reviewer. The reviewer independently checks the implementation against the requirements and reports findings. Review findings are sent back for correction when necessary.

The factory run proceeds sequentially from one stage to the next.

## Running a Stage

Each completed stage contains its own `Dockerfile` and `RUN.md` with the instructions required to build and run that stage.

The submitted stages are the result of the autonomous factory workflow recorded in `room.json`.
