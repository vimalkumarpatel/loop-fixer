# `loop_fixer` — Make the loop language-agnostic (add Java/Maven support)

## Context

`loop_fixer` (`/Users/vimalkumarpatel/git/test/`, pushed to `github.com/vimalkumarpatel/loop-fixer`) currently hardcodes Python/pytest in four places: `test_runner.py` (fixed `pytest` argv), `fsm.py` (`_target_to_path` parses pytest's `file::test` node-id syntax; `_resolve_import_files` uses Python's `ast` module), and `context.py` (`last_meaningful_line` looks for pytest's `"E "` assertion-line prefix, and its path regex only matches `.py`).

The goal is to prove the FSM/patch/git-checkpoint architecture generalizes across languages by adding a second, real target: Java via Maven. The core guarantees — code owns verification (test exit code), the actor can't touch the test file, every write is allowlisted, every failure rolls back — must hold identically for both languages; only the language-specific mechanics (how to invoke the test runner, how to resolve writable source files, how to read a failure signature out of the output) change.

Decisions made with the user: Maven (not Gradle) for the Java side; writable-path resolution via directory-convention mirroring (`src/test/java/.../FooTest.java` → `src/main/java/.../Foo.java`) plus regex-parsing the test file's `import` statements for same-repo helper classes; language is selected via an explicit `--language {python,java}` CLI flag (default `python`, preserving today's behavior); and — per the user's brief — "use Claude Code to run this fixer on Java broken tests and fix the fixer" is a validation instruction for this session (actually run the finished tool end-to-end against a seeded broken Maven repo and fix whatever breaks), not a request to change how `loop_fixer` calls its own LLM. Maven/a JDK are not currently installed in this environment (`mvn` missing, `java`/`javac` are unconfigured stubs) — the user asked to install them via Homebrew (with a go-ahead prompt at that point) so the Java path can be validated live rather than only unit-tested against canned output.

## Design: a `LanguageAdapter` abstraction

New module `loop_fixer/adapters/`:
- `base.py` — the `LanguageAdapter` Protocol:
  ```python
  class LanguageAdapter(Protocol):
      name: str
      def resolve_test_file(self, repo_root: Path, target: str) -> Path: ...
      def resolve_writable_paths(self, repo_root: Path, test_file: Path) -> set[str]: ...
      def run_test(self, repo_root: Path, target: str, timeout: float) -> TestResult: ...
      def compute_signature(self, result: TestResult) -> str: ...
  ```
  `TestResult` (already in `test_runner.py`) stays the shared, language-neutral return type.
