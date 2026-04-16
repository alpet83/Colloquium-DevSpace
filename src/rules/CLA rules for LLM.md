# Code Lossless Assistance (CLA) CRITICAL IMPORTANCE RULES
 
@relevance:100

**Date**: 2025-06-24  
**Purpose**: To define the guidelines for providing code-related assistance in a way that ensures accuracy, clarity, and minimal data loss when working with user-provided code, project files, and development tasks.

## Overview
The Code Lossless Assistance (CLA) rules are designed to ensure that responses involving code (e.g., debugging, feature development, unit testing) preserve the integrity of the user’s project, provide clear and accurate solutions, and minimize context overload. These rules are particularly relevant for projects like `trade_report`, where iterative code changes, file synchronization, and detailed debugging are critical.

## Rules

## Rule 0: Create `CLA_selftest.md` for Compliance Tracking
- **Description**: Generate a `CLA_selftest.md` file evaluating compliance with all CLA rules (1–18) with scores from 1 (radical violation) to 5 (full compliance). Calculate the average KPI score and warn if it is below 4.0. Track KPI progression in subsequent iterations.
- **Purpose**: Ensure systematic adherence to all CLA rules and monitor improvement.

## Rule 1: Focus on the root cause of the issue (EDA)
- **Description**: Identify and address the root cause of issues using exploratory data analysis (EDA), such as logs or test failures.
- **Purpose**: Ensure solutions target the core problem, avoiding superficial fixes.

## Rule 2: Ensure code correctness
- **Description**: Verify that all code changes are syntactically and semantically correct, compile without errors, and pass all tests.
- **Purpose**: Maintain a stable and functional codebase.

## Rule 3: Follow Rust 2024 edition standards
- **Description**: Adhere to Rust 2024 edition conventions, including avoiding `never type fallback` warnings and using modern idioms.
- **Purpose**: Ensure compatibility and maintainability with the latest Rust standards.

## Rule 4: Use appropriate error handling
- **Description**: Implement robust error handling using types like `AppError` and provide meaningful error messages.
- **Purpose**: Improve code reliability and user experience.

## Rule 5: Optimize for performance
- **Description**: Ensure changes do not degrade performance, using efficient data structures and algorithms where applicable.
- **Purpose**: Maintain scalability and responsiveness of the application.

## Rule 6: Maintain test coverage
- **Description**: Update or add tests to cover all changes, ensuring no regression in functionality.
- **Purpose**: Guarantee code reliability through comprehensive testing.

## Rule 7: Follow project style guidelines
- **Description**: Adhere to the project's coding style, including naming conventions, formatting, and logging practices (e.g., `#INFO`, `#DBG`).
- **Purpose**: Ensure code consistency and readability.

## Rule 8: Document changes
- **Description**: Provide clear documentation for new or modified code, including comments and external documentation if needed.
- **Purpose**: Facilitate code understanding and maintenance.

## Rule 9: Respect dependency constraints
- **Description**: Use only dependencies listed in `Cargo.toml` and avoid introducing new ones without justification.
- **Purpose**: Prevent dependency bloat and ensure compatibility.

## Rule 10: Provide clear justification for changes
- **Description**: Justify each change with a clear goal, problem, necessity, and comparison to the original code.
- **Purpose**: Ensure changes are well-reasoned and aligned with project goals.

## Rule 11: Avoid speculative changes
- **Description**: Do not introduce changes based on assumptions or unverified requirements.
- **Purpose**: Prevent unnecessary or incorrect modifications.

## Rule 12: Minimize changes, preserve working functionality
- **Description**: Make minimal changes to achieve the goal, preserving existing functionality ("If it works, don't touch it").
- **Purpose**: Reduce risk of introducing new bugs.

## Rule 13: Verify context with GitHub and request files if unclear
- **Description**: Cross-check changes with the GitHub repository (`https://github.com/alpet83/trade_report`) and request clarification if context is missing.
- **Purpose**: Ensure alignment with the latest codebase and requirements.

## Rule 14: Avoid abbreviations in code and comments
- **Description**: Use full, descriptive names for variables, functions, and comments to enhance clarity.
- **Purpose**: Improve code readability and maintainability.

## Rule 15: Control token count
- **Description**: Monitor token count changes in modified code, ensuring reductions do not compromise functionality unless explicitly required.
- **Purpose**: Maintain compatibility with LLM context limits while preserving code integrity.

## Rule 16: Provide full files for changes
- **Description**: Submit complete files with changes, not partial snippets, to ensure context and functionality are preserved.
- **Purpose**: Facilitate review and integration of changes.

## Rule 17: Avoid code duplication
- **Description**: Refactor duplicated code into reusable functions or modules to reduce redundancy.
- **Purpose**: Improve maintainability and reduce token count.

## Rule 18: Include module header comments
- **Description**: Every full Rust module (`.rs` file) must start with a comment containing the full file path (e.g., `// /src/tests/setup.rs`) and the proposed change date (e.g., `// Proposed: 2025-07-11`).
- **Purpose**: Ensure traceability and documentation of module changes.

**Note**: All rules (0–18) must be applied consistently for every change. Compliance is tracked in `CLA_selftest.md`.