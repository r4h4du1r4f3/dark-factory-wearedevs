# Factory Description

## Overview

This project is an autonomous three-seat software factory operated through Band Desktop.

The factory separates coordination, implementation, and independent verification into distinct agent seats. The seats communicate through a shared room and pass concrete implementation state between one another.

The factory is designed to execute a stage specification without human steering, clarification, manual coding, or approval during the submitted run.

## Seats

### Coordinator

The Coordinator owns orchestration.

Responsibilities:

1. Read the current stage specification.
2. Determine the required implementation and verification sequence.
3. Dispatch a complete, self-contained specification to the Implementer.
4. Receive the implementation handoff.
5. Dispatch the complete implementation state and requirements to the Reviewer.
6. Process review findings.
7. Send required corrections back to the Implementer.
8. Repeat implementation/review until the stage is accepted.
9. Dispatch the next stage only after the current stage has completed successfully.

The Coordinator does not implement application code.

### Implementer

The Implementer owns application implementation.

Responsibilities:

1. Inspect the existing repository and current stage state.
2. Implement the complete assigned specification.
3. Run appropriate checks.
4. Commit the implementation.
5. Report the committed revision and verification results.
6. Address concrete findings returned by the Reviewer.
7. Produce a new committed revision when corrections are required.

The Implementer does not treat the shipped tests as the complete specification.

### Reviewer

The Reviewer provides independent verification.

Responsibilities:

1. Receive the complete requirements and implementation handoff.
2. Inspect the implementation independently.
3. Run relevant checks.
4. Compare the implementation against the complete specification.
5. Report concrete PASS/FAIL findings.
6. Reject incomplete or incorrect implementations.
7. Re-review corrected revisions.

The Reviewer does not simply acknowledge the Implementer's work.

## Handoff Protocol

Every delegated task contains the complete requirements needed by the receiving seat.

The workflow is:

```text
Coordinator
    |
    | complete stage specification
    v
Implementer
    |
    | committed revision + checks
    v
Reviewer
    |
    | PASS or concrete findings
    v
Coordinator
    |
    | corrections when required
    v
Implementer
```

A stage is not considered complete merely because the Implementer reports success. The Reviewer independently checks the resulting revision.

## Stage Progression

The factory processes stages sequentially.

For each stage:

1. Coordinator dispatches the complete specification.
2. Implementer implements the specification.
3. Implementer commits the revision.
4. Reviewer independently verifies the revision.
5. Findings are corrected when necessary.
6. Reviewer verifies the corrected revision.
7. Coordinator proceeds to the next stage.

The submitted repository contains the completed Stage 1 and Stage 2 outputs.

## Communication

The room is used for agent-to-agent coordination and handoffs.

Messages are intended to represent meaningful state changes: specifications, implementation handoffs, review findings, corrections, and completion decisions. The factory does not rely on acknowledgement-only messages as work.

## Autonomy

The submitted workflow is intended to run without human intervention between dispatches.

The human does not manually implement application code, approve intermediate results, provide clarification, debug the implementation, or manually rerun failed work during the autonomous factory run.

## Evidence

`room.json` records the factory's room session.

The stage directories contain the resulting buildable services, including their Dockerfiles and `RUN.md` instructions.

The repository therefore provides both the factory communication evidence and the resulting software artifacts.
