# Tasks

## 1. Key the client state directory by server and application

- [x] 1.1 Change `ci_directory` to take the server alias and use `ci/<server>/<app>`, pass `manifest["server"]` from
      `github_setup`, `rotate_key`, and `github_status`, and update the existing tests that assert the old path; verify
      with `python -m unittest tests.test_github`
- [x] 1.2 Add the legacy-state migration (move regular files from `ci/<app>` when the new directory has no
      `state.json`, rewrite a moved `private_key` path, leave symlinks and an existing new state alone) and a test in
      `tests/test_github.py` that moves a completed setup to the old location, reruns setup, and asserts the key is
      reused and the state now lives under the server; verify with `python -m unittest tests.test_github`

## 2. Derive the key to rotate from the server

- [x] 2.1 Add the identification helper that returns only labels equal to the label derived from the same key's
      fingerprint, with tests for a matching pair and for a hand-picked label that must be ignored; verify with
      `python -m unittest tests.test_github`
- [x] 2.2 Make `rotate_key` take the old labels from the server's `keys list`, refuse with an error naming
      `shiplord github setup` when there is no identifiable CI key (before generating anything), and stop reading the
      local `secret_set`/`label` gate; add a test that rotates with an empty config directory and a test that refuses
      when the server has no CI key; verify with `python -m unittest tests.test_github`
- [x] 2.3 Revoke every identified old label after the new key is proven and stored, keep the single-key `old_label`
      result and add `old_labels`, keep the revocation running when the local cache cannot be written, and add tests
      for two leftover CI keys, for a failed removal naming the label still registered, and for a failed cache write;
      verify with `python -m unittest tests.test_github`
- [x] 2.4 Add a test that the same application name on two servers keeps separate state and that rotating one server
      leaves the other's key registered; verify with `python -m unittest tests.test_github`

## 3. Verify the whole change

- [x] 3.1 Run `python -m unittest discover -s tests` and confirm `OK` with 68 skipped and no failures
- [x] 3.2 Confirm the new rotation tests fail against the previous `github.py`, so they are regression tests and not
      assertions of existing behaviour
- [x] 3.3 Confirm no test fixture, result, or error message contains private-key material, and that `openspec validate
      ci-state-from-server --strict` passes
