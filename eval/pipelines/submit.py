"""Compile and submit the evaluation pipeline to Vertex AI Pipelines (runs as the eval SA).

    python -m eval.pipelines.submit --image IMAGE --api-url URL [--wait] [--limit N] [--label L]

With --wait: blocks until the run finishes, copies the report from GCS into results/eval/, and
exits 0 if the run (and therefore the gate) succeeded, 1 otherwise.
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--image", required=True, help="app image (the one under test)")
    p.add_argument("--api-url", required=True)
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"))
    p.add_argument("--region", default="us-central1")
    p.add_argument("--bucket", required=True)
    p.add_argument("--service-account", required=True)
    p.add_argument("--judge-model", default="gemini-3.1-pro-preview")
    p.add_argument("--mode", default="")
    p.add_argument("--backend", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--label", default="pipeline")
    p.add_argument("--no-enforce", action="store_true", help="report only; never fail on the gate")
    p.add_argument("--wait", action="store_true")
    args = p.parse_args()

    os.environ["EVAL_IMAGE"] = args.image  # read at import time by the component definitions
    from google.cloud import aiplatform
    from kfp import compiler

    import eval.pipelines.eval_pipeline as pipeline_mod

    importlib.reload(pipeline_mod)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as tmp:
        template = Path(tmp) / "rag_eval.yaml"
        compiler.Compiler().compile(pipeline_mod.rag_eval, str(template))
        aiplatform.init(project=args.project, location=args.region)
        job = aiplatform.PipelineJob(
            display_name=f"rag-eval-{args.label}-{stamp}".lower()[:120],
            template_path=str(template),
            pipeline_root=f"gs://{args.bucket}/pipeline_root",
            parameter_values={
                "api_url": args.api_url,
                "project": args.project,
                "results_gcs": f"gs://{args.bucket}/results/eval",
                "judge_model": args.judge_model,
                "mode": args.mode,
                "backend": args.backend,
                "limit": args.limit,
                "label": args.label,
                "enforce_gate": "false" if args.no_enforce else "true",
            },
            enable_caching=False,
        )
        job.submit(service_account=args.service_account)
    print(f"submitted: {job.resource_name}", file=sys.stderr)
    if not args.wait:
        return 0
    try:
        job.wait()  # type: ignore[no-untyped-call]
    except Exception as exc:  # job.wait raises when the run fails (e.g. the gate)
        print(f"pipeline run did not succeed: {exc}", file=sys.stderr)
    state = job.state.name if job.state else "UNKNOWN"
    print(f"pipeline state: {state}", file=sys.stderr)
    # Copy this run's report(s) from GCS into results/ (best effort).
    reports = f"gs://{args.bucket}/results/eval/*_{args.label}.json"
    subprocess.run(["gcloud", "storage", "cp", "-n", reports, "results/eval/"], check=False)  # noqa: S603, S607
    return 0 if state == "PIPELINE_STATE_SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())
