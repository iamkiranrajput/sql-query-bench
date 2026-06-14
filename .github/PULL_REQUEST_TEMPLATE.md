# Pull Request

## Description

<!-- Briefly describe the changes in this PR -->

## Type of Change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Performance improvement
- [ ] Refactoring (no functional changes)

## Query Execution Checklist (MANDATORY if touching query execution)

**Does this PR modify query execution, planning, or SQL generation?**

- [ ] **NO** - Skip this section
- [ ] **YES** - Complete checklist below

### Validator Authority (Principle 1)

- [ ] Does this PR call `validate_plan_against_schema()` before execution?
- [ ] If NO: Provide justification for validator bypass ➡️ **Requires Principal Engineer approval**
- [ ] Are all validation failures logged with sufficient detail?
- [ ] Is validation failure treated as BLOCKING (not just warning)?

### Separation of Concerns (Principle 2)

- [ ] Are heuristics (Smart Filter, LLM) kept in PROPOSAL layer?
- [ ] Is validation logic deterministic (not heuristic)?
- [ ] Does the code clearly separate Intent, Heuristic, and Enforcement layers?

### ProjectionIntent Usage (Principle 4)

- [ ] Does this PR use `ProjectionIntent`?
- [ ] If YES: Is it ONLY for audit/SLO/explain? (NOT for execution)
- [ ] If NO to above: **REJECT** - Architectural violation

### Validation Changes

- [ ] Does this PR modify validator logic?
- [ ] If YES: **Requires Tech Lead approval**
- [ ] Are new validation rules added (not removed)?
- [ ] Are severity levels (FATAL/WARNING/INFO) used correctly?

## Testing

- [ ] Unit tests added/updated
- [ ] Integration tests added/updated
- [ ] Manual testing performed
- [ ] Test coverage >80% for new code

## Rollback Plan

<!-- Describe how to rollback this change if it causes issues -->

- [ ] Rollback plan documented
- [ ] Feature flag available (if high risk)
- [ ] Monitoring in place to detect issues

## Documentation

- [ ] Code comments added/updated
- [ ] Architecture principles followed (see `query_planning/README.md`)
- [ ] API documentation updated (if applicable)

## Reviewer Notes

<!-- Any special instructions for reviewers -->

---

## Approval Requirements

- **Standard PR:** 1 reviewer
- **Validator changes:** Tech lead approval required
- **Validator bypass:** Principal engineer approval required
- **Architecture changes:** Architecture review meeting required

---

**Remember:** "Prompts propose. Validators decide." - See `query_planning/README.md`
