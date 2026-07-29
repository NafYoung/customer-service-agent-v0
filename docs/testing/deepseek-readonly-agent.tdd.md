# DeepSeek read-only Agent v1 — TDD evidence

## Scope

- OpenAI-compatible non-streaming Chat Completions client for DeepSeek V4.
- Exact six-tool read-only allowlist.
- Bounded single-Agent tool loop with local argument validation.
- Natural-language read-only Eval harness with deterministic grading.
- No `prepare_*`, ticket, confirmation, execution, or debug capability exposed
  to the model.

## RED evidence

First targeted run:

```text
python -m pytest tests/test_openai_compatible_client.py tests/test_readonly_agent.py
```

Collection failed because `app.agent.openai_compatible` did not exist. This was
the expected failure for the new adapter boundary.

Second targeted run added DeepSeek configuration/factory tests and failed
because `app.agent.factory` did not exist.

Third targeted run added retry and finish-reason safety tests. It produced four
expected failures: retry constructor arguments were absent, and `length`,
`content_filter`, and unknown finish reasons were still accepted.

The Eval RED run failed because `evals.readonly_eval` did not exist.

A final adversarial RED batch added completion-token bounds, total tool-call
bounds, cross-round call-ID uniqueness, reserved request-field protection, and
missing finish-reason rejection. Six assertions failed before those controls
were implemented.

The packaging review then found that `.dockerignore` did not exclude `.env`.
A focused regression test failed before `.env`, `.env.*`, and the
`!.env.example` exception were added.

An independent adversarial review added a final five-test RED batch for:
explicit return/exchange facts, whole-batch tool preflight, and HTTPS-only
DeepSeek credentials. All five failed before the corresponding controls were
implemented.

## GREEN evidence

After the smallest implementation for each boundary:

```text
33 passed
```

The targeted suite covers:

- request URL, bearer header, request body and OpenAI function-tool shape;
- credential-safe HTTP errors;
- malformed provider responses;
- bounded 429/5xx retry;
- incomplete/unknown finish reasons;
- DeepSeek V4 Flash factory with thinking explicitly disabled;
- exact tool allowlist;
- valid, invalid, forbidden, cross-customer, multi-tool and max-round flows;
- model-context secret isolation;
- strict Eval case schema and expected-field isolation;
- deterministic tool-result and zero-business-write grading.

The final full suite produced:

```text
70 passed
89% branch coverage
8/8 Reference Eval cases passed
```

The no-key live Eval check exited with the expected configuration error before
making a network request. The exported read-only contract exactly matched the
runtime six-tool allowlist.

## Remaining evidence boundary

No `.env` values were inspected and no private `DEEPSEEK_API_KEY` was supplied
or used during the implementation and TDD cycle. A later live Eval loaded the
Key into the child process without printing it; its separate evidence is in
`deepseek-readonly-agent-live-eval.md`. The TDD suite itself proves local
adapter and control boundaries, not live model quality, availability, latency,
or cost.
