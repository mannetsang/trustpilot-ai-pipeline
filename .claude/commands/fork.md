---
description: Fork the current GitHub repo, wire it up as the `fork` remote, and push the current branch
---

Fork the current repository on GitHub and push the current branch to the
fork. If `$ARGUMENTS` is non-empty, treat it as the destination
organization (fork into that org instead of the authenticated user).

Do this end-to-end, stopping and reporting concretely on any failure:

1. Verify the working directory is a git repository and read
   `git remote get-url origin` to get the source `owner/repo`. Bail if
   the origin URL is not a GitHub HTTPS/SSH remote.
2. Read the current branch: `git rev-parse --abbrev-ref HEAD`. Bail if
   HEAD is detached.
3. Resolve the destination:
   - If `$ARGUMENTS` is empty, call `mcp__github__get_me` to get the
     authenticated user's login.
   - Otherwise use `$ARGUMENTS` verbatim as the destination org.
4. Fork via `mcp__github__fork_repository` with the source owner/repo
   and (if provided) the destination org. A "fork already exists" or
   422 response is not an error — continue.
5. Compute the fork URL as `https://github.com/<dest>/<repo>.git`. Add
   it as a git remote named `fork`. If a `fork` remote already exists
   with a different URL, print both URLs and ask the user before
   overwriting.
6. Push the current branch: `git push -u fork <branch>`. Retry on
   transient network failures with 2s/4s/8s backoff.
7. Report:
   - Fork URL: `https://github.com/<dest>/<repo>`
   - Compare/PR URL: `https://github.com/<source-owner>/<repo>/compare/<default-branch>...<dest>:<branch>`
     (fetch the default branch from the source repo — do not assume
     `main`).

Do not open a pull request unless the user explicitly asks in a later
message.
