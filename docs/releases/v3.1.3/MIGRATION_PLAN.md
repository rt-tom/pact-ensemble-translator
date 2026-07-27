# v3.1.3 migration plan

The three v3.1.3 schemas are new run-scoped artifacts, not conversions of v3.1.2k artifacts. Deployment never rewrites `pipeline_runs`, resumes chapter 60, or deletes legacy artifacts. New clean v3.1.3 runs create the chapter manifest and cache identity on first use; legacy-reuse provenance is created lazily. Rollback preserves all run artifacts.
