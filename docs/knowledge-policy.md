# Knowledge policy

## Placement

- Do not restate derivable facts by hand. Keep generated facts only when a freshness check owns them.
- Put enforceable rules in mechanical guards or tests.
- Use a short local comment when a plausible edit could break ordering, ownership, cancellation, cleanup, or platform behavior and the guard or code does not explain the constraint.
- Record externally justified or repository-wide decisions in dated, immutable Decision Records. Replace a decision with a new record whose `supersedes` field names the earlier record.
- Keep measurements and external references in mutable documents with their source and measurement date.
- Write operational procedures as reproducible runbooks with prerequisites, commands, and completion criteria.

## Cross-cutting invariants

An externally justified invariant that spans areas requires a Decision Record, a local guard or test at each failure boundary, and a short warning comment wherever the plausible wrong edit would otherwise look safe.

## Temporary migration artifacts

Define a completion criterion when adding a temporary migration artifact. Before removal, classify every assertion as a permanent guard, an intentional retirement, or a documented residual boundary. Choose a permanent guard only for a current contract exposed to a documented or plausible wrong edit and only with bounded maintenance cost. A retired scanner's synthetic test is not evidence of need.