- `python_pytest.py` — `PythonPytestAdapter`: today's logic moved verbatim, not rewritten — `_target_to_path`/`_resolve_import_files` (ast-based) from `fsm.py`, the pytest argv from `test_runner.run_pytest`, and the `"E "`-prefix-aware `last_meaningful_line` from `context.py`.
- `java_maven.py` — `JavaMavenAdapter`, new:
  - **Target format**: `com.example.FooTest#testBar` (exactly what Maven Surefire's `-Dtest=` already expects — no invented syntax).
  - **`resolve_test_file`**: dots → slashes, `src/test/java/com/example/FooTest.java`.
  - **`run_test`**: fixed argv `["mvn", "-q", "-Dtest=<spec>", "-Dsurefire.failIfNoSpecifiedTests=false", "test"]`, `cwd=repo_root`, same `subprocess.run(..., capture_output=True, timeout=...)` pattern as the pytest adapter — still exactly one code-constructed command shape, still nothing LLM-influenced in the argv, matching the existing bounded-authority guarantee. Raises a clear `AdapterError` (new, in `errors.py`) if `shutil.which("mvn")` is `None`, mirroring how the Anthropic client fails fast when unconfigured.
  - **`resolve_writable_paths`**: (a) convention — strip a `Test` suffix/prefix from the class name and check `src/main/java/.../<Class>.java` exists; (b) regex `^import\s+([\w.]+);` over the test file, resolve each to `src/main/java/<package/path>/<Class>.java` if it exists under `repo_root`. Union of (a) and (b); test file itself is never included — same non-negotiable exclusion as the Python adapter.
  - **`compute_signature`**: scans Surefire output for the first `Tests run:` / `FAILED` / exception line, extracts the last exception/assertion line (e.g. `org.junit...ComparisonFailure: ...`), reuses `context.normalize_failure_text`/`truncate_output` (generalized, see below) for the volatile-detail stripping.

`context.py` changes: generalize `_PATH_RE` to match any file path, not just `.py` (e.g. `(?:/[\w.\-]+)+\.\w+`), so the shared normalization helper works for both `.py` and `.java` paths. `last_meaningful_line`'s pytest-specific `"E "` check moves into `python_pytest.py`; a Java-specific equivalent lives in `java_maven.py`. `truncate_output`/`normalize_failure_text` stay in `context.py`, shared by both adapters.

## Wiring changes

- **`fsm.py`**: `LoopState` gets `adapter: LanguageAdapter` (replacing the implicit pytest coupling). `state_plan`/`state_edit`/`state_test`/`state_analyze` call `state.adapter.resolve_test_file/resolve_writable_paths/run_test/compute_signature` instead of the hardcoded pytest functions/`ast` import. No change to `state_decide` (stop conditions, git checkpointing) — that logic was already language-neutral.
- **`cli.py`**: add `--language {python,java}` (default `python`); construct `PythonPytestAdapter()` or `JavaMavenAdapter()` and pass it into `LoopState`. Simplify the baseline-run check from the pytest-specific `returncode not in (0, 1)` to a language-neutral `returncode == 0` (already passing) vs. anything else (proceed) — the no-progress detector already guards against a fundamentally broken invocation, so the extra pytest-specific special case is no longer needed once there's more than one adapter.
- **`errors.py`**: add `AdapterError(LoopFixerError)` for missing build tooling.
- **`patch_apply.py`, `git_checkpoint.py`**: unchanged — both already operate on paths/text, not on any language's syntax.
- **Prompts** (`prompts/generate_patch.txt`): already language-neutral wording ("SOURCE FILE(S)", no pytest-specific phrasing); no change needed.

## New fixture + tests

- `tests/fixtures/broken_repo_java/` — minimal Maven project: `pom.xml`, `src/main/java/com/example/Calc.java` (broken: `return a - b;`), `src/test/java/com/example/CalcTest.java` (asserts `add(2, 3) == 5`) — the direct Java analog of the existing `broken_repo` Python fixture.
- `tests/test_adapters_java.py` — unit tests for `JavaMavenAdapter`'s pure logic (target→path resolution, writable-path convention+import resolution, signature extraction from canned Surefire-style output strings) — no real `mvn` invocation required, so these stay hermetic like the rest of the suite.
- `tests/test_e2e_demo_java.py` — mirrors `test_e2e_demo.py` but drives a real `mvn test` against the seeded Maven fixture with a `FakeLLMClient` scripted to fix the bug; asserts convergence, that `CalcTest.java` is byte-identical afterward, and (mirroring the existing Python coverage) that an LLM attempt to edit the test file is rejected before touching disk. This test is naturally skipped if `mvn` isn't on `PATH`, so the suite stays runnable on machines without Java tooling.
- Existing `tests/test_fsm.py`/`test_patch_apply.py`/`test_git_checkpoint.py`/`test_cli.py`/`test_e2e_demo.py` (Python path) need updating only where they construct a `LoopState` directly, to pass `adapter=PythonPytestAdapter()`.

## Critical files
- `loop_fixer/adapters/base.py` — `LanguageAdapter` Protocol
- `loop_fixer/adapters/python_pytest.py` — existing logic relocated
- `loop_fixer/adapters/java_maven.py` — new Maven adapter
- `loop_fixer/fsm.py` — states call through `state.adapter` instead of pytest-specific functions
- `loop_fixer/cli.py` — `--language` flag, adapter construction, simplified baseline check
- `loop_fixer/context.py` — generalize the path regex
- `tests/fixtures/broken_repo_java/` — Maven demo fixture

## Verification (this session)

1. `brew install openjdk maven` (ask for confirmation at execution time — this modifies the environment) and confirm `mvn -version`/`java -version` work afterward.
2. Run the full existing suite (`pytest tests/ -v`) to confirm the Python path is unaffected by the refactor.
3. Run `tests/test_adapters_java.py` (hermetic, no `mvn` needed) to confirm the adapter's pure logic.
4. With Maven installed, run `tests/test_e2e_demo_java.py` for the real hermetic-except-for-Maven end-to-end proof.
5. Live-run the actual CLI against the seeded Java fixture (`python -m loop_fixer --language java --test com.example.CalcTest#testAdd --repo /tmp/demo_java`) with a scripted fake LLM (matching how the Python demo was verified live earlier this session) — confirm convergence, inspect `git log`/`Calc.java`/`CalcTest.java`, and confirm the same test-file-protection and rollback guarantees hold. Fix any real bugs surfaced by this run before calling it done, per the user's brief.

## Execution note

Per the user's request, this plan's implementation is carried out by a forked-off agent working in an isolated **git worktree** on a new branch (not the current `main` checkout) — keeping this refactor's in-progress changes separate from the already-pushed `main` until it's reviewed and ready to merge.
