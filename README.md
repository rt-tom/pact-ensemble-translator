# Pact Translator v3.0

v3 replaces the full segment self-review with a cheaper and safer pipeline:

```text
chapter bible
→ plain-text segmented translation
→ deterministic QA
→ bilingual issue-only audit
→ targeted repair of flagged PIDs
→ semantic restoration of inline formatting
→ final integrity checks
```

The old v2 folders are not touched. v3 uses:

```text
pact_work_v3/
pact_translated_v3/
logs_v3/
```

## Major changes

- Translation returns JSON `PID → plain Russian text`, not HTML.
- No `[[FMT_...]]` tokens in the main translation.
- No full second rewrite of every 900-word segment.
- Reviewer returns only a list of concrete problems.
- Gemma repairs only flagged PIDs.
- `<em>`, `<strong>`, `<i>`, `<b>` and `<a>` are restored separately by
  mapping the source emphasis to an exact substring in the Russian text.
- If formatting mapping fails, the tag is omitted and logged; the chapter is
  not translated again.
- `character` is now treated as a proper-name glossary type.
- Provisional glossary entries are included in later prompts.
- Persistent `book_bible.json` stores characters, gender, entities, objects,
  address register and factual constraints across chapters.
- The final HTML `<title>` is translated.
- The audit report displays the original with its actual formatting.

## Install

Copy the folder to:

```text
D:\pact\pact_translator_v3
```

Either copy the chapter HTML files into:

```text
D:\pact\pact_translator_v3\pact_chapters
```

or change `paths.input_dir` in `config.v3.json` to the existing v2 input
directory.

```powershell
cd D:\pact\pact_translator_v3
py -m pip install -r requirements.txt
py .\pact_translate_v3.py --version
py .\pact_translate_v3.py --self-test
```

## New llama.cpp profile

Start:

```powershell
.\server_profiles\start_llama_v3.ps1
```

It applies all proposed text-only changes at once:

- removes MMProj;
- changes context from `49152` to `32768`;
- sets `--ctx-checkpoints 0`;
- sets `--reasoning-budget 0`;
- retains MTP, Vulkan0, `-ngl 99`, `-ncmoe 18`, `--no-mmap`,
  `-np 1`, flash attention and Jinja.

Then verify:

```powershell
py .\pact_translate_v3.py --smoke-test
```

Required result:

```json
"reasoning_content_chars": 0
```

The old profile is included as:

```text
server_profiles\start_llama_baseline.ps1
```

## Benchmark all-at-once changes

Before replacing the old profile:

```powershell
.\server_profiles\start_llama_baseline.ps1
py .\benchmark_server.py --label baseline
```

Stop the server, start the v3 profile, then:

```powershell
py .\benchmark_server.py --label v3-all-changes
```

The results are saved in `benchmark_results`. This supports the proposed
strategy: make all changes first, then roll back one at a time if the fixed
benchmark or a real chapter exposes a regression.

Recommended rollback order:

1. raise context to `49152` only if a real prompt does not fit;
2. try `--ctx-checkpoints 1`, then the old default, only if disabling them
   hurts repeat-request performance;
3. keep MMProj disabled for text translation;
4. keep `--reasoning-budget 0` for the translator unless reasoning is
   deliberately re-enabled and benchmarked.

## Plan and run

```powershell
py .\pact_translate_v3.py --plan --start 1 --end 1
py .\pact_translate_v3.py --start 1 --end 1
```

Resume is the default behavior; this command is also accepted:

```powershell
py .\pact_translate_v3.py --resume --start 1 --end 1
```

`--force` deletes the selected chapter's entire v3 work folder.

## Separate phases and a different reviewer

Default `--phase all` uses the configured server for all phases.

```powershell
py .\pact_translate_v3.py --phase translate --start 1 --end 1
py .\pact_translate_v3.py --phase audit --start 1 --end 1
py .\pact_translate_v3.py --phase repair --start 1 --end 1
py .\pact_translate_v3.py --phase finalize --start 1 --end 1
```

This allows model switching:

1. translate with Gemma;
2. stop Gemma and start a reviewer on another port;
3. set `reviewer_api` URL/model in `config.v3.json`;
4. run `--phase audit`;
5. restart Gemma and run repair/finalize.

A nonfunctional path template is included:

```text
server_profiles\start_reviewer_gpt_oss_TEMPLATE.ps1
```

Enter the real gpt-oss model path and tested parameters before using it.

## Selective rebuild

```powershell
py .\pact_translate_v3.py --redo-audit --start 1 --end 1
py .\pact_translate_v3.py --redo-repair --start 1 --end 1
py .\pact_translate_v3.py --redo-formatting --start 1 --end 1
```

## Outputs

```text
pact_work_v3\<chapter>\
  manifest.json
  chapter_bible.json
  draft_translations.json
  deterministic_issues.json
  issues.json
  repaired_translations.json
  repair_records.json
  quality_report.json
  audit_report.html
  drafts\
  audit\
  repairs\
  formatting\

pact_translated_v3\<chapter>.html
```

## Reviewer benchmark

A chapter-1 benchmark contains 48 known problematic PIDs and 24 clean
controls:

```powershell
py .\reviewer_benchmark.py
```

For a reviewer on port 8081:

```powershell
py .\reviewer_benchmark.py `
  --url http://127.0.0.1:8081/v1/chat/completions `
  --model YOUR_REVIEWER_MODEL
```

Compare:

- recall;
- false-positive rate;
- missed known-error PIDs;
- incorrectly flagged clean controls.

Do not select a reviewer from one attractive correction. Use the fixed
benchmark and then validate its proposed repairs.
