PACT PIPELINE v3.1.2d — BOM REPAIR HOTFIX

The traceback proves that the active pact_translate_v3.py still used
encoding="utf-8". Therefore the previous hotfix did not modify the active core.

This installer patches the files currently installed on disk by content:
- pact_translate_v3.py read_json -> utf-8-sig;
- runner JSON writers -> UTF-8 without BOM;
- removes BOM from the existing generated run config;
- preserves all translation and audit caches;
- verifies Python compilation and JSON parsing.

Install from PowerShell 7:
.\install_hotfix.ps1

Then resume:
cd D:\pact\pact_translator_v3\pact_full_pipeline_runner_v1
.\run_full_pipeline_v31.ps1 -Start 60 -End 60

Do not use -Reset, -RedoTranslation, or -RedoQuality.
