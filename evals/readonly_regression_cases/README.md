# Public read-only regression cases

These cases promote confirmed model or prompt failures from the retired
`readonly-holdout-v1` into a public development regression set.

- They are public and are no longer secret holdout material.
- They must not be used to recalculate or replace the v1 formal score.
- They contain public atomic `semantic_contract` claims for isolated language
  scoring; those claims never enter the tested Agent context.
- They are used by the 49 fixed public human-labelled calibration fixtures and future
  four-trial development runs.
- The retired v1 holdout must never be rerun.

Cases whose original expectations over-prescribed an equivalent read tool or
an arbitrary one-search limit were intentionally not promoted.
