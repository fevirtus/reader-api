"""Isolated CPU inference. This module is only installed in the render image."""

import contextlib
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import wave
from pathlib import Path


def normalize(source):
    source = re.sub(r"<[^>]+>", " ", source)
    source = html.unescape(source)
    source = re.sub(r"[\t ]+", " ", source)
    return source.strip()


def load_model():
    from huggingface_hub import snapshot_download
    from vieneu import Vieneu

    revision = "61b85e3d937fbbacb387714180e8182823512523"
    model_dir = Path(os.environ.get("HF_HOME", "/models")) / ("reader-vieneu-" + revision)
    if not (model_dir / ".complete").is_file():
        snapshot = snapshot_download(
            "pnnbao-ump/VieNeu-TTS-v3-Turbo",
            revision=revision,
            allow_patterns=["onnx_update/*", "config.json", "voices_v3_turbo.json"],
        )
        model_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=model_dir.parent) as staging:
            # ONNX external weights must be real adjacent files, not HF blob symlinks.
            ready = Path(staging) / "model"
            shutil.copytree(snapshot, ready, symlinks=False)
            (ready / ".complete").touch()
            if model_dir.exists():
                shutil.rmtree(model_dir)
            os.replace(ready, model_dir)
    codec_repo = "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"
    codec_revision = "ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae"
    codec_dir = model_dir.parent / ("reader-codec-" + codec_revision)
    if not (codec_dir / ".complete").is_file():
        snapshot = snapshot_download(
            codec_repo, revision=codec_revision, allow_patterns=["*.onnx", "*.data", "*.json"]
        )
        with tempfile.TemporaryDirectory(dir=model_dir.parent) as staging:
            ready = Path(staging) / "codec"
            shutil.copytree(snapshot, ready, symlinks=False)
            (ready / ".complete").touch()
            if codec_dir.exists():
                shutil.rmtree(codec_dir)
            os.replace(ready, codec_dir)
    # SDK 3.7.1 does not forward codec_dir through its public constructor.
    # Scope this compatibility adapter to construction and forbid unpinned fetches.
    from unittest.mock import patch

    from vieneu._v3_turbo_engine.onnx_runtime_lite import OnnxV3LiteEngine

    def resolve_codec(repo, files, subfolder):
        if repo != codec_repo:
            raise ValueError("Unpinned model artifact requested")
        return codec_dir

    with patch.object(OnnxV3LiteEngine, "_fetch", staticmethod(resolve_codec)):
        model = Vieneu(
            backend="onnx",
            backbone_repo=str(model_dir),
            onnx_dir=str(model_dir / "onnx_update"),
            threads=int(os.getenv("AUDIOBOOK_THREADS", "1")),
        )
    return model


def synthesize(model, source, destination, voice):
    import numpy as np

    content = normalize(Path(source).read_text(encoding="utf-8"))
    if not content:
        raise ValueError("Empty chapter")
    samples = 0
    chunks = 0
    started = last_report = time.monotonic()
    logging.getLogger("audiobook-synthesize").info(
        "Synthesis started voice=%s chars=%s", voice, len(content)
    )
    with wave.open(destination, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        for chunk in model.infer_stream(content, voice=voice):
            values = np.asarray(chunk)
            if not np.isfinite(values).all():
                raise ValueError("Invalid audio samples")
            pcm = (np.clip(values, -1, 1) * 32767).astype("<i2")
            output.writeframes(pcm.tobytes())
            samples += pcm.size
            chunks += 1
            now = time.monotonic()
            if chunks == 1 or now - last_report >= 30:
                logging.getLogger("audiobook-synthesize").info(
                    "Synthesis progress chunks=%s audioSeconds=%.1f elapsedSeconds=%.1f",
                    chunks,
                    samples / 48000,
                    now - started,
                )
                last_report = now
    if samples == 0:
        raise ValueError("Empty audio")


def main():
    if sys.argv[1:] != ["--serve"]:
        synthesize(load_model(), *sys.argv[1:])
        return
    # stdout is a private JSON-lines protocol; SDK diagnostics go to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        started = time.monotonic()
        model = load_model()
    print(json.dumps({"ready": True, "loadSeconds": time.monotonic() - started}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                synthesize(model, request["source"], request["destination"], request["voice"])
            print(json.dumps({"ok": True, "renderSeconds": time.monotonic() - started}), flush=True)
        except Exception:
            # Do not send source text or library diagnostics into queue metadata.
            print(json.dumps({"ok": False}), flush=True)
            raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
