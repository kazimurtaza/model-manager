#!/usr/bin/env python3
"""Subprocess entrypoint for downloading a single file or a whole HF repo.

Run as:
    python downloader.py --repo ORG/NAME --type gguf|safetensors --dest DIR [--file FILE]

Exit codes: 0 = success, non-zero = failure (the last line of stderr is surfaced
to the user as the error message by the parent worker).

The HF token is read from the HF_TOKEN environment variable by huggingface_hub
automatically, so the parent just passes its environment through.

Note: `local_dir_use_symlinks` is intentionally NOT passed — it was deprecated and
removed in huggingface_hub 1.x; files are written directly into `local_dir`.
"""
import argparse
import sys

from huggingface_hub import hf_hub_download, snapshot_download


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a HF model file or repo.")
    parser.add_argument("--repo", required=True, help="HF repo id, e.g. org/name")
    parser.add_argument("--type", required=True, choices=["gguf", "safetensors"])
    parser.add_argument("--dest", required=True, help="Local destination directory")
    parser.add_argument("--file", default=None, help="Single file to download (omit for whole repo)")
    args = parser.parse_args()

    try:
        if args.file:
            hf_hub_download(repo_id=args.repo, filename=args.file, local_dir=args.dest)
        else:
            snapshot_download(repo_id=args.repo, local_dir=args.dest)
    except Exception as exc:  # surfaced to the user via stderr
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
