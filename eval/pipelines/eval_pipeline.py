"""Vertex AI Pipelines (KFP v2) definition: answer -> judge -> score & gate.

Container components run the application image itself (`python -m eval.steps ...`), so the
pipeline evaluates exactly the code that is deployed and needs no extra packages in the image.
Steps exchange JSON artifacts through the pipeline root in GCS. The score step exits non-zero
when the regression gate fails, which fails the run and blocks promotion (scripts/release.sh).

Compiled by eval.pipelines.submit with the image to use:
    python -m eval.pipelines.submit --image <image> --api-url <url> [--wait]
"""

# No `from __future__ import annotations`: KFP inspects these annotations at runtime.
import os

from kfp import dsl

IMAGE = os.environ.get("EVAL_IMAGE", "us-docker.pkg.dev/cloudrun/container/placeholder")


@dsl.container_component
def answer_questions(
    api_url: str, mode: str, backend: str, limit: int, answers: dsl.Output[dsl.Artifact]
):
    return dsl.ContainerSpec(
        image=IMAGE,
        command=["python", "-m", "eval.steps", "answer"],
        args=[
            "--api-url",
            api_url,
            "--mode",
            mode,
            "--backend",
            backend,
            "--limit",
            limit,
            "--out",
            answers.path,
        ],
    )


@dsl.container_component
def judge_answers(
    answers: dsl.Input[dsl.Artifact],
    project: str,
    location: str,
    model: str,
    limit: int,
    judgments: dsl.Output[dsl.Artifact],
):
    return dsl.ContainerSpec(
        image=IMAGE,
        command=["python", "-m", "eval.steps", "judge"],
        args=[
            "--answers",
            answers.path,
            "--out",
            judgments.path,
            "--project",
            project,
            "--location",
            location,
            "--model",
            model,
            "--limit",
            limit,
        ],
    )


@dsl.container_component
def score_and_gate(
    answers: dsl.Input[dsl.Artifact],
    judgments: dsl.Input[dsl.Artifact],
    label: str,
    results_gcs: str,
    enforce_gate: str,
    limit: int,
    report: dsl.Output[dsl.Artifact],
):
    return dsl.ContainerSpec(
        image=IMAGE,
        command=["python", "-m", "eval.steps", "score"],
        args=[
            "--answers",
            answers.path,
            "--judgments",
            judgments.path,
            "--out",
            report.path,
            "--label",
            label,
            "--results-gcs",
            results_gcs,
            "--enforce-gate",
            enforce_gate,
            "--limit",
            limit,
        ],
    )


@dsl.pipeline(
    name="rag-eval", description="Golden-set evaluation of the RAG API with a regression gate"
)
def rag_eval(
    api_url: str,
    project: str,
    results_gcs: str,
    judge_model: str = "gemini-3.1-pro-preview",
    judge_location: str = "global",
    mode: str = "",
    backend: str = "",
    limit: int = 0,
    label: str = "pipeline",
    enforce_gate: str = "true",
):
    a = answer_questions(api_url=api_url, mode=mode, backend=backend, limit=limit)
    j = judge_answers(
        answers=a.outputs["answers"],
        project=project,
        location=judge_location,
        model=judge_model,
        limit=limit,
    )
    s = score_and_gate(
        answers=a.outputs["answers"],
        judgments=j.outputs["judgments"],
        label=label,
        results_gcs=results_gcs,
        enforce_gate=enforce_gate,
        limit=limit,
    )
    for task in (a, j, s):
        task.set_caching_options(False)  # an evaluation must always really run
    s.set_display_name("score-and-gate")
