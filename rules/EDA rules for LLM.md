# Effective Debugging Assistance (EDA) for Multithreaded Applications

This document outlines universal best practices for debugging multithreaded applications, ensuring efficient issue identification and resolution while minimizing unnecessary changes. The rules align with the **Code Lossless Assistance (CLA)**, emphasizing minimal changes, clear justification, and preservation of working functionality. Inspired by the collaborative debugging metaphor of Sherlock Holmes (the developer, formalizing system logic) and Dr. Watson (the assistant, suggesting corrections), these rules prioritize finding the root cause over quick suppression of symptoms.

## 1. Focus on Root Cause Identification
- **Description**: Prioritize identifying the root cause of an issue (e.g., race conditions, data inconsistencies, test errors) before proposing fixes, akin to solving a detective mystery.
- **Why**: Suppressing symptoms without understanding the cause can mask deeper issues or introduce new bugs.
- **How**: Use logging, assertions, and minimal test cases to trace the issue's origin.
- **When to Apply**: Always start with diagnostics before modifying logic.

## 2. Avoid Modifying Logic Until Cause is Confirmed
- **Description**: Refrain from changing application logic until the root cause is fully understood, preserving the program's intended behavior.
- **Why**: Premature changes can obscure issues or alter the system's "plot," complicating debugging.
- **How**: Collect evidence through logs and tests to confirm the issue's source before proposing fixes.
- **When to Apply**: Until diagnostics confirm the cause.

## 3. Maintain Impartiality in Diagnosis
- **Description**: Consider all possible sources of an issue (code, tests, input data, runtime conditions) with equal weight, systematically excluding non-contributing factors.
- **Why**: Bias toward code fixes can overlook test or data issues, leading to incorrect solutions.
- **How**: Validate code logic, test assertions, and input data coverage independently.
- **When to Apply**: For all issues, especially when tests pass but outputs are incorrect.

## 4. Prioritize Diagnostic Logging
- **Description**: Add targeted logging to track execution flow, state changes, and synchronization points (e.g., thread creation, task scheduling, lock acquisition).
- **Why**: Logging provides visibility into thread interactions without invasive changes, helping identify race conditions or missed tasks.
- **How**: Use debug-level logs for detailed tracing and info/warn for key events.
- **When to Apply**: As the first step in debugging, before modifying logic.

## 5. Use Thread-Safe Data Structures
- **Description**: Prefer thread-safe collections (e.g., concurrent hash maps, atomic variables) to reduce contention and simplify synchronization.
- **Why**: High contention on locks can cause delays or deadlocks in multithreaded environments.
- **How**: Choose structures like concurrent maps or atomic booleans for shared state.
- **When to Apply**: When contention or synchronization issues are suspected, with clear justification.

## 6. Minimize Thread Spawning
- **Description**: Reuse existing threads with control flags to manage lifecycle, avoiding frequent thread creation.
- **Why**: Excessive thread spawning increases overhead and risks race conditions.
- **How**: Use atomic flags or channels to signal thread continuation or termination.
- **When to Apply**: When diagnostics show frequent thread creation.

## 7. Avoid Hard-Coded Timeouts
- **Description**: Use dynamic polling or configurable delays instead of fixed timeouts to handle varying task durations.
- **Why**: Fixed timeouts can cause premature test failures or unreliable behavior in production.
- **How**: Implement polling with a maximum wait time or configurable delays.
- **When to Apply**: When timeouts cause test failures or task durations vary.

## 8. Ensure Sequential Test Execution
- **Description**: Use synchronization primitives (e.g., mutexes) to enforce sequential test execution, preventing shared state corruption.
- **Why**: Concurrent test execution can cause race conditions in shared resources.
- **How**: Apply global locks in tests accessing shared state.
- **When to Apply**: In tests accessing shared resources like singletons.

## 9. Validate Assumptions with Assertions
- **Description**: Add assertions in tests to validate expected states (e.g., task completion, output sizes) before proceeding.
- **Why**: Assertions catch issues early, preventing silent failures.
- **How**: Include checks for critical invariants in test cases.
- **When to Apply**: In all tests to ensure correctness.

## 10. Isolate and Reproduce Issues
- **Description**: Create minimal test cases to reproduce issues in isolation, focusing on specific components.
- **Why**: Isolated tests reduce noise and pinpoint root causes.
- **How**: Write dedicated tests for individual functionalities or failure conditions.
- **When to Apply**: When indirect testing obscures the issue's source.

## 11. Document Changes Clearly
- **Description**: Provide clear justifications for changes (problem, goal, impact), using versioned documentation.
- **Why**: Clear documentation ensures maintainability and alignment with user expectations.
- **How**: Include change rationale in commit messages or artifacts.
- **When to Apply**: For all code changes.

## 12. Use Backtraces for Failures
- **Description**: Enable full backtraces for test failures to capture detailed context.
- **Why**: Backtraces provide critical information about failure points.
- **How**: Run tests with full backtrace enabled (e.g., `RUST_BACKTRACE=full`).
- **When to Apply**: When tests fail unexpectedly or errors are unclear.