# Release checklist

A manual checklist for the repository owner to work through before publishing
a release or substantial security-sensitive update. Nothing in this document
performs those actions automatically.

## 1. Clean tests

- [ ] Run `uv sync --locked --extra dev` in a clean environment and confirm
      `uv.lock` is unchanged.
- [ ] Run the full local suite:
      `.venv/bin/python -m unittest discover -s tests -v`.
- [ ] Run `.venv/bin/python -m compileall -q src skills tests` to catch syntax
      errors.
- [ ] Run `python -m pip_audit --requirement requirements.txt` and resolve or
      explicitly document every finding.
- [ ] Confirm CI (`.github/workflows/ci.yml`) is green on Python 3.14 for the
      commit you intend to push.
- [ ] Build wheel and sdist with `uv run python -m build`, install the wheel in
      a fresh environment, and verify its reported version matches the intended
      `v<version>` tag.

## 2. Ignored and private material

- [ ] Review `.gitignore`, `.git/info/exclude`, and the build manifest; confirm
      local coordination state, machine settings, archives, and unrelated fixtures
      cannot enter the intended public tree or distribution.
- [ ] Run `git status` in this repository (not yet done as part of this
      checklist item) and read every untracked path before staging anything,
      to confirm nothing unexpected would be added.

## 3. Credential scan

- [ ] Search the tracked/intended-public tree for obvious secret material
      (API keys, tokens, passwords, private SSH keys, `.env` files) before
      the release commit. Do not rely solely on `.gitignore`; read file
      contents, not just filenames.
- [ ] Confirm example configuration and service files (`workflow.example.toml`
      and both files under `deploy/`) contain only placeholders, not real paths,
      hosts, or credentials.

## 4. Config and help consistency

- [ ] Confirm `README.md`'s documented settings table matches
      `workflow.example.toml` and `web_app.py --help` output.
- [ ] Confirm every relative path README links to (for example
      `docs/SELF_HOSTING.md`, `docs/RELEASE_CHECKLIST.md`) exists in the
      tree you are about to publish.

## 5. Vendored attribution

- [ ] Confirm `src/coordinator/assets/web/vendor/LICENSE.txt`
      (xterm.js and addon-fit) is present and unmodified, and that
      `README.md`'s "Third-party code" section still accurately names what
      is vendored and where.

## 6. Local smoke test

- [ ] Copy `workflow.example.toml` to `workflow.toml`, run
      `.venv/bin/coordinator serve --config workflow.toml`,
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
      `src/coordinator/assets/web/vendor/` carry their own
      upstream copyright lines and are correct as they stand; do not edit
      them to match the root license.)

## 8. Commit review

- [ ] Before the release commit, run `git status` and `git diff --cached`
      (after staging) and read the complete result, not just a summary.
- [ ] Confirm the commit message and author identity are what the owner
      intends.

## 9. Push

- [ ] Confirm the intended remote and branch immediately before pushing. The
      repository tooling does not push on its own. Do not push until steps 1-8
      above are complete, including the license verification in step 7.
- [ ] After pushing the signed version tag, confirm the Release workflow uploads
      wheel, sdist, CycloneDX SBOM, and `SHA256SUMS`, and that `gh attestation
      verify` succeeds for the wheel before announcing the release.
