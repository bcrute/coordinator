# Release checklist

A manual checklist for the repository owner to work through before pushing
this repository's intended public file set to a new public remote. Nothing
in this document performs any of these actions automatically, and none of
them have been performed on your behalf by adding this file.

## 1. Clean tests

- [ ] Run the full local suite: `python3 -m unittest discover -s tests -v`.
- [ ] Run `python -m compileall -q skills tests` to catch syntax errors.
- [ ] Confirm CI (`.github/workflows/ci.yml`) is green on Python 3.11, 3.12,
      and 3.13 for the commit you intend to push, once a remote exists to run
      it on.

## 2. Ignored and private material

- [ ] Review `.gitignore` and confirm every local-only, proprietary, or
      unrelated fixture (for example `citadel-main.zip`, `readiness_demo/`,
      `examples/`, `.coordination/`, `workflow.toml`) is excluded.
- [ ] Run `git status` in this repository (not yet done as part of this
      checklist item) and read every untracked path before staging anything,
      to confirm nothing unexpected would be added.

## 3. Credential scan

- [ ] Search the tracked/intended-public tree for obvious secret material
      (API keys, tokens, passwords, private SSH keys, `.env` files) before
      the first commit. Do not rely solely on `.gitignore`; read file
      contents, not just filenames.
- [ ] Confirm example configuration and service files (`workflow.example.toml`,
      `deploy/workflow-web.service.example`) contain only placeholders, not
      real paths, hosts, or credentials.

## 4. Config and help consistency

- [ ] Confirm `README.md`'s documented settings table matches
      `workflow.example.toml` and `web_app.py --help` output.
- [ ] Confirm every relative path README links to (for example
      `docs/SELF_HOSTING.md`, `docs/RELEASE_CHECKLIST.md`) exists in the
      tree you are about to publish.

## 5. Vendored attribution

- [ ] Confirm `skills/coordinate-claude-work/assets/web/vendor/LICENSE.txt`
      (xterm.js and addon-fit) is present and unmodified, and that
      `README.md`'s "Third-party code" section still accurately names what
      is vendored and where.

## 6. Local smoke test

- [ ] Copy `workflow.example.toml` to `workflow.toml`, run
      `python3 skills/coordinate-claude-work/scripts/web_app.py --config workflow.toml`,
      and confirm the dashboard loads at the printed loopback URL before
      publishing.

## 7. License verification

The license decision is made: this repository is released under the MIT
License with the owner's personal copyright. Verify that the tree you are
about to publish still says so.

- [ ] Confirm a root `LICENSE` file exists and contains the standard MIT
      License text.
- [ ] Confirm its copyright line reads exactly
      `Copyright (c) 2026 Benjamin Crute`, with no company or organization
      name substituted.
- [ ] Confirm `README.md`'s "License" section names MIT and Benjamin Crute
      and links to `LICENSE`.
- [ ] Confirm every first-party file that mentions licensing points at the
      root MIT license rather than at a choice the owner has yet to make.
      (The vendored third-party notices under
      `skills/coordinate-claude-work/assets/web/vendor/` carry their own
      upstream copyright lines and are correct as they stand; do not edit
      them to match the root license.)

## 8. Commit review

- [ ] Before the first commit, run `git status` and `git diff --cached`
      (after staging) and read the complete result, not just a summary.
- [ ] Confirm the commit message and author identity are what the owner
      intends.

## 9. Remote creation and push

- [ ] Creating the GitHub (or other) remote, and the first push, are
      explicit, separate owner actions. Nothing in this repository's tooling
      creates a remote or pushes on its own. Do not push until steps 1-8
      above are complete, including the license verification in step 7.
