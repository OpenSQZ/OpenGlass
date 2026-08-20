"""Explicitly download a ModelScope model for the local Harness.

The runtime itself deliberately accepts only a local ``--model-path``.  Keeping
download and inference as separate commands makes network access visible and
keeps device runs reproducible.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


DEFAULT_MODEL_ID = (
    "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download a ModelScope model and print its local directory"
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--revision", default=None)
    location = parser.add_mutually_exclusive_group()
    location.add_argument(
        "--cache-dir",
        default=None,
        help="ModelScope cache root; the model ID is appended below it",
    )
    location.add_argument(
        "--local-dir",
        default=None,
        help="exact destination directory for this model",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        from modelscope.hub.snapshot_download import snapshot_download
    except ImportError as exc:
        raise SystemExit(
            "ModelScope is not installed. Run: "
            "python -m pip install -r extensions/assistive_harness/requirements.txt"
        ) from exc

    kwargs: dict[str, str] = {}
    if args.revision:
        kwargs["revision"] = args.revision
    if args.cache_dir:
        kwargs["cache_dir"] = str(Path(args.cache_dir).expanduser().resolve())
    if args.local_dir:
        kwargs["local_dir"] = str(Path(args.local_dir).expanduser().resolve())

    print(f"Downloading ModelScope model: {args.model_id}", flush=True)
    model_path = Path(snapshot_download(args.model_id, **kwargs)).resolve()
    print(f"MODEL_PATH={model_path}")
    print("Start the Harness with:")
    print(
        f'"{sys.executable}" -m extensions.assistive_harness.server '
        f'--enabled --model-path "{model_path}" --port 8021'
    )


if __name__ == "__main__":
    main()
