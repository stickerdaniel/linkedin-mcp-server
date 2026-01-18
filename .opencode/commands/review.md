---
description: 'Comprehensive PR review using specialized agents'
argument-hint: '[review-aspects]'
allowed-tools: ['Bash', 'Glob', 'Grep', 'Read', 'Task']
---

# Comprehensive PR Review

Run a comprehensive pull request review using multiple specialized agents, each focusing on a different aspect of code quality. You can review in plan mode, the review doesnt require modifications until the user approves the final plan with the suggested fixes.

**Review Aspects (optional):** "$ARGUMENTS"

## Review Workflow:

1. **Determine Review Scope**
   - Check git status to identify changed files
   - Parse arguments to see if user requested specific review aspects
   - Default: Run all applicable reviews

2. **Available Review Aspects:**
   - **comments** - Analyze code comment accuracy and maintainability
   - **tests** - Review test coverage quality and completeness
   - **errors** - Check error handling for silent failures
   - **types** - Analyze type design and invariants (if new types added)
   - **code** - General code review for project guidelines
   - **simplify** - Simplify code for clarity and maintainability
   - **all** - Run all applicable reviews (default)

3. **Identify Changed Files**
   - Run `git diff --name-only` to see modified files
   - Check if PR already exists: `gh pr view`
   - Identify file types and what reviews apply

4. **Determine Applicable Reviews**

   Based on changes:
   - **Always applicable**: code-reviewer (general quality)
   - **If test files changed**: pr-test-analyzer
   - **If comments/docs added**: comment-analyzer
   - **If error handling changed**: silent-failure-hunter
   - **If types added/modified**: type-design-analyzer
   - **After passing review**: code-simplifier (polish and refine)

5. **Launch Review Agents**

   **Sequential approach** (user can request one at a time):
   - Easier to understand and act on
   - Each report is complete before next
   - Good for interactive review

   **Parallel approach** (default):
   - Launch all agents simultaneously
   - Faster for comprehensive review
   - Results come back together

6. **Aggregate Results**

   After agents complete, summarize:
   - **Critical Issues** (must fix before merge)
   - **Important Issues** (should fix)
   - **Suggestions** (nice to have)
   - **Positive Observations** (what's good)

7. **Provide Action Plan**

   Organize findings:

   ```markdown
   # PR Review Summary

   ## Critical Issues (X found)

   - [agent-name]: Issue description [file:line]

   ## Important Issues (X found)

   - [agent-name]: Issue description [file:line]

   ## Suggestions (X found)

   - [agent-name]: Suggestion [file:line]

   ## Strengths

   - What's well-done in this PR

   ## Recommended Action

   1. Fix critical issues first
   2. Address important issues
   3. Consider suggestions
   4. Re-run review after fixes
   ```

## Usage Examples:

**Full review (default):**

```
/review
```

**Specific aspects:**

```
/review tests errors
# Reviews only test coverage and error handling

/review comments
# Reviews only code comments

/review simplify
# Simplifies code after passing review
```

**Perpendicular review:**

```
/review all perpendicular
# Launches all agents after each other
```

## Agent Descriptions:

**comment-analyzer**:

- Verifies comment accuracy vs code
- Identifies comment rot
- Checks documentation completeness

**pr-test-analyzer**:

- Reviews behavioral test coverage
- Identifies critical gaps
- Evaluates test quality

**silent-failure-hunter**:

- Finds silent failures
- Reviews catch blocks
- Checks error logging

**type-design-analyzer**:

- Analyzes type encapsulation
- Reviews invariant expression
- Rates type design quality

**code-reviewer**:

- Checks AGENTS.md compliance
- Detects bugs and issues
- Reviews general code quality

**code-simplifier**:

- Simplifies complex code
- Improves clarity and readability
- Applies project standards
- Preserves functionality

## Tips:

- **Run early**: Before creating PR, not after
- **Focus on changes**: Agents analyze git diff by default
- **Address critical first**: Fix high-priority issues before lower priority
- **Re-run after fixes**: Verify issues are resolved
- **Use specific reviews**: Target specific aspects when you know the concern

## Workflow Integration:

**Before committing:**

```
1. Write code
2. Run: /review code errors
3. After review agents have finished, launch a general subagent for every critical / important issue found that should verify if this is indeed an issue and if it should be fixed. Instruct those general agents to use the tools available. For example, if it's a Svelte specific issue, it should use the Svelte MCP. If it's a Convex related issue, use the Convex mcp.
4. Enter plan mode if you arent already in it. Create a plan that addresses the issues and how to fix them.
5. User confirms the plan and fix the issues.
```

**Before creating PR:**

```
1. Stage all changes
2. Run: /review all
3. After review agents have finished, launch a general subagent for every critical / important issue found that should verify if this is indeed an issue and if it should be fixed. Instruct those general agents to use the tools available. For example, if it's a Svelte specific issue, it should use the Svelte MCP. If it's a Convex related issue, use the Convex mcp.
4. Create a plan that addresses the issues and how to fix them.
5. Run specific reviews again to verify
6. Create PR
```

**After PR feedback:**

```
1. Make requested changes
2. Run targeted reviews based on feedback
3. Verify issues are resolved
4. Push updates
```

## Notes

- Agents run autonomously and return detailed reports
- Each agent focuses on its specialty for deep analysis
- Results are actionable with specific file:line references
- Agents use appropriate models for their complexity
