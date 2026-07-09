---
name: git-commit
description: Triggered when committing or pushing code. Run tests, write a conventional commit message, and confirm before pushing.
user-invocable: true
---

## Process
1. Run the full test suite (`pytest tests/ -v --tb=short`) and confirm it passes
2. Run the linter and fix any errors before proceeding
3. Show me a summary of all changed files
4. Write a commit message using conventional commits format:
   - feat: new feature
   - fix: bug fix
   - chore: maintenance or refactor
   - docs: documentation only
   - test: adding or updating tests
5. Show me the diff and the proposed commit message — wait for my approval
6. Never push directly to main — always push to a feature branch
7. Never commit .env files, API keys, or secrets
